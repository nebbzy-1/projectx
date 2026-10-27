from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from .env import get_env, read_env_file, write_env_values
from .profile import normalize_candidate_profile


ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = ROOT / "prompts" / "job_fit_ranker.md"
SCHEMA_PATH = ROOT / "prompts" / "job_fit_result.schema.json"
PROMPT_VERSION = "job-fit-rank-v2"
RUBRIC_VERSION = "legal-role-rubric-v1"
TOP_LIMIT = 30
BATCH_SIZE_DEFAULT = 10
DESCRIPTION_CHAR_LIMIT = 12000
RESUME_CHAR_LIMIT = 8000
API_TIMEOUT_SECONDS = 120

TIERS = {"strong_fit", "possible_fit", "weak_fit", "insufficient_information"}
CONFIDENCE = {"high", "medium", "low"}
EVIDENCE_FIELDS = {"job_excerpt", "candidate_fact", "assessment"}


class AIRankingError(RuntimeError):
    pass


@dataclass
class RankingSummary:
    candidates: int
    assessed: int
    reused: int
    prioritized: int


def load_api_key() -> str:
    return get_env("OPENAI_API_KEY")


def api_key_configured() -> bool:
    return bool(load_api_key())


def api_key_source() -> str:
    value = get_env("OPENAI_API_KEY")
    if not value:
        return ""
    dotenv_value = read_env_file().get("OPENAI_API_KEY", "").strip()
    if dotenv_value and value == dotenv_value:
        return "dotenv"
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return "environment"
    return ""


def save_api_key(value: str) -> None:
    key = str(value or "").strip()
    if not key:
        raise ValueError("Paste an OpenAI API key first.")
    if len(key) > 1024 or any(char.isspace() for char in key):
        raise ValueError("That API key does not look valid. Check for spaces or line breaks.")
    write_env_values({"OPENAI_API_KEY": key})


def profile_hash(profile: dict, locations: list[str]) -> str:
    normalized = normalize_candidate_profile(profile)
    normalized.pop("enabled", None)
    settings = {
        "candidate": normalized,
        "allowed_locations": locations,
        "location_policy": "strict",
    }
    payload = json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _job_payload(row: dict) -> dict:
    description = BeautifulSoup(str(row.get("description") or ""), "html.parser").get_text(" ", strip=True)
    description = unescape(re.sub(r"\s+", " ", description)).strip()
    return {
        "job_id": str(row["id"]),
        "title": str(row.get("title") or "Untitled role")[:300],
        "company": str(row.get("company") or "")[:200],
        "location": str(row.get("location") or "")[:200],
        "description": description[:DESCRIPTION_CHAR_LIMIT] or None,
        "posted_at": str(row.get("date_posted") or "") or None,
    }


def _job_hash(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _candidate_rows(connection) -> list[dict]:
    rows = connection.execute(
        """SELECT id, title, company, location, description, date_posted, status, list_bucket
           FROM jobs WHERE status IN ('new', 'saved') AND list_bucket != 'deleted' ORDER BY id"""
    ).fetchall()
    return [dict(row) for row in rows]


def _valid_assessments(connection, candidates: list[dict], profile_digest: str, model: str) -> tuple[dict[int, dict], list[dict]]:
    rows = connection.execute("SELECT * FROM ai_job_assessments").fetchall()
    saved = {int(row["job_id"]): dict(row) for row in rows}
    valid: dict[int, dict] = {}
    pending: list[dict] = []
    for row in candidates:
        job_id = int(row["id"])
        payload = _job_payload(row)
        digest = _job_hash(payload)
        assessment = saved.get(job_id)
        if (
            assessment
            and assessment["job_hash"] == digest
            and assessment["profile_hash"] == profile_digest
            and assessment["model"] == model
            and assessment["prompt_version"] == PROMPT_VERSION
            and assessment["rubric_version"] == RUBRIC_VERSION
        ):
            valid[job_id] = assessment
        else:
            pending.append({**row, "payload": payload, "job_hash": digest})
    return valid, pending


def _prompt_text() -> str:
    try:
        source = PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as error:
        raise AIRankingError("The AI ranking prompt file is missing.") from error
    match = re.search(r"## Developer prompt\s*```text\s*(.*?)\s*```", source, re.DOTALL)
    if not match:
        raise AIRankingError("The AI ranking prompt could not be loaded.")
    return match.group(1).strip()


def _output_schema() -> dict:
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AIRankingError("The AI response schema could not be loaded.") from error
    # Responses Structured Outputs accepts a JSON Schema subset. The schema
    # metadata is descriptive and is not part of the submitted contract.
    schema.pop("$schema", None)
    schema.pop("title", None)
    return schema


def _response_text(response: dict) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    fragments: list[str] = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "refusal":
                raise AIRankingError("The AI provider declined to assess this batch.")
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                fragments.append(content["text"])
    return "\n".join(fragments)


def _request_batch(
    *, api_key: str, endpoint: str, model: str, developer_prompt: str,
    user_payload: dict, schema: dict,
) -> dict:
    body = {
        "model": model,
        "input": [
            {"role": "developer", "content": developer_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "job_fit_assessment_batch",
                "strict": True,
                "schema": schema,
            }
        },
        "max_output_tokens": 8000,
    }
    request = Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = ""
        try:
            body_data = json.loads(error.read().decode("utf-8"))
            detail = str(body_data.get("error", {}).get("message", ""))
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            pass
        suffix = f": {detail[:400]}" if detail else ""
        raise AIRankingError(f"OpenAI API returned HTTP {error.code}{suffix}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise AIRankingError(f"Could not reach the configured AI endpoint: {error}") from error
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AIRankingError("The AI endpoint returned an unreadable response.") from error

    output_text = _response_text(response_data)
    if not output_text:
        status = str(response_data.get("status", "unknown"))
        raise AIRankingError(f"The AI endpoint returned no assessment text (status: {status}).")
    try:
        parsed = json.loads(output_text)
    except json.JSONDecodeError as error:
        raise AIRankingError("The AI endpoint returned invalid JSON; this batch was not saved.") from error
    if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
        raise AIRankingError("The AI endpoint returned a batch in the wrong format; it was not saved.")
    return parsed


def _validate_results(results: list, expected_ids: set[str]) -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    for result in results:
        if not isinstance(result, dict):
            raise AIRankingError("The AI result contained a malformed job assessment; this batch was not saved.")
        job_id = result.get("job_id")
        if not isinstance(job_id, str) or job_id in by_id:
            raise AIRankingError("The AI result omitted or repeated a job ID; this batch was not saved.")
        score = result.get("score")
        if type(score) is not int or not 0 <= score <= 100:
            raise AIRankingError("The AI result contained a score outside 0–100; this batch was not saved.")
        if result.get("tier") not in TIERS or result.get("confidence") not in CONFIDENCE:
            raise AIRankingError("The AI result used an unknown fit tier or confidence value; this batch was not saved.")
        if type(result.get("hard_blocker")) is not bool:
            raise AIRankingError("The AI result contained an invalid hard-blocker value; this batch was not saved.")
        if not isinstance(result.get("summary"), str):
            raise AIRankingError("The AI result omitted its summary; this batch was not saved.")
        for field in ("strengths", "gaps_or_unknowns", "evidence"):
            value = result.get(field)
            if not isinstance(value, list) or len(value) > 3:
                raise AIRankingError(f"The AI result contained invalid {field}; this batch was not saved.")
        if any(not isinstance(value, str) for value in result["strengths"] + result["gaps_or_unknowns"]):
            raise AIRankingError("The AI result contained invalid explanation text; this batch was not saved.")
        for evidence in result["evidence"]:
            if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_FIELDS:
                raise AIRankingError("The AI result contained invalid evidence; this batch was not saved.")
            if any(not isinstance(evidence[field], str) for field in EVIDENCE_FIELDS):
                raise AIRankingError("The AI result contained invalid evidence text; this batch was not saved.")
        by_id[job_id] = result
    if set(by_id) != expected_ids:
        raise AIRankingError("The AI result did not cover every job in this batch; none of this batch was saved.")
    return by_id


def rank_open_jobs(
    connection,
    *,
    profile: dict,
    locations: list[str],
    model: str,
    endpoint: str,
    batch_size: int = BATCH_SIZE_DEFAULT,
    progress: Callable[[int, int, str], None] | None = None,
) -> RankingSummary:
    api_key = load_api_key()
    if not api_key:
        raise AIRankingError("Save an OpenAI API key in the Prioritized tab before ranking jobs.")
    if not endpoint.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        raise AIRankingError("The configured AI endpoint must use HTTPS (or localhost for a local provider).")

    candidates = _candidate_rows(connection)
    profile_digest = profile_hash(profile, locations)
    valid, pending = _valid_assessments(connection, candidates, profile_digest, model)
    if not pending:
        return RankingSummary(len(candidates), 0, len(valid), min(len(valid), TOP_LIMIT))

    developer_prompt = _prompt_text() + (
        "\n\nAPP BEHAVIOR: This run only assesses and ranks open jobs. "
        "Return an assessment for every supplied job. The app displays the top 30; "
        "it does not move, close, or delete jobs based on this assessment."
    )
    schema = _output_schema()
    normalized_profile = normalize_candidate_profile(profile)
    candidate = {
        "circumstances": normalized_profile["circumstances"],
        "admission_status": None,
        "years_pqe": None,
        "maximum_required_pqe": normalized_profile["max_required_pqe"],
        "practice_interests": [],
        "skills": [],
        "allowed_locations": locations,
        "location_policy": "strict",
        "work_preferences": [],
        "resume_summary": normalized_profile["resume_summary"][:RESUME_CHAR_LIMIT] or None,
    }
    batch_size = max(1, min(int(batch_size), 25))
    total = len(pending)
    completed = 0
    assessed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if progress:
        progress(completed, total, f"Preparing {total} un-applied jobs for AI assessment")

    for offset in range(0, total, batch_size):
        batch = pending[offset:offset + batch_size]
        expected_ids = {str(row["id"]) for row in batch}
        user_payload = {
            "prompt_version": PROMPT_VERSION,
            "candidate": candidate,
            "jobs": [row["payload"] for row in batch],
        }
        response = _request_batch(
            api_key=api_key,
            endpoint=endpoint,
            model=model,
            developer_prompt=developer_prompt,
            user_payload=user_payload,
            schema=schema,
        )
        results = _validate_results(response["results"], expected_ids)
        for row in batch:
            job_id = int(row["id"])
            result = results[str(job_id)]
            connection.execute(
                """INSERT INTO ai_job_assessments
                   (job_id, score, tier, confidence, hard_blocker, summary,
                    strengths_json, gaps_json, evidence_json, model, prompt_version,
                    rubric_version, job_hash, profile_hash, assessed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id) DO UPDATE SET
                    score = excluded.score, tier = excluded.tier,
                    confidence = excluded.confidence, hard_blocker = excluded.hard_blocker,
                    summary = excluded.summary, strengths_json = excluded.strengths_json,
                    gaps_json = excluded.gaps_json, evidence_json = excluded.evidence_json,
                    model = excluded.model, prompt_version = excluded.prompt_version,
                    rubric_version = excluded.rubric_version, job_hash = excluded.job_hash,
                    profile_hash = excluded.profile_hash, assessed_at = excluded.assessed_at""",
                (
                    job_id,
                    result["score"],
                    result["tier"],
                    result["confidence"],
                    int(result["hard_blocker"]),
                    result["summary"][:2000],
                    json.dumps(result["strengths"], ensure_ascii=False),
                    json.dumps(result["gaps_or_unknowns"], ensure_ascii=False),
                    json.dumps(result["evidence"], ensure_ascii=False),
                    model,
                    PROMPT_VERSION,
                    RUBRIC_VERSION,
                    row["job_hash"],
                    profile_digest,
                    assessed_at,
                ),
            )
        connection.commit()
        completed += len(batch)
        if progress:
            progress(completed, total, f"Assessed {completed} of {total} jobs")

    return RankingSummary(
        candidates=len(candidates),
        assessed=completed,
        reused=len(valid),
        prioritized=min(len(candidates), TOP_LIMIT),
    )


def prioritized_view_data(
    connection,
    *,
    profile: dict,
    allowed_locations: list[str],
    model: str,
    location: str = "",
    query: str = "",
) -> dict:
    candidates = _candidate_rows(connection)
    valid, pending = _valid_assessments(
        connection, candidates, profile_hash(profile, allowed_locations), model
    )
    candidate_by_id = {int(row["id"]): row for row in candidates}
    manually_prioritized: list[dict] = []
    ai_prioritized: list[dict] = []
    for job_id, assessment in valid.items():
        row = candidate_by_id[job_id]
        item = dict(row)
        item["manual_priority"] = row.get("list_bucket") == "prioritized"
        item.update(
            ai_score=int(assessment["score"]),
            ai_tier=assessment["tier"],
            ai_confidence=assessment["confidence"],
            ai_hard_blocker=bool(assessment["hard_blocker"]),
            ai_summary=assessment["summary"],
            ai_strengths=json.loads(assessment["strengths_json"] or "[]"),
            ai_gaps=json.loads(assessment["gaps_json"] or "[]"),
            ai_evidence=json.loads(assessment["evidence_json"] or "[]"),
            ai_assessed_at=assessment["assessed_at"],
        )
        (manually_prioritized if item["manual_priority"] else ai_prioritized).append(item)

    score_order = lambda item: (
        item.get("ai_score", -1),
        not item.get("ai_hard_blocker", False),
        str(item.get("date_posted") or ""),
        -int(item["id"]),
    )
    manually_prioritized.extend(
        {
            **row,
            "manual_priority": True,
        }
        for row in candidates
        if row.get("list_bucket") == "prioritized" and int(row["id"]) not in valid
    )
    manually_prioritized.sort(key=score_order, reverse=True)
    ai_prioritized.sort(key=score_order, reverse=True)
    manual_ids = {int(item["id"]) for item in manually_prioritized}
    ranked_count = min(len(manually_prioritized) + len(ai_prioritized), TOP_LIMIT)
    top = manually_prioritized[:TOP_LIMIT]
    if len(top) < TOP_LIMIT:
        top.extend(
            item for item in ai_prioritized
            if int(item["id"]) not in manual_ids
        )
        top = top[:TOP_LIMIT]
    city = location.split(",", 1)[0].strip().casefold() if location else ""
    term = query.casefold().strip()
    if city:
        top = [item for item in top if city in str(item.get("location") or "").casefold()]
    if term:
        top = [
            item for item in top
            if term in " ".join(str(item.get(field) or "") for field in ("title", "company", "location")).casefold()
        ]
    return {
        "jobs": top,
        "total": len(top),
        "ranked_count": ranked_count,
        "ai_ranked_count": min(len(valid), TOP_LIMIT),
        "manual_priority_count": len(manually_prioritized),
        "candidate_count": len(candidates),
        "pending_count": len(pending),
        "assessed_count": len(valid),
    }
