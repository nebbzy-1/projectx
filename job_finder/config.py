from __future__ import annotations

import json
import os

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib
from pathlib import Path

from .profile import load_candidate_profile

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
DATABASE_PATH = ROOT / "data" / "jobs.sqlite3"
WEB_SETTINGS_PATH = ROOT / "data" / "search_settings.json"


def save_web_search_settings(settings: dict) -> None:
    """Persist editable title/location lists without rewriting config.toml."""
    sections = {"search": {"titles": settings["titles"], "locations": settings["locations"]}}
    WEB_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = WEB_SETTINGS_PATH.with_suffix(WEB_SETTINGS_PATH.suffix + ".tmp")
    temporary_path.write_text(json.dumps(sections, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, WEB_SETTINGS_PATH)


def load_search_config() -> dict:
    with CONFIG_PATH.open("rb") as config_file:
        config = tomllib.load(config_file)

    try:
        saved_settings = json.loads(WEB_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        saved_settings = {}
    if isinstance(saved_settings, dict):
        search_overrides = saved_settings.get("search", {})
        if isinstance(search_overrides, dict):
            for key in ("titles", "locations"):
                if key in search_overrides:
                    config.setdefault("search", {})[key] = search_overrides[key]

    search = config.get("search", {})
    required = ("titles", "locations", "sites")
    missing = [key for key in required if not search.get(key)]
    if missing:
        raise ValueError(f"Missing required [search] settings: {', '.join(missing)}")
    for key in ("titles", "locations", "sites"):
        if not isinstance(search[key], list) or not search[key]:
            raise ValueError(f"[search].{key} must be a non-empty list")
    limit_sites = list(dict.fromkeys([*search["sites"], "indeed", "glassdoor"]))
    limits = search.get("results_per_site", 25)
    if isinstance(limits, dict):
        search["results_per_site"] = {
            site: max(1, int(limits.get(site, 25))) for site in limit_sites
        }
    else:
        search["results_per_site"] = {site: max(1, int(limits)) for site in limit_sites}
    search["check_results_total"] = max(
        1000, min(int(search.get("check_results_total", 1500)), 2000)
    )
    initial = config.get("initial_import", {})
    search["initial_import_enabled"] = bool(initial.get("enabled", False))
    search["initial_results_per_title_location"] = int(
        initial.get("results_per_title_location", 250)
    )
    gmail = config.get("gmail", {})
    search["gmail_query"] = str(gmail.get("query", "label:SEEK")).strip() or "label:SEEK"
    search["gmail_max_messages"] = max(1, min(int(gmail.get("max_messages", 250)), 1000))
    search["gmail_stale_after_days"] = max(1, int(gmail.get("stale_after_days", 90)))
    ai = config.get("ai", {})
    search["ai_model"] = str(ai.get("model", "gpt-6-luna")).strip() or "gpt-6-luna"
    search["candidate_profile"] = load_candidate_profile()
    return search
