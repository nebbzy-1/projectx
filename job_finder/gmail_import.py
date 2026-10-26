from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import base64
import os
from pathlib import Path
import re
from html import unescape
from urllib.parse import parse_qs, unquote, urlsplit

from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .env import get_env
from .database import (
    get_metadata,
    job_fingerprint,
    job_match_key,
    normalize_url,
    set_metadata,
    upsert_jobs_with_ids,
)


ROOT = Path(__file__).resolve().parent.parent
GMAIL_CLIENT_PATH = ROOT / "data" / "gmail_oauth_client.json"
GMAIL_TOKEN_PATH = ROOT / "data" / "gmail_token.json"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
GMAIL_MIGRATION_KEY = "gmail_import_table_ready"
PARSER_VERSION = 6

SEEK_JOB_PATH = re.compile(r"/(?:job|jobs)/(\d+)(?:/|$)", re.IGNORECASE)
AU_LOCATION = re.compile(
    r"\b(Perth|Melbourne|Sydney|Brisbane|Gold Coast|Adelaide|Canberra|Hobart|Darwin)\b"
    r"\s*(?:,\s*|\s+)?(WA|VIC|NSW|QLD|SA|ACT|TAS|NT)?\b",
    re.IGNORECASE,
)
POSTED_DAYS = re.compile(r"posted\s+(\d+)\s+days?\s+ago", re.IGNORECASE)
SALARY_LINE = re.compile(
    r"(?:\$|\bAUD\b|\bper\s+(?:year|annum|hour|week)\b|\bp\.?a\.?\b|\bsuper\b|"
    r"\bcompetitive remuneration\b|\bremuneration\b|\bsalary\b|\bbase\b\s*\+?)",
    re.IGNORECASE,
)
POSTED_LINE = re.compile(r"\bposted\s+(?:on\s+)?(?:today|yesterday|\d|\d{1,2}\s+[a-z]{3})", re.IGNORECASE)


class GmailSetupError(RuntimeError):
    pass


@dataclass
class GmailSyncSummary:
    messages_seen: int
    messages_processed: int
    jobs_found: int
    jobs_added: int
    jobs_existing: int
    messages_without_jobs: int
    messages_without_body: int
    new_job_ids: list[int] = field(default_factory=list)


def gmail_client_configured() -> bool:
    return bool(get_env("GOOGLE_CLIENT_ID") and get_env("GOOGLE_CLIENT_SECRET")) or GMAIL_CLIENT_PATH.is_file()


def gmail_connected() -> bool:
    return GMAIL_TOKEN_PATH.is_file()


def _save_token(credentials: Credentials) -> None:
    GMAIL_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = GMAIL_TOKEN_PATH.with_suffix(".tmp")
    temporary.write_text(credentials.to_json(), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(GMAIL_TOKEN_PATH)


def authorize_gmail() -> Credentials:
    if not gmail_client_configured():
        raise GmailSetupError(
            "Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to the project .env file first."
        )
    client_id = get_env("GOOGLE_CLIENT_ID")
    client_secret = get_env("GOOGLE_CLIENT_SECRET")
    if client_id and client_secret:
        redirect_uris = [
            uri.strip() for uri in get_env("GOOGLE_REDIRECT_URIS", "http://localhost").split(",")
            if uri.strip()
        ] or ["http://localhost"]
        client_config = {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "project_id": get_env("GOOGLE_PROJECT_ID"),
                "auth_uri": get_env("GOOGLE_AUTH_URI", "https://accounts.google.com/o/oauth2/auth"),
                "token_uri": get_env("GOOGLE_TOKEN_URI", "https://oauth2.googleapis.com/token"),
                "auth_provider_x509_cert_url": get_env(
                    "GOOGLE_AUTH_PROVIDER_X509_CERT_URL",
                    "https://www.googleapis.com/oauth2/v1/certs",
                ),
                "redirect_uris": redirect_uris,
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, scopes=GMAIL_SCOPES)
    else:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(GMAIL_CLIENT_PATH), scopes=GMAIL_SCOPES
        )
    credentials = flow.run_local_server(port=0, open_browser=True)
    _save_token(credentials)
    return credentials


def _load_credentials() -> Credentials:
    if not GMAIL_TOKEN_PATH.is_file():
        raise GmailSetupError("Connect your Gmail account before syncing SEEK alerts.")
    try:
        credentials = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_PATH), GMAIL_SCOPES)
    except (ValueError, OSError) as error:
        raise GmailSetupError("The saved Gmail token is invalid. Connect Gmail again.") from error
    if credentials.scopes and not set(GMAIL_SCOPES).issubset(credentials.scopes):
        raise GmailSetupError(
            "Gmail permissions changed. Remove data/gmail_token.json and connect Gmail again."
        )
    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            _save_token(credentials)
        except Exception as error:
            raise GmailSetupError(
                "Gmail authorization expired or was revoked. Connect Gmail again."
            ) from error
    if not credentials.valid:
        raise GmailSetupError("Connect Gmail again to renew access.")
    return credentials


def gmail_service():
    return build("gmail", "v1", credentials=_load_credentials(), cache_discovery=False)


def _decode_part(part: dict, attachment_loader=None) -> str:
    body = part.get("body", {})
    data = body.get("data")
    if not data and body.get("attachmentId") and attachment_loader:
        try:
            data = attachment_loader(body["attachmentId"])
        except Exception:
            data = None
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _message_bodies(payload: dict, attachment_loader=None) -> tuple[str, str]:
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict) -> None:
        mime = str(part.get("mimeType", "")).casefold().split(";", 1)[0].strip()
        body = _decode_part(part, attachment_loader)
        if mime.startswith("text/plain") and body:
            plain.append(body)
        elif mime.startswith("text/html") and body:
            html.append(body)
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    return "\n".join(plain), "\n".join(html)


def _header_map(payload: dict) -> dict[str, str]:
    return {
        str(header.get("name", "")).casefold(): str(header.get("value", ""))
        for header in payload.get("headers", [])
    }


def _clean_url(raw_url: str) -> str:
    candidate = unquote(unescape(raw_url.strip().replace("&amp;", "&")))
    for _ in range(3):
        try:
            candidate = unquote(unescape(candidate))
            parts = urlsplit(candidate)
            host = (parts.hostname or "").casefold()
        except ValueError:
            return ""
        values = {key.casefold(): value for key, value in parse_qs(parts.query).items()}
        path_match = SEEK_JOB_PATH.search(parts.path)
        seek_host = (
            host in {"seek.com.au", "au.seek.com", "nz.seek.com", "seek.com"}
            or host.endswith(".seek.com.au")
            or host.endswith(".seek.com")
        )
        if seek_host:
            job_id = path_match.group(1) if path_match else next(
                (value[0] for key in ("jobid", "job_id", "job-id") if (value := values.get(key))), None
            )
            if job_id and job_id.isdigit():
                return f"https://www.seek.com.au/job/{job_id}"
        elif not host and path_match:
            return f"https://www.seek.com.au/job/{path_match.group(1)}"
        nested = next(
            (value for key in ("url", "u", "target", "redirect", "redirect_url", "destination", "link")
             for value in values.get(key, [])),
            None,
        )
        if not nested or nested == candidate:
            return ""
        candidate = nested
    return ""


def _is_seek_job_url(raw_url: str) -> bool:
    url = _clean_url(raw_url)
    if not url:
        return False
    parts = urlsplit(url)
    return bool(SEEK_JOB_PATH.search(parts.path))


def _normal_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _card_for(anchor, title: str):
    for parent in anchor.parents:
        if parent.name in {"tr", "li", "article"}:
            text = _normal_text(parent.get_text(" ", strip=True))
            if title.casefold() in text.casefold() and len(text) <= 1800:
                return parent
        classes = " ".join(parent.get("class", [])).casefold()
        if any(word in classes for word in ("job", "listing", "card", "result")):
            text = _normal_text(parent.get_text(" ", strip=True))
            if title.casefold() in text.casefold() and len(text) <= 1800:
                return parent
    return anchor.parent or anchor


def _extract_location(value: str) -> str | None:
    matches = list(AU_LOCATION.finditer(value))
    if not matches:
        return None
    match = next((candidate for candidate in reversed(matches) if candidate.group(2)), matches[-1])
    city = match.group(1).title()
    state = (match.group(2) or "").upper()
    return f"{city}, {state}" if state else city


def _is_location_line(value: str) -> bool:
    """Tell a location card line from a role title that mentions a city."""
    line = _normal_text(value)
    if not _extract_location(line):
        return False
    matches = list(AU_LOCATION.finditer(line))
    match = next((candidate for candidate in reversed(matches) if candidate.group(2)), matches[-1])
    before = line[:match.start()].strip()
    after = line[match.end():].strip(" ,.-")
    if before and not before.endswith(","):
        return False
    if after and after.casefold() not in {"cbd", "metro", "inner city", "inner north", "inner south"}:
        return False
    return len(line) <= 90


def _extract_company(card, anchor, title: str) -> str | None:
    for sibling_anchor in card.find_all("a", href=True):
        if sibling_anchor is anchor:
            continue
        label = _normal_text(sibling_anchor.get_text(" ", strip=True))
        if label.casefold() == title.casefold():
            continue
        if 2 <= len(label) <= 90 and not re.search(
            r"\b(view|apply|save|more jobs|see all|learn more)\b", label, re.IGNORECASE
        ):
            return label

    title_tail = re.search(r"\b(?:at|with)\s+([^|·]{2,90})$", title, re.IGNORECASE)
    if title_tail:
        return _normal_text(title_tail.group(1))

    lines = [_normal_text(line) for line in card.get_text("\n", strip=True).splitlines()]
    lines = [line for line in lines if line and line.casefold() != title.casefold()]
    for line in lines:
        if _is_location_line(line):
            continue
        if len(line) > 2 and len(line) < 90 and not re.search(
            r"\b(new jobs|posted|salary|apply|save|view|job alert|seek)\b", line, re.IGNORECASE
        ):
            return line
    return None


def _extract_posted_date(text: str, received_at: datetime) -> str | None:
    lowered = text.casefold()
    if "posted today" in lowered or "posted 0 days ago" in lowered:
        return received_at.date().isoformat()
    if "posted yesterday" in lowered:
        return (received_at - timedelta(days=1)).date().isoformat()
    match = POSTED_DAYS.search(text)
    if match:
        return (received_at - timedelta(days=int(match.group(1)))).date().isoformat()
    return None


def _is_plain_job_detail(value: str) -> bool:
    line = _normal_text(value)
    if not line or line.casefold() == "logo":
        return False
    if re.search(r"https?://", line, re.IGNORECASE) or _clean_url(line):
        return False
    if _is_location_line(line) or SALARY_LINE.search(line) or POSTED_LINE.search(line):
        return False
    if re.fullmatch(r"[\d,.$%+\-–\s]+", line):
        return False
    if re.search(
        r"\b(seek job alert|unsubscribe|manage alerts?|view all jobs|see all jobs|"
        r"view all matching jobs|take your next career step|job opportunities|explore now|"
        r"jobs you may have missed|download seek app|apple store|"
        r"find out how to make your saved search|job alert|new jobs matching|click here|"
        r"view job|apply now|privacy policy)\b",
        line,
        re.IGNORECASE,
    ):
        return False
    if line.casefold().startswith(("hi ", "hello ", "dear ")):
        return False
    return 2 <= len(line) <= 180


def _extract_plain_seek_jobs(plain_body: str, received_at: datetime) -> list[dict]:
    lines = [_normal_text(line) for line in plain_body.splitlines()]
    matches: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        for found in re.finditer(r"https?://[^\s<>]+", line, re.IGNORECASE):
            url = _clean_url(found.group(0).rstrip(").,;]}>'\""))
            if not url or not _is_seek_job_url(url):
                continue
            prefix = _normal_text(re.sub(r"[\[\]()*_`|]", " ", line[:found.start()]))
            suffix = _normal_text(re.sub(r"[\[\]()*_`|]", " ", line[found.end():]))
            matches.append((index, url.split("#", 1)[0], prefix or suffix))

    jobs: list[dict] = []
    seen_urls: set[str] = set()
    for match_index, (line_index, url, same_line) in enumerate(matches):
        if url in seen_urls:
            continue
        next_url_line = matches[match_index + 1][0] if match_index + 1 < len(matches) else len(lines)
        section = [same_line] if same_line else []
        section.extend(lines[line_index + 1:next_url_line])
        # SEEK alerts put the direct URL immediately before the title. Cap the
        # card scan so the final listing cannot absorb the email footer.
        section = [line for line in section[:12] if line]
        title = next((line for line in section if _is_plain_job_detail(line)), "")
        if not title:
            continue
        title_index = section.index(title)
        location = next((_extract_location(line) for line in section if _is_location_line(line)), None)
        if not location:
            location = next(
                (_extract_location(line) for line in lines[max(0, line_index - 2):line_index]
                 if _is_location_line(line)),
                None,
            )
        if not location:
            location = _extract_location(" ".join(section))
        company = next(
            (
                line for line in section[title_index + 1:]
                if line.casefold() != title.casefold()
                and not _is_location_line(line)
                and _is_plain_job_detail(line)
            ),
            None,
        )
        description_parts = [
            line for line in section
            if line.casefold() not in {title.casefold(), (company or "").casefold()}
            and not _is_location_line(line)
            and _is_plain_job_detail(line)
        ]
        description = _normal_text(" ".join(description_parts))[:2000] or None
        jobs.append(
            {
                "title": title,
                "company": company,
                "location": location,
                "description": description,
                "job_url": url,
                "site": "seek",
                "date_posted": _extract_posted_date(" ".join(section), received_at),
            }
        )
        seen_urls.add(url)
    return jobs


def _extract_html_seek_jobs(html_body: str, received_at: datetime) -> list[dict]:
    jobs: list[dict] = []
    seen_urls: set[str] = set()
    if html_body:
        soup = BeautifulSoup(html_body, "html.parser")
        for anchor in soup.find_all(True):
            raw_url = next(
                (str(anchor.get(attribute, "")) for attribute in ("href", "data-href", "data-url")
                 if anchor.get(attribute)),
                "",
            )
            if not raw_url:
                continue
            url = _clean_url(raw_url)
            if not url or not _is_seek_job_url(url):
                continue
            normalized_url = url.split("#", 1)[0]
            if normalized_url in seen_urls:
                continue
            title = _normal_text(anchor.get_text(" ", strip=True) or anchor.get("aria-label", "") or anchor.get("title", ""))
            card = _card_for(anchor, title)
            if not title or title.casefold() in {"view job", "see job", "apply now", "click here"}:
                title = next(
                    (
                        _normal_text(candidate.get_text(" ", strip=True))
                        for candidate in card.find_all(["h1", "h2", "h3", "h4", "a", "strong"])
                        if candidate is not anchor
                        and 2 <= len(_normal_text(candidate.get_text(" ", strip=True))) <= 180
                        and _normal_text(candidate.get_text(" ", strip=True)).casefold()
                        not in {"view job", "see job", "apply now", "click here"}
                    ),
                    "",
                )
            if not title or len(title) > 180:
                continue
            card_text = _normal_text(card.get_text(" ", strip=True))
            location = _extract_location(card_text)
            company = _extract_company(card, anchor, title)
            snippet = card_text.replace(title, " ", 1)
            if company:
                snippet = snippet.replace(company, " ", 1)
            if location:
                snippet = snippet.replace(location, " ", 1)
            description = _normal_text(snippet)[:2000] or None
            jobs.append(
                {
                    "title": title,
                    "company": company,
                    "location": location,
                    "description": description,
                    "job_url": normalized_url,
                    "site": "seek",
                    "date_posted": _extract_posted_date(card_text, received_at),
                }
            )
            seen_urls.add(normalized_url)

    return jobs


def extract_seek_jobs(html_body: str, plain_body: str, received_at: datetime) -> list[dict]:
    # The message's plain-text alternative contains direct SEEK listing URLs.
    # Its HTML alternative uses opaque click-tracking URLs, so prefer the
    # direct links and use HTML parsing only for older alert formats.
    plain_jobs = _extract_plain_seek_jobs(plain_body, received_at) if plain_body else []
    return plain_jobs or _extract_html_seek_jobs(html_body, received_at)


def _merge_duplicate_job_state(connection, source_job_id: int, target_job_id: int) -> None:
    source = connection.execute(
        "SELECT status, notes FROM jobs WHERE id = ?",
        (source_job_id,),
    ).fetchone()
    target = connection.execute(
        "SELECT status, notes FROM jobs WHERE id = ?",
        (target_job_id,),
    ).fetchone()
    if not source or not target:
        return
    source_status = source["status"]
    target_status = target["status"]
    if target_status == "new" and source_status != "new":
        target_status = source_status
    elif source_status in {"applied", "interviewing", "rejected", "closed"} and target_status not in {
        "applied", "interviewing", "rejected", "closed"
    }:
        target_status = source_status
    source_notes = str(source["notes"] or "").strip()
    target_notes = str(target["notes"] or "").strip()
    if source_notes and target_notes and source_notes != target_notes:
        merged_notes = f"{target_notes}\n\n{source_notes}"
    else:
        merged_notes = target_notes or source_notes
    connection.execute(
        "UPDATE jobs SET status = ?, notes = ? WHERE id = ?",
        (target_status, merged_notes, target_job_id),
    )

    source_latest = connection.execute(
        "SELECT added_at, is_new FROM latest_jobs WHERE job_id = ?", (source_job_id,)
    ).fetchone()
    target_latest = connection.execute(
        "SELECT added_at, is_new FROM latest_jobs WHERE job_id = ?", (target_job_id,)
    ).fetchone()
    if source_latest:
        if target_latest:
            connection.execute(
                "UPDATE latest_jobs SET added_at = ?, is_new = ? WHERE job_id = ?",
                (
                    min(source_latest["added_at"], target_latest["added_at"]),
                    int(bool(source_latest["is_new"] or target_latest["is_new"])),
                    target_job_id,
                ),
            )
        else:
            connection.execute(
                "UPDATE latest_jobs SET job_id = ? WHERE job_id = ?",
                (target_job_id, source_job_id),
            )


def _upsert_seek_jobs(connection, jobs: list[dict]) -> tuple[int, int, list[int]]:
    """Upsert SEEK listings, repairing records made by the earlier bad parser."""
    unique_jobs: dict[str, dict] = {}
    for job in jobs:
        url = normalize_url(job.get("job_url"))
        if url:
            current = unique_jobs.get(url)
            if current is None:
                unique_jobs[url] = job
                continue

            def quality(candidate: dict) -> tuple[int, int, int, int]:
                title = str(candidate.get("title") or "").strip()
                role_signal = int(bool(re.search(
                    r"\b(law|lawyer|legal|solicitor|associate|clerk|paralegal|graduate|counsel|"
                    r"officer|secretary|assistant|trainee|reviewer|case manager)\b",
                    title,
                    re.IGNORECASE,
                )))
                return (
                    role_signal,
                    int(bool(candidate.get("company"))),
                    int(bool(candidate.get("location"))),
                    len(title),
                )

            if quality(job) > quality(current):
                unique_jobs[url] = job

    # A previous parser could assign several distinct listing URLs to one row
    # because it mistook a location or salary for the job title. If we have
    # corrected data for those URLs now, split the row before updating it.
    job_ids_by_url: dict[str, int] = {}
    for url in unique_jobs:
        row = connection.execute(
            "SELECT job_id FROM job_sources WHERE source = 'seek' AND url = ?", (url,)
        ).fetchone()
        if row:
            job_ids_by_url[url] = int(row["job_id"])
    urls_by_job_id: dict[int, list[str]] = {}
    for url, job_id in job_ids_by_url.items():
        urls_by_job_id.setdefault(job_id, []).append(url)
    for job_id, urls in urls_by_job_id.items():
        if len(urls) < 2:
            continue
        for url in sorted(urls)[1:]:
            connection.execute(
                "DELETE FROM job_sources WHERE source = 'seek' AND url = ? AND job_id = ?",
                (url, job_id),
            )

    added = existing = 0
    added_ids: list[int] = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for url, job in unique_jobs.items():
        key = job_match_key(job)
        link = connection.execute(
            "SELECT job_id FROM job_sources WHERE source = 'seek' AND url = ?", (url,)
        ).fetchone()
        if link:
            old_job_id = int(link["job_id"])
            target = connection.execute(
                "SELECT id FROM jobs WHERE match_key = ? AND id != ? ORDER BY id LIMIT 1",
                (key, old_job_id),
            ).fetchone() if key else None
            source_count = connection.execute(
                "SELECT COUNT(*) FROM job_sources WHERE job_id = ?", (old_job_id,)
            ).fetchone()[0]
            if target:
                target_job_id = int(target["id"])
                _merge_duplicate_job_state(connection, old_job_id, target_job_id)
                connection.execute(
                    "UPDATE job_sources SET job_id = ?, date_posted = COALESCE(?, date_posted) "
                    "WHERE source = 'seek' AND url = ?",
                    (target_job_id, job.get("date_posted"), url),
                )
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM job_sources WHERE job_id = ?", (old_job_id,)
                ).fetchone()[0]
                if remaining == 0:
                    connection.execute("DELETE FROM jobs WHERE id = ?", (old_job_id,))
                existing += 1
                continue
            if source_count == 1:
                # The row belongs only to this SEEK link, so safely correct its
                # mistaken fields in place while retaining notes and status.
                fingerprint = job_fingerprint(job)
                connection.execute(
                    "UPDATE jobs SET fingerprint = ?, match_key = ?, title = ?, "
                    "company = ?, location = ?, source = 'seek', url = ?, description = ?, "
                    "date_posted = COALESCE(?, date_posted), last_seen = ? "
                    "WHERE id = ?",
                    (
                        fingerprint,
                        key or None,
                        job.get("title") or "Untitled role",
                        job.get("company"),
                        job.get("location"),
                        url,
                        job.get("description"),
                        job.get("date_posted"),
                        now,
                        old_job_id,
                    ),
                )
                connection.execute(
                    "UPDATE job_sources SET date_posted = COALESCE(?, date_posted) "
                    "WHERE source = 'seek' AND url = ?",
                    (job.get("date_posted"), url),
                )
                existing += 1
                continue
            # Keep other board links intact; only relink this SEEK result and
            # let the normal deduplicator find or create the corrected record.
            connection.execute(
                "DELETE FROM job_sources WHERE source = 'seek' AND url = ?", (url,)
            )
        new_count, _, new_ids = upsert_jobs_with_ids(connection, [job])
        if new_count:
            added += 1
            added_ids.extend(new_ids)
        else:
            existing += 1
    connection.commit()
    return added, existing, added_ids


def _remove_seek_alert_ctas(connection) -> int:
    """Remove imported alert buttons that the earlier parser treated as jobs."""
    rows = connection.execute(
        """SELECT DISTINCT j.id, j.status, j.notes
           FROM jobs j JOIN job_sources s ON s.job_id = j.id
           WHERE s.source = 'seek' AND lower(trim(j.title)) IN (
               'take your next career step', 'view all matching jobs', 'explore now',
               'jobs you may have missed', 'download seek app'
           )"""
    ).fetchall()
    removed = 0
    for row in rows:
        connection.execute("DELETE FROM job_sources WHERE job_id = ? AND source = 'seek'", (row["id"],))
        remaining = connection.execute(
            "SELECT COUNT(*) FROM job_sources WHERE job_id = ?", (row["id"],)
        ).fetchone()[0]
        if remaining:
            continue
        if row["status"] == "new" and not str(row["notes"] or "").strip():
            connection.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))
            removed += 1
        else:
            connection.execute("UPDATE jobs SET source = NULL, url = NULL WHERE id = ?", (row["id"],))
    connection.commit()
    return removed


def ensure_gmail_tables(connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS gmail_imported_messages (
            message_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            received_at TEXT NOT NULL,
            subject TEXT NOT NULL DEFAULT '',
            imported_at TEXT NOT NULL,
            jobs_found INTEGER NOT NULL DEFAULT 0,
            trashed_at TEXT
        )"""
    )
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(gmail_imported_messages)")}
    if "parser_version" not in columns:
        connection.execute(
            "ALTER TABLE gmail_imported_messages ADD COLUMN parser_version INTEGER NOT NULL DEFAULT 1"
        )
    if not get_metadata(connection, GMAIL_MIGRATION_KEY):
        set_metadata(connection, GMAIL_MIGRATION_KEY, "true")
    connection.commit()


def _processed_ids(connection) -> set[str]:
    return {
        row["message_id"]
        for row in connection.execute(
            "SELECT message_id FROM gmail_imported_messages "
            "WHERE jobs_found > 0 AND parser_version = ?",
            (PARSER_VERSION,),
        ).fetchall()
    }


def sync_seek_alerts(connection, config: dict) -> GmailSyncSummary:
    service = gmail_service()
    ensure_gmail_tables(connection)
    query = str(config.get("gmail_query", "label:SEEK")).strip() or "label:SEEK"
    processed = _processed_ids(connection)
    messages: list[dict] = []
    page_token = None
    max_messages = max(1, min(int(config.get("gmail_max_messages", 250)), 1000))
    while len(messages) < max_messages:
        page = service.users().messages().list(
            userId="me", q=query, maxResults=min(100, max_messages - len(messages)), pageToken=page_token
        ).execute()
        messages.extend(page.get("messages", []))
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    pending = [message for message in messages if message["id"] not in processed]
    jobs_found = no_jobs = no_body = done = 0
    all_jobs: list[dict] = []
    imported_records: list[tuple[dict, dict, datetime, int]] = []
    for summary in pending:
        message = service.users().messages().get(
            userId="me", id=summary["id"], format="full"
        ).execute()
        payload = message.get("payload", {})
        headers = _header_map(payload)
        try:
            timestamp = int(message.get("internalDate", "0"))
            received_at = (
                datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                if timestamp > 0
                else datetime.now(timezone.utc)
            )
        except (TypeError, ValueError, OSError):
            received_at = datetime.now(timezone.utc)
        def load_attachment(attachment_id: str) -> str:
            result = service.users().messages().attachments().get(
                userId="me", messageId=message["id"], id=attachment_id
            ).execute()
            return result.get("data", "")

        plain, html = _message_bodies(payload, load_attachment)
        if not plain.strip() and not html.strip():
            no_body += 1
        jobs = extract_seek_jobs(html, plain, received_at)
        if jobs:
            jobs_found += len(jobs)
            all_jobs.extend(jobs)
        else:
            no_jobs += 1
        imported_records.append((message, headers, received_at, len(jobs)))

    jobs_added = jobs_existing = 0
    new_job_ids: list[int] = []
    if all_jobs:
        jobs_added, jobs_existing, new_job_ids = _upsert_seek_jobs(connection, all_jobs)
        _remove_seek_alert_ctas(connection)

    for message, headers, received_at, found_count in imported_records:
        imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        connection.execute(
            """INSERT INTO gmail_imported_messages
            (message_id, thread_id, received_at, subject, imported_at, jobs_found, parser_version)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                thread_id = excluded.thread_id,
                received_at = excluded.received_at,
                subject = excluded.subject,
                imported_at = excluded.imported_at,
                jobs_found = excluded.jobs_found,
                parser_version = excluded.parser_version""",
            (
                message["id"],
                message.get("threadId", ""),
                received_at.isoformat(timespec="seconds"),
                headers.get("subject", "")[:500],
                imported_at,
                found_count,
                PARSER_VERSION,
            ),
        )
        connection.commit()
        done += 1
    return GmailSyncSummary(
        len(messages), done, jobs_found, jobs_added, jobs_existing, no_jobs, no_body, new_job_ids
    )


def trash_stale_imported_alerts(connection, stale_after_days: int) -> tuple[int, int]:
    service = gmail_service()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, stale_after_days))).isoformat(
        timespec="seconds"
    )
    rows = connection.execute(
        """SELECT message_id FROM gmail_imported_messages
           WHERE jobs_found > 0 AND trashed_at IS NULL AND received_at < ?""",
        (cutoff,),
    ).fetchall()
    trashed = failed = 0
    for row in rows:
        try:
            service.users().messages().trash(userId="me", id=row["message_id"]).execute()
        except Exception:
            failed += 1
            continue
        connection.execute(
            "UPDATE gmail_imported_messages SET trashed_at = ? WHERE message_id = ?",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), row["message_id"]),
        )
        connection.commit()
        trashed += 1
    return trashed, failed
