from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .database import list_job_sources


UNAVAILABLE_MARKERS = (
    "this job is no longer available",
    "this job is no longer accepting applications",
    "job is no longer available",
    "job has expired",
    "this job has expired",
    "no longer accepting applications",
)
ACTIVE_MARKERS = ("apply now", "apply on employer site", "easily apply", "easy apply")


def _check_source(source: str, url: str) -> bool | None:
    """Return True if the board confirms the listing is live, False if closed, else None."""
    board = source.casefold()
    if board not in {"indeed", "glassdoor"}:
        return None
    try:
        hostname = (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return None
    allowed_domains = {"indeed.com", "glassdoor.com"}
    if not any(hostname == domain or hostname.endswith(f".{domain}") for domain in allowed_domains):
        return None
    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; JobFinder/1.0; listing status check)"},
    )
    try:
        with urlopen(request, timeout=8) as response:
            body = response.read(750_000).decode("utf-8", errors="ignore").casefold()
            final_url = response.geturl().casefold()
            if any(marker in body for marker in UNAVAILABLE_MARKERS):
                return False
            if (
                response.status == 200
                and final_url.startswith(("https://", "http://"))
                and any(marker in body for marker in ACTIVE_MARKERS)
            ):
                return True
    except HTTPError as error:
        if error.code in {404, 410}:
            return False
    except (URLError, TimeoutError, OSError, ValueError):
        pass
    return None


def _job_availability(sources: list[tuple[str, str]]) -> bool | None:
    if not sources:
        return None
    checks = [_check_source(source, url) for source, url in sources]
    if any(result is True for result in checks):
        return True
    if all(result is False for result in checks):
        return False
    return None


def remove_confirmed_expired(connection) -> tuple[int, int]:
    """Check every saved job's source URLs; keep anything not clearly expired."""
    rows = connection.execute("SELECT id FROM jobs").fetchall()
    candidates = [int(row["id"]) for row in rows]
    if not candidates:
        return 0, 0

    # Probe concurrently so a large archive does not spend several seconds per URL.
    candidate_sources = {
        job_id: [(row["source"], row["url"]) for row in list_job_sources(connection, job_id)]
        for job_id in candidates
    }
    with ThreadPoolExecutor(max_workers=12) as pool:
        availability = list(pool.map(_job_availability, candidate_sources.values()))
    results = list(zip(candidate_sources, availability))
    expired_ids = [job_id for job_id, available in results if available is False]
    uncertain_count = sum(available is None for _, available in results)
    if expired_ids:
        placeholders = ",".join("?" for _ in expired_ids)
        connection.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", expired_ids)
        connection.commit()
    return len(expired_ids), uncertain_count
