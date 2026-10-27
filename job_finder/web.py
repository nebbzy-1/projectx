from __future__ import annotations

import secrets
import threading
from datetime import datetime, timezone
from typing import Any
import webbrowser
from html.parser import HTMLParser

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

from .config import DATABASE_PATH, load_search_config
from .database import (
    APPLICATION_STATUSES,
    JOB_BUCKETS,
    JOB_PLACEMENTS,
    LATEST_LIMIT,
    connect,
    count_tab_jobs,
    clear_job_database,
    delete_deleted_jobs,
    get_metadata,
    latest_job_count,
    list_job_sources,
    list_tab_jobs,
    save_latest_jobs,
    set_job_placement,
    set_note,
)
from .runner import (
    INITIAL_IMPORT_KEY,
    LatestPreview,
    commit_latest_refresh,
    preview_latest_refresh,
    run_initial_import,
    run_listing_search,
)
from .availability import remove_confirmed_expired
from .ai_ranker import (
    AIRankingError,
    api_key_configured,
    api_key_source,
    prioritized_view_data,
    rank_open_jobs,
    save_api_key,
)
from .gmail_import import (
    GmailSetupError,
    authorize_gmail,
    gmail_connected,
    sync_seek_alerts,
    trash_stale_imported_alerts,
)
from .profile import load_candidate_profile, normalize_candidate_profile, save_candidate_profile
from .scheduler import remove_schedule


class _DescriptionParser(HTMLParser):
    BLOCK_TAGS = {"br", "div", "li", "ol", "p", "section", "ul"}
    HIDDEN_TAGS = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() in self.HIDDEN_TAGS:
            self.hidden_depth += 1
            return
        if self.hidden_depth:
            return
        if tag.casefold() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self.HIDDEN_TAGS and self.hidden_depth:
            self.hidden_depth -= 1
            return
        if self.hidden_depth:
            return
        if tag.casefold() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def _plain_description(value: object) -> str:
    parser = _DescriptionParser()
    parser.feed(str(value or ""))
    parser.close()
    lines = [line.strip() for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)


class SearchController:
    """Runs long board searches away from Flask's request thread."""

    ACTIVE_TASKS = {
        "initial_import", "partial_reset", "full_reset", "listing_check",
        "gmail_cleanup", "ai_ranking", "latest_search",
        "confirmation", "committing",
    }

    def __init__(self, config: dict, database_path=DATABASE_PATH):
        self.config = config
        self.config.setdefault("candidate_profile", load_candidate_profile())
        self.database_path = database_path
        self._lock = threading.RLock()
        self._preview: LatestPreview | None = None
        self._preview_token: str | None = None
        connection = connect(self.database_path)
        try:
            self._seeded = get_metadata(connection, INITIAL_IMPORT_KEY) is not None
        finally:
            connection.close()
        self._state: dict[str, Any] = {
            "task": "idle",
            "message": "Your saved jobs are ready.",
            "error": False,
            "progress": None,
            "event_id": 0,
            "preview_token": None,
            "preview_count": 0,
            "total_after": 0,
            "requires_confirmation": False,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            state["seeded"] = self._seeded
            state["busy"] = state["task"] in self.ACTIVE_TASKS
            state["can_retry_seed"] = (
                self.config["initial_import_enabled"]
                and not self._seeded
                and not state["busy"]
            )
            return state

    def dismiss_completed_status(self) -> None:
        """Clear a completed summary after the user switches job lists."""
        with self._lock:
            if self._state["task"] != "done":
                return
            self._state.update(
                task="idle",
                message="Your saved jobs are ready.",
                error=False,
                progress=None,
                details=[],
                preview_token=None,
                requires_confirmation=False,
                event_id=self._state["event_id"] + 1,
            )

    def start_initial_import(self) -> bool:
        if not self.config["initial_import_enabled"]:
            return False
        with self._lock:
            if self._state["task"] in self.ACTIVE_TASKS or self._seeded:
                return False
            self._state.update(
                task="initial_import",
                message="Starting the one-time initial import…",
                error=False,
                progress={"done": 0, "total": 0, "label": "Preparing searches"},
                details=[],
                event_id=self._state["event_id"] + 1,
            )
        threading.Thread(target=self._run_initial_import, daemon=True).start()
        return True

    def start_partial_reset(self) -> bool:
        with self._lock:
            if self._state["task"] in self.ACTIVE_TASKS:
                return False
            self._state.update(
                task="partial_reset",
                message="Refreshing the full job pool and checking listings that were not returned…",
                error=False,
                progress={"done": 0, "total": 0, "label": "Preparing searches"},
                details=[],
                event_id=self._state["event_id"] + 1,
            )
        threading.Thread(target=self._run_partial_reset, daemon=True).start()
        return True

    def _run_partial_reset(self) -> None:
        connection = connect(self.database_path)
        try:
            summary = run_initial_import(connection, self.config, progress=self._update_progress)
            removed = uncertain = 0
            if not summary.errors:
                removed, uncertain = remove_confirmed_expired(connection)
            self._seeded = not summary.errors
            backlog_count = count_tab_jobs(connection, "backlog")
            message = (
                f"Full pool refresh complete: {summary.added} new, {summary.existing} already saved; "
                f"{removed} confirmed expired listings removed. {backlog_count} jobs in Backlog."
            )
            if summary.errors:
                message = "Partial refresh finished with search errors; no expiry checks were used to delete jobs."
            elif uncertain:
                message += f" {uncertain} listings could not be confirmed, so they were kept."
            with self._lock:
                self._state.update(
                    task="error" if summary.errors else "done",
                    message=message,
                    error=bool(summary.errors),
                    progress=None,
                    details=summary.errors[:8],
                    event_id=self._state["event_id"] + 1,
                )
        except Exception as error:
            self._finish_error(f"Partial reset failed: {error}")
        finally:
            connection.close()

    def start_full_reset(self) -> bool:
        with self._lock:
            if self._state["task"] in self.ACTIVE_TASKS:
                return False
            self._state.update(
                task="full_reset",
                message="Clearing the saved job database and starting a fresh initial search…",
                error=False,
                progress={"done": 0, "total": 0, "label": "Clearing saved jobs"},
                details=[],
                event_id=self._state["event_id"] + 1,
            )
            self._seeded = False
        threading.Thread(target=self._run_full_reset, daemon=True).start()
        return True

    def _run_full_reset(self) -> None:
        connection = connect(self.database_path)
        try:
            clear_job_database(connection)
            summary = run_initial_import(connection, self.config, progress=self._update_progress)
            self._seeded = not summary.errors
            backlog_count = count_tab_jobs(connection, "backlog")
            message = (
                f"Full reset complete: {backlog_count} jobs saved in Backlog."
                if not summary.errors
                else "The database was cleared and the fresh search had some errors. Retry the initial search to fill any gaps."
            )
            with self._lock:
                self._state.update(
                    task="error" if summary.errors else "done",
                    message=message,
                    error=bool(summary.errors),
                    progress=None,
                    details=summary.errors[:8],
                    event_id=self._state["event_id"] + 1,
                )
        except Exception as error:
            self._finish_error(f"Full reset failed: {error}")
        finally:
            connection.close()

    def _run_initial_import(self) -> None:
        connection = connect(self.database_path)
        try:
            summary = run_initial_import(
                connection,
                self.config,
                progress=self._update_progress,
            )
            backlog_count = count_tab_jobs(connection, "backlog")
        except Exception as error:
            self._finish_error(f"Initial import failed: {error}")
        else:
            with self._lock:
                self._seeded = not summary.errors
                message = (
                    f"Partial import: {backlog_count} jobs saved in Backlog. "
                    "Some searches failed; retry to finish the import."
                    if summary.errors
                    else f"Initial import complete: {backlog_count} jobs saved in Backlog."
                )
                self._state.update(
                    task="error" if summary.errors else "done",
                    message=message,
                    error=bool(summary.errors),
                    progress=None,
                    details=summary.errors[:8],
                    event_id=self._state["event_id"] + 1,
                )
        finally:
            connection.close()

    def _update_progress(self, done: int, total: int, label: str) -> None:
        with self._lock:
            if self._state["task"] in {"initial_import", "partial_reset", "full_reset"}:
                self._state["progress"] = {"done": done, "total": total, "label": label}
                self._state["message"] = f"Search: {done} of {total} searches complete."

    def start_latest_refresh(self) -> bool:
        with self._lock:
            if self._state["task"] in self.ACTIVE_TASKS:
                return False
            if self.config["initial_import_enabled"] and not self._seeded:
                return False
            self._preview = None
            self._preview_token = None
            self._state.update(
                task="latest_search",
                message="Searching job boards for new listings…",
                error=False,
                progress=None,
                details=[],
                requires_confirmation=False,
                event_id=self._state["event_id"] + 1,
            )
        threading.Thread(target=self._run_latest_preview, daemon=True).start()
        return True

    def _run_latest_preview(self) -> None:
        connection = connect(self.database_path)
        try:
            preview = preview_latest_refresh(connection, self.config)
            if preview.errors:
                self._finish_error(
                    "Refresh failed; nothing was added and the refresh time was not changed.",
                    preview.errors[:8],
                )
                return

            current_count = latest_job_count(connection)
            total_after = current_count + len(preview.jobs)
            if total_after > LATEST_LIMIT:
                token = secrets.token_urlsafe(24)
                with self._lock:
                    self._preview = preview
                    self._preview_token = token
                    self._state.update(
                        task="confirmation",
                        message="Review the Latest list size before saving this refresh.",
                        error=False,
                        progress=None,
                        preview_token=token,
                        preview_count=len(preview.jobs),
                        total_after=total_after,
                        requires_confirmation=True,
                        event_id=self._state["event_id"] + 1,
                    )
                return

            added = commit_latest_refresh(connection, preview.jobs)
            message = f"Refresh complete: {added} jobs added to Latest."
            self._finish_success(message)
        except Exception as error:
            self._finish_error(f"Refresh failed; nothing was added: {error}")
        finally:
            connection.close()

    def confirm_latest_refresh(self, token: str) -> tuple[bool, str]:
        with self._lock:
            if (
                self._state["task"] != "confirmation"
                or not token
                or token != self._preview_token
                or self._preview is None
            ):
                return False, "That refresh preview has expired. Start a new refresh."
            preview = self._preview
            self._preview = None
            self._preview_token = None
            self._state.update(task="committing", message="Saving the confirmed refresh…")
        connection = connect(self.database_path)
        try:
            added = commit_latest_refresh(connection, preview.jobs)
        except Exception as error:
            self._finish_error(f"Refresh could not be saved: {error}")
            return False, str(error)
        finally:
            connection.close()
        message = f"Refresh complete: {added} jobs added to Latest."
        self._finish_success(message)
        return True, "Refresh saved."

    def cancel_latest_refresh(self, token: str) -> tuple[bool, str]:
        with self._lock:
            if (
                self._state["task"] != "confirmation"
                or not token
                or token != self._preview_token
            ):
                return False, "That refresh preview has expired."
            self._preview = None
            self._preview_token = None
            self._state.update(
                task="idle",
                message="Refresh cancelled. Your saved jobs and refresh time were not changed.",
                error=False,
                requires_confirmation=False,
                preview_token=None,
                event_id=self._state["event_id"] + 1,
            )
            return True, "Refresh cancelled."

    def update_candidate_profile(self, values: dict[str, Any]) -> bool:
        profile = normalize_candidate_profile(values)
        with self._lock:
            if self._state["task"] in self.ACTIVE_TASKS:
                return False
            profile = save_candidate_profile(profile)
            self.config["candidate_profile"] = profile
            self._state.update(
                task="done",
                message="AI ranking profile saved. Existing rankings will refresh the next time you run Prioritize top 30.",
                error=False,
                progress=None,
                details=[],
                preview_token=None,
                requires_confirmation=False,
                event_id=self._state["event_id"] + 1,
            )
            return True

    def start_listing_check(self) -> bool:
        with self._lock:
            if self._state["task"] in self.ACTIVE_TASKS:
                return False
            self._state.update(
                task="listing_check",
                message="Searching Indeed and Glassdoor for recent listings…",
                error=False,
                progress={"done": 0, "total": 0, "label": "Preparing job-board searches"},
                details=[],
                event_id=self._state["event_id"] + 1,
            )
        threading.Thread(target=self._run_listing_check, daemon=True).start()
        return True

    def _run_listing_check(self) -> None:
        connection = connect(self.database_path)
        board_log = "Indeed/Glassdoor added: 0; search did not complete."
        seek_log = "SEEK added: 0; email sync did not complete."
        expired_log = "Deleted: expiry check did not complete."
        latest_log = "Latest: no new jobs were added."
        new_job_ids: list[int] = []
        errors: list[str] = []
        board_search_ok = False
        try:
            try:
                board_summary = run_listing_search(
                    connection, self.config, progress=self._update_listing_progress
                )
                site_stats = board_summary.sites
                board_added = sum(stats["added"] for stats in site_stats.values())
                board_existing = sum(stats["existing"] for stats in site_stats.values())
                new_job_ids.extend(board_summary.new_job_ids)
                indeed = site_stats.get("indeed", {"found": 0, "added": 0, "existing": 0})
                glassdoor = site_stats.get("glassdoor", {"found": 0, "added": 0, "existing": 0})
                board_log = (
                    f"Indeed/Glassdoor added: {board_added} new; {board_existing} already saved or duplicates "
                    f"(Indeed {indeed['added']} added, Glassdoor {glassdoor['added']} added; "
                    f"up to {board_summary.requested} results across {board_summary.completed_searches} searches, "
                    f"covering the last {board_summary.hours_old} hours)."
                )
                errors.extend(board_summary.errors)
                board_search_ok = not board_summary.errors
            except Exception as error:
                board_log = f"Indeed/Glassdoor added: 0; search failed: {error}"
                errors.append(f"Indeed/Glassdoor search: {error}")

            with self._lock:
                if self._state["task"] == "listing_check":
                    self._state.update(
                        message="Reading new SEEK alerts from Gmail…",
                        progress=None,
                    )
            if not gmail_connected():
                seek_log = "SEEK added: 0; Gmail is not connected, so email alerts were skipped."
            else:
                try:
                    seek_summary = sync_seek_alerts(connection, self.config)
                    new_job_ids.extend(seek_summary.new_job_ids)
                    seek_log = (
                        f"SEEK added: {seek_summary.jobs_added} new; {seek_summary.jobs_existing} already saved; "
                        f"checked {seek_summary.messages_seen} labelled emails "
                        f"({seek_summary.messages_processed} processed, "
                        f"{seek_summary.messages_without_jobs} without readable job links, "
                        f"{seek_summary.messages_without_body} without a readable body)."
                    )
                except Exception as error:
                    seek_log = f"SEEK added: 0; email sync failed: {error}"
                    errors.append(f"SEEK email sync: {error}")

            with self._lock:
                if self._state["task"] == "listing_check":
                    self._state.update(
                        message="Checking all saved listings for confirmed expiry…",
                        progress={"done": 0, "total": 0, "label": "Checking source listings"},
                    )
            try:
                removed, uncertain = remove_confirmed_expired(connection)
                expired_log = (
                    f"Deleted: {removed} confirmed expired jobs; {uncertain} unclear listings were kept."
                )
            except Exception as error:
                expired_log = f"Deleted: expiry check failed: {error}"
                errors.append(f"Expiry check: {error}")

            try:
                unique_ids = list(dict.fromkeys(new_job_ids))
                saved_ids = {
                    int(row["id"])
                    for row in connection.execute("SELECT id FROM jobs").fetchall()
                }
                active_ids = [job_id for job_id in unique_ids if job_id in saved_ids]
                refreshed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                save_latest_jobs(
                    connection,
                    active_ids,
                    refreshed_at,
                    update_refresh_time=board_search_ok,
                )
                latest_count = min(len(active_ids), LATEST_LIMIT)
                latest_log = (
                    f"Latest: {latest_count} new active jobs listed; the tab keeps the newest "
                    f"{LATEST_LIMIT} jobs total."
                )
            except Exception as error:
                latest_log = f"Latest: could not update the list: {error}"
                errors.append(f"Latest list update: {error}")

            message = "Check listings complete: board search, SEEK sync, and expiry check finished."
            if errors:
                message = "Check listings finished with some errors; see the step logs below."
            final_details = [expired_log, board_log, seek_log, latest_log] + [
                f"Error: {item}" for item in errors[:5]
            ]
            if errors:
                self._finish_error(message, final_details)
            else:
                self._finish_success(message, details=final_details)
        except Exception as error:
            self._finish_error(
                f"Check listings failed: {error}",
                [expired_log, board_log, seek_log, latest_log, f"Error: {error}"],
            )
        finally:
            connection.close()

    def _update_listing_progress(self, done: int, total: int, label: str) -> None:
        with self._lock:
            if self._state["task"] != "listing_check":
                return
            self._state.update(
                message=f"Searching Indeed and Glassdoor: {done} of {total} searches complete.",
                progress={"done": done, "total": total, "label": label},
            )

    def start_gmail_cleanup(self) -> bool:
        with self._lock:
            if self._state["task"] in self.ACTIVE_TASKS:
                return False
            self._state.update(
                task="gmail_cleanup",
                message="Moving old imported SEEK alert emails to Gmail Trash…",
                error=False,
                progress=None,
                details=[],
                event_id=self._state["event_id"] + 1,
            )
        threading.Thread(target=self._run_gmail_cleanup, daemon=True).start()
        return True

    def _run_gmail_cleanup(self) -> None:
        connection = connect(self.database_path)
        try:
            removed, failed = trash_stale_imported_alerts(
                connection, self.config.get("gmail_stale_after_days", 90)
            )
            message = f"Gmail cleanup complete: moved {removed} old imported SEEK emails to Trash."
            if failed:
                message += f" {failed} could not be moved."
            self._finish_success(message)
        except Exception as error:
            self._finish_error(f"Gmail cleanup failed: {error}")
        finally:
            connection.close()

    def start_ai_ranking(self) -> bool:
        with self._lock:
            if self._state["task"] in self.ACTIVE_TASKS:
                return False
            self._state.update(
                task="ai_ranking",
                message="Preparing your un-applied jobs for AI ranking…",
                error=False,
                progress=None,
                details=[],
                event_id=self._state["event_id"] + 1,
            )
        threading.Thread(target=self._run_ai_ranking, daemon=True).start()
        return True

    def _update_ai_progress(self, done: int, total: int, label: str) -> None:
        with self._lock:
            if self._state["task"] != "ai_ranking":
                return
            self._state.update(
                message="Ranking open jobs against your profile…",
                progress={"done": done, "total": total, "label": label},
            )

    def _run_ai_ranking(self) -> None:
        connection = connect(self.database_path)
        try:
            if self.config.get("ai_provider", "openai") != "openai":
                raise AIRankingError("This build currently supports the OpenAI Responses API provider.")
            summary = rank_open_jobs(
                connection,
                profile=self.config.get("candidate_profile", {}),
                locations=self.config.get("locations", []),
                model=self.config.get("ai_model", "gpt-6-luna"),
                endpoint=self.config.get("ai_responses_url", "https://api.openai.com/v1/responses"),
                batch_size=self.config.get("ai_batch_size", 10),
                progress=self._update_ai_progress,
            )
            if summary.assessed:
                message = (
                    f"AI ranking complete: assessed {summary.assessed} new or changed jobs, "
                    f"reused {summary.reused} saved assessments, and selected up to {summary.prioritized} for Prioritized."
                )
            else:
                message = (
                    f"AI priorities are up to date: reused {summary.reused} saved assessments; "
                    f"no API calls were needed."
                )
            self._finish_success(message)
        except Exception as error:
            self._finish_error(
                f"AI ranking stopped: {error}. Completed batches are saved; run it again to resume."
            )
        finally:
            connection.close()

    def _finish_success(self, message: str, details: list[str] | None = None) -> None:
        with self._lock:
            self._state.update(
                task="done",
                message=message,
                error=False,
                progress=None,
                details=details or [],
                preview_token=None,
                requires_confirmation=False,
                event_id=self._state["event_id"] + 1,
            )

    def _finish_error(self, message: str, details: list[str] | None = None) -> None:
        with self._lock:
            self._state.update(
                task="error",
                message=message,
                error=True,
                progress=None,
                preview_token=None,
                requires_confirmation=False,
                details=details or [],
                event_id=self._state["event_id"] + 1,
            )


def create_app(*, seed_on_start: bool = True, config: dict | None = None, database_path=DATABASE_PATH) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = secrets.token_hex(32)
    search_config = config or load_search_config()
    controller = SearchController(search_config, database_path)
    app.extensions["job_finder_search_controller"] = controller

    @app.get("/")
    def index():
        tab = request.args.get("tab", "backlog").casefold()
        if tab not in {"applied", "backlog", "latest", "prioritized", "deleted", "profile"}:
            tab = "backlog"
        if request.args.get("dismiss_logs") == "1":
            controller.dismiss_completed_status()
        location = request.args.get("location", "")
        valid_locations = {"", *search_config["locations"]}
        if location not in valid_locations:
            location = ""
        query = request.args.get("q", "").strip()[:100]
        page = max(1, request.args.get("page", 1, type=int) or 1)
        page_size = 50
        candidate_profile = controller.config.get("candidate_profile", {})

        connection = connect(database_path)
        try:
            priority_data = prioritized_view_data(
                connection,
                profile=candidate_profile,
                allowed_locations=search_config["locations"],
                model=controller.config.get("ai_model", "gpt-6-luna"),
                location=location,
                query=query,
            )
            if tab == "profile":
                total = 0
                rows = []
            elif tab == "prioritized":
                total = priority_data["total"]
                rows = priority_data["jobs"][(page - 1) * page_size:page * page_size]
            else:
                total = count_tab_jobs(
                    connection,
                    tab,
                    location or None,
                    query or None,
                )
                rows = list_tab_jobs(
                    connection,
                    tab,
                    location or None,
                    query or None,
                    limit=page_size,
                    offset=(page - 1) * page_size,
                )
            jobs = []
            for row in rows:
                job = dict(row)
                job["latest_new"] = bool(job["latest_new"])
                job["placement"] = (
                    job["status"]
                    if job["status"] in APPLICATION_STATUSES
                    else job["list_bucket"]
                )
                job["manual_priority"] = job["list_bucket"] == "prioritized"
                job["description_text"] = _plain_description(job.get("description"))
                job["sources"] = [dict(source) for source in list_job_sources(connection, job["id"])]
                grouped_sources = {}
                for source in job["sources"]:
                    grouped_sources.setdefault(source["source"], []).append(source["url"])
                job["source_groups"] = [
                    {"source": name, "urls": sorted(set(urls))}
                    for name, urls in sorted(grouped_sources.items())
                ]
                jobs.append(job)
            counts = {
                name: count_tab_jobs(connection, name)
                for name in ("latest", "applied", "backlog", "deleted")
            }
            counts["prioritized"] = priority_data["ranked_count"]
        finally:
            connection.close()

        return render_template(
            "index.html",
            tab=tab,
            jobs=jobs,
            total=total,
            counts=counts,
            candidate_profile=candidate_profile,
            tab_labels={
                "latest": "Latest",
                "prioritized": "Prioritized",
                "applied": "Applied",
                "backlog": "Backlog",
                "deleted": "Deleted",
                "profile": "Profile",
            },
            page=page,
            page_size=page_size,
            location=location,
            query=query,
            locations=search_config["locations"],
            search_settings={
                "titles": list(search_config.get("titles", [])),
                "locations": list(search_config.get("locations", [])),
                "sites": list(search_config.get("sites", [])),
                "results_per_site": dict(search_config.get("results_per_site", {})),
                "hours_old": search_config.get("hours_old", 24),
                "check_results_total": search_config.get("check_results_total", 1500),
                "initial_results_per_title_location": search_config.get(
                    "initial_results_per_title_location", 250
                ),
                "gmail_query": search_config.get("gmail_query", "label:SEEK"),
                "gmail_max_messages": search_config.get("gmail_max_messages", 250),
                "ai_model": search_config.get("ai_model", "gpt-6-luna"),
                "ai_batch_size": search_config.get("ai_batch_size", 10),
            },
            job_buckets=JOB_BUCKETS,
            application_statuses=APPLICATION_STATUSES,
            search_state=controller.snapshot(),
            latest_limit=LATEST_LIMIT,
            gmail_connected=gmail_connected(),
            ai_model=controller.config.get("ai_model", "gpt-6-luna"),
            ai_api_key_configured=api_key_configured(),
            ai_api_key_source=api_key_source(),
            ai_candidate_count=priority_data["candidate_count"],
            ai_pending_count=priority_data["pending_count"],
            ai_ranked_count=priority_data["ai_ranked_count"],
            manual_priority_count=priority_data["manual_priority_count"],
        )

    @app.post("/jobs/<int:job_id>")
    def update_job(job_id: int):
        placement = request.form.get("placement", "")
        notes = request.form.get("notes", "").strip()[:2000]
        tab = request.form.get("tab", "backlog")
        location = request.form.get("location", "")
        query = request.form.get("q", "")
        page = request.form.get("page", "1")
        if placement not in JOB_PLACEMENTS:
            flash("Choose Backlog, Prioritized, Deleted, or a valid application status.", "error")
            return redirect(url_for("index", tab=tab, location=location, q=query, page=page))
        connection = connect(database_path)
        try:
            found = set_job_placement(connection, job_id, placement)
            if found:
                set_note(connection, job_id, notes)
        finally:
            connection.close()
        flash("Job updated." if found else "That job could not be found.", "success" if found else "error")
        return redirect(url_for("index", tab=tab, location=location, q=query, page=page))

    @app.post("/deleted/clear")
    def clear_deleted():
        if controller.snapshot()["busy"]:
            flash("Wait for the current search or ranking to finish before clearing Deleted.", "error")
            return redirect(url_for("index", tab="deleted"))
        connection = connect(database_path)
        try:
            removed = delete_deleted_jobs(connection)
        finally:
            connection.close()
        flash(f"Permanently removed {removed} job{'s' if removed != 1 else ''} from Deleted.", "success")
        return redirect(url_for("index", tab="deleted"))

    @app.post("/latest/refresh")
    def refresh_latest():
        if controller.start_latest_refresh():
            flash("Search started. You can keep using the app while it runs.", "success")
        else:
            flash("Finish the initial import before refreshing Latest, or wait for the current search.", "error")
        return redirect(url_for("index", tab="latest"))

    @app.post("/initial/retry")
    def retry_initial():
        if controller.start_initial_import():
            flash("Initial import started.", "success")
        else:
            flash("The initial import is already complete or a search is running.", "error")
        return redirect(url_for("index", tab="backlog"))

    @app.post("/database/partial-reset")
    def partial_reset():
        if controller.start_partial_reset():
            flash("Partial reset started: refreshing the full pool and checking saved job-board listing URLs.", "success")
        else:
            flash("Wait for the current search or refresh to finish before resetting.", "error")
        return redirect(url_for("index", tab="backlog"))

    @app.post("/database/full-reset")
    def full_reset():
        if controller.start_full_reset():
            flash("Full reset started. Saved jobs and their statuses, notes, and Latest history were cleared.", "success")
        else:
            flash("Wait for the current search or refresh to finish before resetting.", "error")
        return redirect(url_for("index", tab="backlog"))

    @app.post("/backlog/refresh")
    def refresh_backlog():
        return_tab = request.form.get("return_tab", "backlog").casefold()
        if return_tab not in {"latest", "prioritized", "applied", "backlog", "deleted", "profile"}:
            return_tab = "backlog"
        if controller.start_listing_check():
            flash("Check listings started: Indeed, Glassdoor, SEEK email alerts, then an expiry check.", "success")
        else:
            flash("Wait for the current search or refresh to finish before checking listings.", "error")
        return redirect(url_for("index", tab=return_tab))

    @app.post("/profile")
    def update_profile():
        try:
            updated = controller.update_candidate_profile(
                {
                    "circumstances": request.form.get("circumstances", ""),
                    "max_required_pqe": request.form.get("max_required_pqe", "1"),
                    "resume_summary": request.form.get("resume_summary", ""),
                }
            )
        except (ValueError, OSError) as error:
            flash(str(error), "error")
        else:
            if updated:
                flash("AI ranking profile saved. Existing rankings will update the next time you run Prioritize top 30.", "success")
            else:
                flash("Wait for the current search to finish before changing the AI ranking profile.", "error")
        return redirect(url_for("index", tab="profile"))

    @app.post("/ai/api-key")
    def update_ai_api_key():
        try:
            save_api_key(request.form.get("api_key", ""))
        except (ValueError, OSError) as error:
            flash(str(error), "error")
        else:
            flash(
                "OpenAI API key saved to the ignored .env file. API usage is billed separately from a ChatGPT subscription.",
                "success",
            )
        return redirect(url_for("index", tab="prioritized"))

    @app.post("/ai/prioritize")
    def prioritize_jobs():
        if search_config.get("ai_provider", "openai") != "openai":
            flash("This app currently uses the OpenAI Responses API for AI ranking.", "error")
        elif not api_key_configured():
            flash("Save an OpenAI API key before starting AI ranking.", "error")
        elif controller.start_ai_ranking():
            flash("AI ranking started. Completed batches will be saved as they finish.", "success")
        else:
            flash("Wait for the current search or refresh to finish before ranking jobs.", "error")
        return redirect(url_for("index", tab="prioritized"))

    @app.get("/gmail/oauth/connect")
    def gmail_oauth_connect():
        try:
            authorize_gmail()
        except GmailSetupError as error:
            flash(str(error), "error")
        except Exception as error:
            flash(f"Could not connect Gmail: {error}", "error")
        else:
            flash("Gmail connected. SEEK alerts will be imported when you press Check listings.", "success")
        return redirect(url_for("index", tab="backlog"))

    @app.post("/gmail/cleanup")
    def gmail_cleanup():
        if not gmail_connected():
            flash("Connect Gmail before cleaning up old SEEK alerts.", "error")
        elif controller.start_gmail_cleanup():
            flash(
                f"Email cleanup started. Only imported SEEK alerts older than {search_config.get('gmail_stale_after_days', 90)} days will be moved to Trash.",
                "success",
            )
        else:
            flash("Wait for the current search or sync to finish.", "error")
        return redirect(url_for("index", tab="backlog"))

    @app.get("/api/search-state")
    def search_state():
        return jsonify(controller.snapshot())

    @app.post("/api/latest/confirm")
    def confirm_latest():
        payload = request.get_json(silent=True) or {}
        success, message = controller.confirm_latest_refresh(str(payload.get("token", "")))
        return jsonify({"success": success, "message": message}), (200 if success else 409)

    @app.post("/api/latest/cancel")
    def cancel_latest():
        payload = request.get_json(silent=True) or {}
        success, message = controller.cancel_latest_refresh(str(payload.get("token", "")))
        return jsonify({"success": success, "message": message}), (200 if success else 409)

    if seed_on_start and search_config["initial_import_enabled"] and not controller.snapshot()["seeded"]:
        controller.start_initial_import()
    return app


def launch_web_app() -> None:
    remove_schedule()
    app = create_app()
    url = "http://127.0.0.1:8765"
    print(f"Job Finder is running at {url} (press Ctrl+C in this window to stop).")
    threading.Timer(0.8, lambda: webbrowser.open_new_tab(url)).start()
    app.run(host="127.0.0.1", port=8765, threaded=True, debug=False, use_reloader=False)
