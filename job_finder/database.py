from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


STATUSES = ("new", "saved", "applied", "interviewing", "rejected", "closed")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
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
            notes TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.commit()
    return connection


def job_fingerprint(job: dict) -> str:
    """Prefer the board URL; fall back to normalized employer/title/location."""
    url = str(job.get("job_url") or "").strip().rstrip("/").lower()
    if url:
        return f"url:{url}"
    parts = [job.get("company"), job.get("title"), job.get("location")]
    normalized = "|".join(" ".join(str(part or "").lower().split()) for part in parts)
    return f"text:{normalized}"


def upsert_jobs(connection: sqlite3.Connection, jobs: list[dict]) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_count = 0
    for job in jobs:
        fingerprint = job_fingerprint(job)
        existing = connection.execute(
            "SELECT id FROM jobs WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if existing is None:
            connection.execute(
                """INSERT INTO jobs
                (fingerprint, title, company, location, source, url, description,
                 date_posted, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fingerprint,
                    str(job.get("title") or "Untitled role"),
                    job.get("company"),
                    job.get("location"),
                    job.get("site"),
                    job.get("job_url"),
                    job.get("description"),
                    str(job.get("date_posted") or "") or None,
                    now,
                    now,
                ),
            )
            new_count += 1
        else:
            connection.execute(
                "UPDATE jobs SET last_seen = ?, source = COALESCE(source, ?) WHERE id = ?",
                (now, job.get("site"), existing["id"]),
            )
    connection.commit()
    return new_count, len(jobs) - new_count


def list_jobs(connection: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    if status:
        return connection.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY first_seen DESC", (status,)
        ).fetchall()
    return connection.execute("SELECT * FROM jobs ORDER BY first_seen DESC").fetchall()


def set_status(connection: sqlite3.Connection, job_id: int, status: str) -> bool:
    result = connection.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    connection.commit()
    return result.rowcount > 0


def set_note(connection: sqlite3.Connection, job_id: int, note: str) -> bool:
    result = connection.execute("UPDATE jobs SET notes = ? WHERE id = ?", (note, job_id))
    connection.commit()
    return result.rowcount > 0
