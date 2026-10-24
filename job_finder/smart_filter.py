from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any

from .database import get_metadata, set_metadata
from .profile import DEFAULT_CANDIDATE_PROFILE, normalize_candidate_profile


PROFILE_HASH_KEY = "smart_filter_profile_hash"
RULESET_VERSION = "1"

_NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_NUMBER = r"(?:\d+(?:\.\d+)?|" + "|".join(_NUMBER_WORDS) + r")"
_PQE_TERMS = (
    r"(?:p\.?\s*q\.?\s*e\.?|post[- ]?qualif(?:ication|ied|ying)?|"
    r"post[- ]?admission|after\s+(?:(?:being\s+)?admitted|admission)|after\s+qualifying)"
)
_PQE_PATTERNS = (
    re.compile(
        rf"(?P<lead>at\s+least|minimum(?:\s+of)?|more\s+than)?\s*"
        rf"(?P<low>{_NUMBER})(?:\s*[-–]\s*(?P<high>{_NUMBER}))?\s*"
        rf"(?:\+|or\s+more)?\s*(?:years?|yrs?)\s*(?:['’])?\s*(?:of\s+)?{_PQE_TERMS}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<lead>at\s+least|minimum(?:\s+of)?|more\s+than)?\s*"
        rf"(?P<low>{_NUMBER})\s*\+\s*(?:years?\s*)?{_PQE_TERMS}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<lead>at\s+least|minimum(?:\s+of)?|more\s+than)?\s*"
        rf"(?P<low>{_NUMBER})(?:\s*[-–]\s*(?P<high>{_NUMBER}))?\s*"
        rf"(?:years?\s*)?{_PQE_TERMS}",
        re.IGNORECASE,
    ),
)
_SENIOR_TITLE = re.compile(
    r"\b(?:senior\s+associate|senior\s+(?:lawyer|solicitor|counsel)|special\s+counsel|"
    r"legal\s+director|head\s+of\s+legal|general\s+counsel|principal\s+(?:lawyer|solicitor)|"
    r"managing\s+lawyer|partner)\b",
    re.IGNORECASE,
)
_JUNIOR_TITLE_CUE = re.compile(
    r"\b(?:graduate|junior\s+(?:lawyer|solicitor|associate)|newly\s+admitted|"
    r"recently\s+admitted|early[- ]career|entry[- ]level)\b",
    re.IGNORECASE,
)
_JUNIOR_DESCRIPTION_CUE = re.compile(
    r"\b(?:graduate\s+(?:lawyer|program|role|position)|junior\s+(?:lawyer|solicitor)\s+(?:role|position)|"
    r"newly\s+admitted|recently\s+admitted|early[- ]career|entry[- ]level)\b",
    re.IGNORECASE,
)
_OPTIONAL_CUE = re.compile(
    r"\b(?:preferred|preferably|ideally|desirable|advantageous|beneficial|nice\s+to\s+have|up\s+to|at\s+most)\b",
    re.IGNORECASE,
)
_PRACTICE_AREAS = (
    "project finance",
    "projects",
    "finance",
    "transactional",
    "corporate",
    "commercial",
    "property",
    "employment",
    "litigation",
    "family law",
    "insurance",
    "construction",
    "planning",
    "intellectual property",
    "disputes",
    "criminal",
)


def _number(value: str) -> float:
    normalized = value.casefold()
    return float(_NUMBER_WORDS[normalized]) if normalized in _NUMBER_WORDS else float(normalized)


def _required_pqe(description: str) -> list[tuple[float, bool]]:
    requirements: list[tuple[float, bool]] = []
    for pattern in _PQE_PATTERNS:
        for match in pattern.finditer(description):
            minimum = _number(match.group("low"))
            context = description[max(0, match.start() - 60):match.end() + 60]
            optional = bool(_OPTIONAL_CUE.search(context))
            requirements.append((minimum, optional))
    return requirements


def classify_job(job: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, str]:
    profile = normalize_candidate_profile(profile or DEFAULT_CANDIDATE_PROFILE)
    if not profile["enabled"]:
        return {"fit_status": "review", "fit_reason": "Smart filter is turned off."}

    title = str(job.get("title") or "")
    description = str(job.get("description") or "")
    circumstances = str(profile["circumstances"] or "")
    combined = f"{title}\n{description}"

    if _SENIOR_TITLE.search(title):
        return {"fit_status": "skip", "fit_reason": f"The title is a senior role ({title.strip()})."}

    pqe_limit = float(profile["max_required_pqe"])
    too_senior = [years for years, optional in _required_pqe(description) if years > pqe_limit and not optional]
    if too_senior:
        years = min(too_senior)
        years_label = str(int(years)) if years.is_integer() else str(years)
        limit_label = str(int(pqe_limit)) if pqe_limit.is_integer() else str(pqe_limit)
        return {
            "fit_status": "skip",
            "fit_reason": f"Requires at least {years_label} years PQE; your profile allows up to {limit_label}.",
        }

    if not description.strip():
        return {"fit_status": "review", "fit_reason": "No job description was available to check seniority."}

    profile_areas = {area for area in _PRACTICE_AREAS if area in circumstances.casefold()}
    job_areas = {area for area in _PRACTICE_AREAS if area in description.casefold()}
    if profile_areas and job_areas and profile_areas.isdisjoint(job_areas):
        job_area = ", ".join(sorted(job_areas))
        return {
            "fit_status": "review",
            "fit_reason": f"The description focuses on {job_area}; your profile lists different practice interests.",
        }

    if _JUNIOR_TITLE_CUE.search(title) or _JUNIOR_DESCRIPTION_CUE.search(description):
        return {
            "fit_status": "recommended",
            "fit_reason": "The title or description signals graduate or junior seniority; review the other requirements.",
        }

    if any(years <= pqe_limit for years, optional in _required_pqe(description) if not optional):
        return {
            "fit_status": "recommended",
            "fit_reason": "The stated PQE requirement is within your profile limit; review practice-area and skill requirements.",
        }

    if _OPTIONAL_CUE.search(description) and _required_pqe(description):
        return {
            "fit_status": "review",
            "fit_reason": "The PQE language appears to describe a preference, so check the full requirements before deciding.",
        }

    profile_admitted = bool(re.search(r"\b(admitted|admission|solicitor)\b", circumstances, re.IGNORECASE))
    job_requires_admission = bool(re.search(r"\b(?:must be|currently|already)\s+admitted\b", description, re.IGNORECASE))
    if job_requires_admission and not profile_admitted:
        return {
            "fit_status": "review",
            "fit_reason": "The role requires admission; your profile does not say whether you are admitted.",
        }

    return {
        "fit_status": "review",
        "fit_reason": "No clear seniority mismatch found; review the required experience and skills against your profile.",
    }


def profile_fingerprint(profile: dict[str, Any]) -> str:
    normalized = normalize_candidate_profile(profile)
    material = json.dumps({"ruleset": RULESET_VERSION, "profile": normalized}, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def reclassify_jobs(
    connection: sqlite3.Connection,
    profile: dict[str, Any],
    *,
    reset_overrides: bool = False,
) -> int:
    profile = normalize_candidate_profile(profile)
    jobs = connection.execute("SELECT id, title, description, fit_override FROM jobs").fetchall()
    changed = 0
    for job in jobs:
        if job["fit_override"] and not reset_overrides:
            continue
        assessment = classify_job(dict(job), profile)
        connection.execute(
            "UPDATE jobs SET fit_status = ?, fit_reason = ?, fit_override = 0 WHERE id = ?",
            (assessment["fit_status"], assessment["fit_reason"], job["id"]),
        )
        changed += 1
    connection.execute(
        "DELETE FROM latest_jobs WHERE job_id IN (SELECT id FROM jobs WHERE fit_status = 'skip')"
    )
    connection.commit()
    set_metadata(connection, PROFILE_HASH_KEY, profile_fingerprint(profile))
    return changed


def ensure_jobs_classified(connection: sqlite3.Connection, profile: dict[str, Any]) -> bool:
    if get_metadata(connection, PROFILE_HASH_KEY) == profile_fingerprint(profile):
        return False
    reclassify_jobs(connection, profile, reset_overrides=True)
    return True
