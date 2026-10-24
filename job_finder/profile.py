from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = PROJECT_ROOT / "data" / "smart_profile.json"
DEFAULT_CANDIDATE_PROFILE = {
    "enabled": True,
    "circumstances": (
        "Recently admitted graduate lawyer in Australia; PLT completed. "
        "Looking for graduate or junior lawyer roles."
    ),
    "max_required_pqe": 1.0,
}


def normalize_candidate_profile(values: dict[str, Any] | None) -> dict[str, Any]:
    values = values or {}
    if not isinstance(values, dict):
        raise ValueError("Smart filter profile must be an object.")
    try:
        max_required_pqe = float(values.get("max_required_pqe", DEFAULT_CANDIDATE_PROFILE["max_required_pqe"]))
    except (TypeError, ValueError) as error:
        raise ValueError("Maximum required PQE must be a number between 0 and 30.") from error
    if not 0 <= max_required_pqe <= 30:
        raise ValueError("Maximum required PQE must be between 0 and 30.")
    circumstances = str(values.get("circumstances", DEFAULT_CANDIDATE_PROFILE["circumstances"])).strip()
    if len(circumstances) > 4000:
        raise ValueError("Keep the circumstances description under 4,000 characters.")
    return {
        "enabled": bool(values.get("enabled", DEFAULT_CANDIDATE_PROFILE["enabled"])),
        "circumstances": circumstances,
        "max_required_pqe": max_required_pqe,
    }


def load_candidate_profile(path: Path = PROFILE_PATH) -> dict[str, Any]:
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return dict(DEFAULT_CANDIDATE_PROFILE)
    try:
        return normalize_candidate_profile(values)
    except (TypeError, ValueError):
        return dict(DEFAULT_CANDIDATE_PROFILE)


def save_candidate_profile(values: dict[str, Any], path: Path = PROFILE_PATH) -> dict[str, Any]:
    profile = normalize_candidate_profile(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary_path, path)
    return profile
