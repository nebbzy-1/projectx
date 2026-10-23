from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import sqlite3
from typing import Callable

from .database import (
    find_unseen_jobs,
    get_metadata,
    save_latest_jobs,
    set_metadata,
    upsert_jobs,
    upsert_jobs_with_ids,
)
from .search import search_jobs


INITIAL_IMPORT_KEY = "initial_import_completed_at"
LATEST_REFRESH_KEY = "last_latest_refresh_at"


@dataclass
class RunSummary:
    found: int
    added: int
    existing: int
    errors: list[str]
    initial_import: bool


@dataclass
class LatestPreview:
    jobs: list[dict]
    errors: list[str]
    hours_old: int


def _initial_quotas(total: int, sites: list[str]) -> dict[str, int]:
    if not sites:
        raise ValueError("Configure at least one search site")
    total = max(total, len(sites))
    base, remainder = divmod(total, len(sites))
    return {site: base + (1 if index < remainder else 0) for index, site in enumerate(sites)}


def run_initial_import(
    connection: sqlite3.Connection,
    config: dict,
    progress: Callable[[int, int, str], None] | None = None,
) -> RunSummary:
    quotas = _initial_quotas(config["initial_results_per_title_location"], config["sites"])
    found = added = existing = 0
    errors: list[str] = []
    total = len(config["titles"]) * len(config["locations"]) * len(config["sites"])
    completed = 0
    for title in config["titles"]:
        for location in config["locations"]:
            for site, quota in quotas.items():
                try:
                    batch = search_jobs(config, title, location, quota, hours_old=None, sites=[site])
                    new_count, existing_count = upsert_jobs(connection, batch.jobs)
                    found += len(batch.jobs)
                    added += new_count
                    existing += existing_count
                    errors.extend(f"{site} / {title} / {location}: {message}" for message in batch.errors.values())
                except Exception as error:
                    errors.append(f"{site} / {title} / {location}: {error}")
                completed += 1
                if progress:
                    progress(completed, total, f"{title} · {location} · {site}")

    if not errors:
        set_metadata(connection, INITIAL_IMPORT_KEY, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return RunSummary(found, added, existing, errors, True)


def run_daily_search(connection: sqlite3.Connection, config: dict) -> RunSummary:
    found = added = existing = 0
    errors: list[str] = []
    for title in config["titles"]:
        for location in config["locations"]:
            for site in config["sites"]:
                try:
                    batch = search_jobs(
                        config,
                        title,
                        location,
                        per_site=int(config["results_per_site"][site]),
                        hours_old=int(config.get("hours_old", 24)),
                        sites=[site],
                    )
                    new_count, existing_count = upsert_jobs(connection, batch.jobs)
                    found += len(batch.jobs)
                    added += new_count
                    existing += existing_count
                    errors.extend(
                        f"{title} / {location} / {site}: {message}"
                        for message in batch.errors.values()
                    )
                except Exception as error:
                    errors.append(f"{title} / {location} / {site}: {error}")
    return RunSummary(found, added, existing, errors, False)


def run_search(connection: sqlite3.Connection, config: dict) -> RunSummary:
    if config.get("initial_import_enabled") and not get_metadata(connection, INITIAL_IMPORT_KEY):
        return run_initial_import(connection, config)
    return run_daily_search(connection, config)


def _hours_since(value: str | None, now: datetime) -> int:
    if not value:
        return int(24)
    try:
        previous = datetime.fromisoformat(value)
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
    except ValueError:
        return 24
    return max(1, math.ceil((now - previous.astimezone(timezone.utc)).total_seconds() / 3600))


def preview_latest_refresh(connection: sqlite3.Connection, config: dict) -> LatestPreview:
    now = datetime.now(timezone.utc)
    baseline = get_metadata(connection, LATEST_REFRESH_KEY) or get_metadata(connection, INITIAL_IMPORT_KEY)
    hours_old = _hours_since(baseline, now)
    candidates: list[dict] = []
    errors: list[str] = []
    for title in config["titles"]:
        for location in config["locations"]:
            for site in config["sites"]:
                try:
                    batch = search_jobs(
                        config,
                        title,
                        location,
                        per_site=int(config["results_per_site"][site]),
                        hours_old=hours_old,
                        sites=[site],
                    )
                    candidates.extend(batch.jobs)
                    errors.extend(
                        f"{site} / {title} / {location}: {message}"
                        for message in batch.errors.values()
                    )
                except Exception as error:
                    errors.append(f"{site} / {title} / {location}: {error}")
    unseen = find_unseen_jobs(connection, candidates) if not errors else []
    return LatestPreview(unseen, errors, hours_old)


def commit_latest_refresh(connection: sqlite3.Connection, jobs: list[dict]) -> int:
    _, _, job_ids = upsert_jobs_with_ids(connection, jobs)
    refreshed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_latest_jobs(connection, job_ids, refreshed_at)
    return len(job_ids)
