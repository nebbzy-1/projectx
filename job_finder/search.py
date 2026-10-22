from __future__ import annotations

from jobspy import scrape_jobs


def search_jobs(config: dict) -> list[dict]:
    """Search configured boards via JobSpy and return plain dictionaries."""
    options = {
        "site_name": config["sites"],
        "search_term": config["query"],
        "location": config["location"],
        "results_wanted": int(config.get("results_per_site", 25)),
        "country_indeed": config.get("country_indeed", "Australia"),
        "hours_old": int(config.get("hours_old", 168)),
        "verbose": 1,
    }
    results = scrape_jobs(**options)
    if results is None or results.empty:
        return []
    return results.where(results.notna(), None).to_dict(orient="records")
