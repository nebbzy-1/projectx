from __future__ import annotations

import secrets
import threading
from typing import Any
import webbrowser
from html.parser import HTMLParser

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

from .config import DATABASE_PATH, load_search_config
from .database import (
    LATEST_LIMIT,
    STATUSES,
    connect,
    count_tab_jobs,
    clear_job_database,
    get_metadata,
    latest_job_count,
    list_job_sources,
    list_tab_jobs,
    restore_job_to_backlog,
    set_note,
    set_status,
)
from .runner import (
    INITIAL_IMPORT_KEY,
    LatestPreview,
    commit_latest_refresh,
    preview_latest_refresh,
    run_initial_import,
)
from .availability import remove_confirmed_expired
from .profile import load_candidate_profile, normalize_candidate_profile, save_candidate_profile
from .scheduler import remove_schedule
from .smart_filter import ensure_jobs_classified, reclassify_jobs


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

    ACTIVE_TASKS = {"initial_import", "partial_reset", "full_reset", "listing_check", "latest_search", "confirmation", "committing"}

    def __init__(self, config: dict, database_path=DATABASE_PATH):
        self.config = config
        self.config.setdefault("candidate_profile", load_candidate_profile())
        self.database_path = database_path
        self._lock = threading.RLock()
        self._preview: LatestPreview | None = None
        self._preview_latest_jobs: list[dict] = []
        self._preview_token: str | None = None
        connection = connect(self.database_path)
        try:
            ensure_jobs_classified(connection, self.config.get("candidate_profile", {}))
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
            deleted_count = count_tab_jobs(connection, "deleted")
            message = (
                f"Full reset complete: {backlog_count} jobs in Backlog; {deleted_count} raw listings in Deleted."
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
            deleted_count = count_tab_jobs(connection, "deleted")
        except Exception as error:
            self._finish_error(f"Initial import failed: {error}")
        else:
            with self._lock:
                self._seeded = not summary.errors
                message = (
                    f"Partial import: {backlog_count} jobs in Backlog; {deleted_count} raw listings in Deleted. "
                    "Some searches failed; retry to finish the import."
                    if summary.errors
                    else f"Initial import complete: {backlog_count} jobs in Backlog; {deleted_count} raw listings saved in Deleted."
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
            self._preview_latest_jobs = []
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

            visible_jobs = [job for job in preview.jobs if job.get("fit_status") != "skip"]
            skipped_count = len(preview.jobs) - len(visible_jobs)
            current_count = latest_job_count(connection)
            total_after = current_count + len(visible_jobs)
            if total_after > LATEST_LIMIT - 1:
                token = secrets.token_urlsafe(24)
                with self._lock:
                    self._preview = preview
                    self._preview_latest_jobs = visible_jobs
                    self._preview_token = token
                    self._state.update(
                        task="confirmation",
                        message="Review the Latest list size before saving this refresh.",
                        error=False,
                        progress=None,
                        preview_token=token,
                        preview_count=len(visible_jobs),
                        skipped_count=skipped_count,
                        total_after=total_after,
                        requires_confirmation=True,
                        event_id=self._state["event_id"] + 1,
                    )
                return

            added = commit_latest_refresh(connection, preview.jobs, latest_jobs=visible_jobs)
            message = f"Refresh complete: {added} jobs added to Latest."
            if skipped_count:
                message += f" {skipped_count} low-fit jobs were saved in Deleted."
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
            visible_jobs = self._preview_latest_jobs
            skipped_count = int(self._state.get("skipped_count", 0))
            self._preview = None
            self._preview_latest_jobs = []
            self._preview_token = None
            self._state.update(task="committing", message="Saving the confirmed refresh…")
        connection = connect(self.database_path)
        try:
            added = commit_latest_refresh(connection, preview.jobs, latest_jobs=visible_jobs)
        except Exception as error:
            self._finish_error(f"Refresh could not be saved: {error}")
            return False, str(error)
        finally:
            connection.close()
        message = f"Refresh complete: {added} jobs added to Latest."
        if skipped_count:
            message += f" {skipped_count} low-fit jobs were saved in Deleted."
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
            self._preview_latest_jobs = []
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

    def update_candidate_profile(self, values: dict[str, Any]) -> tuple[bool, int]:
        profile = normalize_candidate_profile(values)
        with self._lock:
            if self._state["task"] in self.ACTIVE_TASKS:
                return False, 0
            profile = save_candidate_profile(profile)
            self.config["candidate_profile"] = profile
            connection = connect(self.database_path)
            try:
                reclassified = reclassify_jobs(connection, profile, reset_overrides=True)
            finally:
                connection.close()
            self._state.update(
                task="done",
                message=f"Smart filter profile saved. Rechecked {reclassified} saved jobs.",
                error=False,
                progress=None,
                details=[],
                preview_token=None,
                requires_confirmation=False,
                event_id=self._state["event_id"] + 1,
            )
            return True, reclassified

    def start_listing_check(self) -> bool:
        with self._lock:
            if self._state["task"] in self.ACTIVE_TASKS:
                return False
            self._state.update(
                task="listing_check",
                message="Checking saved Indeed and Glassdoor listings…",
                error=False,
                progress=None,
                details=[],
                event_id=self._state["event_id"] + 1,
            )
        threading.Thread(target=self._run_listing_check, daemon=True).start()
        return True

    def _run_listing_check(self) -> None:
        connection = connect(self.database_path)
        try:
            removed, uncertain = remove_confirmed_expired(connection)
            message = f"Listing check complete: {removed} confirmed expired jobs removed."
            if uncertain:
                message += f" {uncertain} could not be confirmed and were kept."
            self._finish_success(message)
        except Exception as error:
            self._finish_error(f"Listing check failed: {error}")
        finally:
            connection.close()

    def _finish_success(self, message: str) -> None:
        with self._lock:
            self._state.update(
                task="done",
                message=message,
                error=False,
                progress=None,
                details=[],
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
        if tab not in {"applied", "backlog", "latest", "deleted"}:
            tab = "backlog"
        location = request.args.get("location", "")
        valid_locations = {"", *search_config["locations"]}
        if location not in valid_locations:
            location = ""
        query = request.args.get("q", "").strip()[:100]
        page = max(1, request.args.get("page", 1, type=int) or 1)
        page_size = 50

        connection = connect(database_path)
        try:
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
                job["description_text"] = _plain_description(job.get("description"))
                if job["status"] in {"applied", "interviewing", "rejected", "closed"}:
                    job["archive_route"] = f"Application · {job['status'].replace('_', ' ').title()}"
                elif job["fit_status"] == "skip":
                    job["archive_route"] = "Set aside · low fit"
                else:
                    job["archive_route"] = "In Backlog"
                job["sources"] = [dict(source) for source in list_job_sources(connection, job["id"])]
                jobs.append(job)
            counts = {
                name: count_tab_jobs(connection, name)
                for name in ("latest", "applied", "backlog", "deleted")
            }
        finally:
            connection.close()

        return render_template(
            "index.html",
            tab=tab,
            jobs=jobs,
            total=total,
            counts=counts,
            candidate_profile=controller.config.get("candidate_profile", {}),
            tab_labels={
                "latest": "Latest",
                "applied": "Applied",
                "backlog": "Backlog",
                "deleted": "Deleted",
            },
            page=page,
            page_size=page_size,
            location=location,
            query=query,
            locations=search_config["locations"],
            statuses=STATUSES,
            search_state=controller.snapshot(),
            latest_limit=LATEST_LIMIT,
        )

    @app.post("/jobs/<int:job_id>")
    def update_job(job_id: int):
        status = request.form.get("status", "")
        notes = request.form.get("notes", "").strip()[:2000]
        tab = request.form.get("tab", "backlog")
        location = request.form.get("location", "")
        query = request.form.get("q", "")
        page = request.form.get("page", "1")
        if status not in STATUSES:
            flash("Choose a valid application status.", "error")
            return redirect(url_for("index", tab=tab, location=location, q=query, page=page))
        connection = connect(database_path)
        try:
            found = set_status(connection, job_id, status)
            if found:
                set_note(connection, job_id, notes)
        finally:
            connection.close()
        flash("Job updated." if found else "That job could not be found.", "success" if found else "error")
        return redirect(url_for("index", tab=tab, location=location, q=query, page=page))

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
            flash("Partial reset started: refreshing the full pool and checking saved Indeed/Glassdoor listing URLs.", "success")
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
        if controller.start_listing_check():
            flash("Listing check started. Confirmed expired Indeed/Glassdoor listings will be removed; unclear results will be kept.", "success")
        else:
            flash("Wait for the current search or refresh to finish before checking listings.", "error")
        return redirect(url_for("index", tab="backlog"))

    @app.post("/jobs/<int:job_id>/restore")
    def restore_job(job_id: int):
        connection = connect(database_path)
        try:
            restored = restore_job_to_backlog(connection, job_id)
        finally:
            connection.close()
        flash("Restored to Backlog." if restored else "That job could not be restored.", "success" if restored else "error")
        return redirect(url_for("index", tab="deleted"))

    @app.post("/profile")
    def update_profile():
        try:
            updated, reclassified = controller.update_candidate_profile(
                {
                    "enabled": request.form.get("enabled") == "on",
                    "circumstances": request.form.get("circumstances", ""),
                    "max_required_pqe": request.form.get("max_required_pqe", "1"),
                }
            )
        except (ValueError, OSError) as error:
            flash(str(error), "error")
        else:
            if updated:
                flash(f"Smart filter saved. Rechecked {reclassified} jobs.", "success")
            else:
                flash("Wait for the current search to finish before changing the smart filter.", "error")
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
