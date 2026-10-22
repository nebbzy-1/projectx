from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
DATABASE_PATH = ROOT / "data" / "jobs.sqlite3"


def load_search_config() -> dict:
    with CONFIG_PATH.open("rb") as config_file:
        config = tomllib.load(config_file)

    search = config.get("search", {})
    required = ("query", "location", "sites")
    missing = [key for key in required if not search.get(key)]
    if missing:
        raise ValueError(f"Missing required [search] settings: {', '.join(missing)}")
    return search
