from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


STATUSES = ("new", "saved", "applied", "interviewing", "rejected", "closed")
LATEST_LIMIT = 26


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY,
            fingerprint TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            company TEXT,
            location TEXT,
            source TEXT,
            url TEXT,
            description TEXT,
            date_posted TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            notes TEXT NOT NULL DEFAULT '',
            fit_status TEXT NOT NULL DEFAULT 'review',
            fit_reason TEXT NOT NULL DEFAULT '',
            fit_override INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
    needs_match_key_backfill = "match_key" not in columns
    if needs_match_key_backfill:
        connection.execute("ALTER TABLE jobs ADD COLUMN match_key TEXT")
    if "fit_status" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN fit_status TEXT NOT NULL DEFAULT 'review'")
    if "fit_reason" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN fit_reason TEXT NOT NULL DEFAULT ''")
    if "fit_override" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN fit_override INTEGER NOT NULL DEFAULT 0")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS job_sources (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            url TEXT NOT NULL,
            date_posted TEXT,
            UNIQUE(source, url)
        )
        """
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS app_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS latest_jobs (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
            added_at TEXT NOT NULL,
            is_new INTEGER NOT NULL DEFAULT 0
        )"""
    )
    # Backfill the original single-source records and canonical match keys.
    migration_done = connection.execute(
        "SELECT value FROM app_metadata WHERE key = 'legacy_source_links_migrated'"
    ).fetchone()
    if not migration_done:
        old_jobs = connection.execute(
            "SELECT id, title, company, location, source, url, date_posted FROM jobs"
        ).fetchall()
        for job in old_jobs:
            key = job_match_key(dict(job))
            if key:
                connection.execute(
                    "UPDATE jobs SET match_key = COALESCE(match_key, ?) WHERE id = ?",
                    (key, job["id"]),
                )
            if job["source"] and job["url"]:
                connection.execute(
                    "INSERT OR IGNORE INTO job_sources(job_id, source, url, date_posted) VALUES (?, ?, ?, ?)",
                    (job["id"], job["source"], normalize_url(job["url"]), job["date_posted"]),
                )
        connection.execute(
            "INSERT INTO app_metadata(key, value) VALUES ('legacy_source_links_migrated', 'true')"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_match_key ON jobs(match_key) WHERE match_key IS NOT NULL"
    )
    connection.commit()
    return connection


def job_fingerprint(job: dict) -> str:
    """Use a normalized role identity to merge cross-board copies."""
    if key := job_match_key(job):
        return key
    if url := normalize_url(job.get("job_url")):
        return f"url:{url}"
    parts = (_normalize_text(job.get(field)) for field in ("company", "title", "location"))
    return "text:" + "|".join(parts)


def _normalize_text(value: object) -> str:
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", value))


def normalize_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() != "ref"
    ]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(sorted(query)), "")
    )


def job_match_key(job: dict) -> str:
    title = _normalize_text(job.get("title"))
    company = _normalize_text(job.get("company"))
    location = _normalize_text(job.get("location"))
    if not (title and company and location):
        return ""
    return f"role:{company}|{title}|{location}"


def get_metadata(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM app_metadata WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_metadata(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO app_metadata(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    connection.commit()


def upsert_jobs(connection: sqlite3.Connection, jobs: list[dict]) -> tuple[int, int]:
    new_count, existing_count, _ = upsert_jobs_with_ids(connection, jobs)
    return new_count, existing_count


def _known_job(connection: sqlite3.Connection, job: dict) -> sqlite3.Row | None:
    raw_url = normalize_url(job.get("job_url"))
    source = str(job.get("site") or "unknown")
    if raw_url:
        existing = connection.execute(
            "SELECT job_id AS id FROM job_sources WHERE source = ? AND url = ?",
            (source, raw_url),
        ).fetchone()
        if existing:
            return existing
    match_key = job_match_key(job)
    if match_key:
        existing = connection.execute(
            "SELECT id FROM jobs WHERE match_key = ? ORDER BY id LIMIT 1", (match_key,)
        ).fetchone()
        if existing:
            return existing
    fingerprint = job_fingerprint(job)
    return connection.execute("SELECT id FROM jobs WHERE fingerprint = ?", (fingerprint,)).fetchone()


def find_job_id(connection: sqlite3.Connection, job: dict) -> int | None:
    row = _known_job(connection, job)
    return int(row["id"]) if row else None


def find_unseen_jobs(connection: sqlite3.Connection, jobs: list[dict]) -> list[dict]:
    """Return new, deduplicated candidates without writing them to the database."""
    unseen: list[dict] = []
    candidates: set[str] = set()
    for job in jobs:
        fingerprint = job_fingerprint(job)
        if fingerprint in candidates or _known_job(connection, job):
            continue
        candidates.add(fingerprint)
        unseen.append(job)
    return unseen


def upsert_jobs_with_ids(connection: sqlite3.Connection, jobs: list[dict]) -> tuple[int, int, list[int]]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_count = 0
    new_ids: list[int] = []
    for job in jobs:
        raw_url = normalize_url(job.get("job_url"))
        source = str(job.get("site") or "unknown")
        existing = _known_job(connection, job)
        match_key = job_match_key(job)
        if existing is None:
            fingerprint = job_fingerprint(job)
            connection.execute(
                """INSERT INTO jobs
                (fingerprint, match_key, title, company, location, source, url, description,
                 date_posted, first_seen, last_seen, fit_status, fit_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fingerprint,
                    match_key or None,
                    str(job.get("title") or "Untitled role"),
                    job.get("company"),
                    job.get("location"),
                    source,
                    raw_url or None,
                    job.get("description"),
                    str(job.get("date_posted") or "") or None,
                    now,
                    now,
                    str(job.get("fit_status") or "review"),
                    str(job.get("fit_reason") or ""),
                ),
            )
            job_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            new_count += 1
            new_ids.append(job_id)
        else:
            job_id = existing["id"]
            connection.execute(
                """UPDATE jobs SET last_seen = ?, description = COALESCE(?, description),
                   date_posted = COALESCE(date_posted, ?), match_key = COALESCE(match_key, ?),
                   fit_status = CASE WHEN fit_override = 1 THEN fit_status ELSE ? END,
                   fit_reason = CASE WHEN fit_override = 1 THEN fit_reason ELSE ? END
                   WHERE id = ?""",
                (
                    now,
                    job.get("description"),
                    str(job.get("date_posted") or "") or None,
                    match_key or None,
                    str(job.get("fit_status") or "review"),
                    str(job.get("fit_reason") or ""),
                    job_id,
                ),
            )
        if raw_url:
            connection.execute(
                "INSERT OR IGNORE INTO job_sources(job_id, source, url, date_posted) VALUES (?, ?, ?, ?)",
                (job_id, source, raw_url, str(job.get("date_posted") or "") or None),
            )
    connection.commit()
    return new_count, len(jobs) - new_count, new_ids


def list_jobs(connection: sqlite3.Connection, status: str | None = None, query: str | None = None) -> list[sqlite3.Row]:
    clauses = []
    values: list[str] = []
    if status:
        clauses.append("status = ?")
        values.append(status)
    if query:
        pattern = f"%{query}%"
        clauses.append("(title LIKE ? OR company LIKE ? OR location LIKE ?)")
        values.extend((pattern, pattern, pattern))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return connection.execute(
        f"SELECT * FROM jobs {where} ORDER BY first_seen DESC LIMIT 1000", values
    ).fetchall()


def list_job_sources(connection: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT source, url FROM job_sources WHERE job_id = ? ORDER BY source, url", (job_id,)
    ).fetchall()


def _tab_clauses(
    tab: str,
    location: str | None,
    query: str | None,
) -> tuple[str, list[str], str, str]:
    clauses: list[str] = []
    values: list[str] = []
    if location:
        city = location.split(",", 1)[0].strip()
        clauses.append("j.location LIKE ?")
        values.append(f"%{city}%")
    if query:
        pattern = f"%{query}%"
        clauses.append("(j.title LIKE ? OR j.company LIKE ? OR j.location LIKE ?)")
        values.extend((pattern, pattern, pattern))
    if tab == "backlog":
        clauses.extend(("j.status IN ('new', 'saved')", "COALESCE(j.fit_status, 'review') != 'skip'"))
        from_clause = "jobs j"
        order = "j.first_seen DESC"
    elif tab == "deleted":
        # Deleted is the raw search archive: keep every fetched listing visible
        # here, including rows routed to Backlog or Applied after filtering.
        from_clause = "jobs j"
        order = "j.first_seen DESC"
    elif tab == "applied":
        clauses.append("j.status IN ('applied', 'interviewing', 'rejected', 'closed')")
        from_clause = "jobs j"
        order = "j.last_seen DESC"
    else:
        from_clause = "latest_jobs l JOIN jobs j ON j.id = l.job_id"
        order = "l.added_at DESC, l.id DESC"
    if tab == "latest":
        clauses.append("COALESCE(j.fit_status, 'review') != 'skip'")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, values, from_clause, order


def _tab_query(
    connection: sqlite3.Connection,
    tab: str,
    location: str | None,
    query: str | None,
    limit: int,
    offset: int,
) -> list[sqlite3.Row]:
    where, values, from_clause, order = _tab_clauses(tab, location, query)
    return connection.execute(
        f"SELECT j.id, j.title, j.company, j.location, j.source, j.url, j.description, j.date_posted, "
        f"j.first_seen, j.last_seen, j.status, j.notes, j.fit_status, j.fit_reason, j.fit_override, "
        f"{'l.is_new' if tab == 'latest' else '0'} AS latest_new "
        f"FROM {from_clause} {where} ORDER BY {order} LIMIT ? OFFSET ?",
        [*values, limit, offset],
    ).fetchall()


def list_tab_jobs(
    connection: sqlite3.Connection,
    tab: str,
    location: str | None = None,
    query: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    if tab not in {"applied", "backlog", "latest", "deleted"}:
        raise ValueError(f"Unknown job tab: {tab}")
    return _tab_query(
        connection,
        tab,
        location,
        query,
        min(max(int(limit), 1), 200),
        max(int(offset), 0),
    )


def count_tab_jobs(
    connection: sqlite3.Connection,
    tab: str,
    location: str | None = None,
    query: str | None = None,
) -> int:
    if tab not in {"applied", "backlog", "latest", "deleted"}:
        raise ValueError(f"Unknown job tab: {tab}")
    where, values, from_clause, _ = _tab_clauses(tab, location, query)
    return int(
        connection.execute(f"SELECT COUNT(*) FROM {from_clause} {where}", values).fetchone()[0]
    )


def latest_job_count(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT COUNT(*) FROM latest_jobs").fetchone()[0])


def save_latest_jobs(connection: sqlite3.Connection, job_ids: list[int], refreshed_at: str) -> None:
    connection.execute("UPDATE latest_jobs SET is_new = 0")
    for job_id in job_ids:
        connection.execute(
            "INSERT INTO latest_jobs(job_id, added_at, is_new) VALUES (?, ?, 1) "
            "ON CONFLICT(job_id) DO UPDATE SET added_at = excluded.added_at, is_new = 1",
            (job_id, refreshed_at),
        )
    connection.execute(
        "DELETE FROM latest_jobs WHERE id NOT IN "
        "(SELECT id FROM latest_jobs ORDER BY added_at DESC, id DESC LIMIT ?)",
        (LATEST_LIMIT,),
    )
    connection.execute(
        "INSERT INTO app_metadata(key, value) VALUES ('last_latest_refresh_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (refreshed_at,),
    )
    connection.commit()


def set_status(connection: sqlite3.Connection, job_id: int, status: str) -> bool:
    result = connection.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    connection.commit()
    return result.rowcount > 0


def set_note(connection: sqlite3.Connection, job_id: int, note: str) -> bool:
    result = connection.execute("UPDATE jobs SET notes = ? WHERE id = ?", (note, job_id))
    connection.commit()
    return result.rowcount > 0


def restore_job_to_backlog(connection: sqlite3.Connection, job_id: int) -> bool:
    result = connection.execute(
        "UPDATE jobs SET fit_status = 'review', fit_reason = 'Manually restored from Deleted.', fit_override = 1 "
        "WHERE id = ? AND fit_status = 'skip' AND status IN ('new', 'saved')",
        (job_id,),
    )
    connection.commit()
    return result.rowcount > 0
