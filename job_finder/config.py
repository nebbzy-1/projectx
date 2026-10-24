from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib
from pathlib import Path

from .profile import load_candidate_profile

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
DATABASE_PATH = ROOT / "data" / "jobs.sqlite3"


def load_search_config() -> dict:
    with CONFIG_PATH.open("rb") as config_file:
        config = tomllib.load(config_file)

    search = config.get("search", {})
    required = ("titles", "locations", "sites")
    missing = [key for key in required if not search.get(key)]
    if missing:
        raise ValueError(f"Missing required [search] settings: {', '.join(missing)}")
    for key in ("titles", "locations", "sites"):
        if not isinstance(search[key], list) or not search[key]:
            raise ValueError(f"[search].{key} must be a non-empty list")
    limits = search.get("results_per_site", 25)
    if isinstance(limits, dict):
        search["results_per_site"] = {
            site: max(1, int(limits.get(site, 25))) for site in search["sites"]
        }
    else:
        search["results_per_site"] = {site: max(1, int(limits)) for site in search["sites"]}
    initial = config.get("initial_import", {})
    search["initial_import_enabled"] = bool(initial.get("enabled", False))
    search["initial_results_per_title_location"] = int(
        initial.get("results_per_title_location", 250)
    )
    gmail = config.get("gmail", {})
    search["gmail_query"] = str(gmail.get("query", "label:SEEK")).strip() or "label:SEEK"
    search["gmail_max_messages"] = max(1, min(int(gmail.get("max_messages", 250)), 1000))
    search["gmail_stale_after_days"] = max(1, int(gmail.get("stale_after_days", 90)))
    search["candidate_profile"] = load_candidate_profile()
    return search
