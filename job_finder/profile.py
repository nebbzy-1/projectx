from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = PROJECT_ROOT / "data" / "candidate_profile.json"
DEFAULT_CANDIDATE_PROFILE = {
    "circumstances": (
        "Recently admitted graduate lawyer in Australia; PLT completed. "
        "Looking for graduate or junior lawyer roles."
    ),
    "max_required_pqe": 1.0,
    "resume_summary": "",
}


def normalize_candidate_profile(values: dict[str, Any] | None) -> dict[str, Any]:
    values = values or {}
    if not isinstance(values, dict):
        raise ValueError("AI ranking profile must be an object.")
    try:
        max_required_pqe = float(values.get("max_required_pqe", DEFAULT_CANDIDATE_PROFILE["max_required_pqe"]))
    except (TypeError, ValueError) as error:
        raise ValueError("Maximum required PQE must be a number between 0 and 30.") from error
    if not 0 <= max_required_pqe <= 30:
        raise ValueError("Maximum required PQE must be between 0 and 30.")
    circumstances = str(values.get("circumstances", DEFAULT_CANDIDATE_PROFILE["circumstances"])).strip()
    if len(circumstances) > 4000:
        raise ValueError("Keep the circumstances description under 4,000 characters.")
    resume_summary = str(values.get("resume_summary", "")).strip()
    if len(resume_summary) > 8000:
        raise ValueError("Keep the optional resume summary under 8,000 characters.")
    return {
        "circumstances": circumstances,
        "max_required_pqe": max_required_pqe,
        "resume_summary": resume_summary,
    }


def load_candidate_profile(path: Path | None = None) -> dict[str, Any]:
    path = path or PROFILE_PATH
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
