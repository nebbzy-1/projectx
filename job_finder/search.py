from __future__ import annotations

from dataclasses import dataclass

from jobspy import scrape_jobs


@dataclass
class SearchBatch:
    jobs: list[dict]
    errors: dict


def search_jobs(
    config: dict,
    title: str,
    location: str,
    per_site: int,
    hours_old: int | None,
    sites: list[str] | None = None,
) -> SearchBatch:
    """Search each configured source for one title/location pair."""
    options = {
        "site_name": sites or config["sites"],
        "search_term": title,
        "location": location,
        "results_wanted": per_site,
        "country_indeed": config.get("country_indeed", "Australia"),
        "verbose": 1,
    }
    if hours_old is not None:
        options["hours_old"] = hours_old
    results = scrape_jobs(**options)
    errors = dict(getattr(results, "attrs", {}).get("errors", {}) or {})
    if results is None or results.empty:
        return SearchBatch([], errors)
    rows = results.where(results.notna(), None).to_dict(orient="records")
    return SearchBatch(rows, errors)
