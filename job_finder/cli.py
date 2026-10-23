from __future__ import annotations

import argparse
import sys

from .config import DATABASE_PATH, load_search_config
from .database import STATUSES, connect, list_job_sources, list_jobs, set_note, set_status
from .runner import run_search
from .scheduler import remove_schedule


def _search() -> None:
    try:
        config = load_search_config()
        connection = connect(DATABASE_PATH)
        summary = run_search(connection, config)
    except Exception as error:
        print(f"Search failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    phase = "Initial import" if summary.initial_import else "Daily search"
    print(f"{phase}: found {summary.found} listings, added {summary.added} new, {summary.existing} already known.")
    if summary.errors:
        print("Some searches failed; the initial import will retry next time." if summary.initial_import else "Some searches failed.", file=sys.stderr)
        for message in summary.errors:
            print(f"  - {message}", file=sys.stderr)
        raise SystemExit(1)


def _list(status: str | None) -> None:
    connection = connect(DATABASE_PATH)
    jobs = list_jobs(connection, status)
    if not jobs:
        print("No jobs found. Run `python -m job_finder search` first.")
        return
    for job in jobs:
        place = f" — {job['location']}" if job["location"] else ""
        company = job["company"] or "Unknown company"
        source = job["source"] or "unknown source"
        posted = f" | posted {job['date_posted']}" if job["date_posted"] else ""
        print(f"#{job['id']} [{job['status']}] {job['title']} — {company}{place}")
        print(f"   {source}{posted} | first seen {job['first_seen'][:10]}")
        source_links = list_job_sources(connection, job["id"])
        for item in source_links:
            print(f"   {item['source']}: {item['url']}")
        if not source_links:
            print(f"   {job['url'] or '(no URL)'}")
        if job["notes"]:
            print(f"   Note: {job['notes']}")
    connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Search and track job listings locally.")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("search", help="search configured boards and save new listings")
    list_parser = commands.add_parser("list", help="show saved listings")
    list_parser.add_argument("--status", choices=STATUSES)
    status_parser = commands.add_parser("status", help="update a job's application status")
    status_parser.add_argument("job_id", type=int)
    status_parser.add_argument("status", choices=STATUSES)
    note_parser = commands.add_parser("note", help="add or replace a job note")
    note_parser.add_argument("job_id", type=int)
    note_parser.add_argument("text")
    commands.add_parser("start", help="launch the local browser app")
    schedule_parser = commands.add_parser("schedule", help="remove a previous daily schedule")
    schedule_actions = schedule_parser.add_subparsers(dest="schedule_action", required=True)
    schedule_actions.add_parser("remove", help="remove the Job Finder macOS schedule")

    args = parser.parse_args()
    if args.command == "search":
        _search()
    elif args.command == "list":
        _list(args.status)
    elif args.command == "status":
        connection = connect(DATABASE_PATH)
        if not set_status(connection, args.job_id, args.status):
            parser.error(f"No job with ID {args.job_id}")
        print(f"Job #{args.job_id} marked {args.status}.")
    elif args.command == "note":
        connection = connect(DATABASE_PATH)
        if not set_note(connection, args.job_id, args.text):
            parser.error(f"No job with ID {args.job_id}")
        print(f"Note saved for job #{args.job_id}.")
    elif args.command == "start":
        from .web import launch_web_app

        launch_web_app()
    elif args.command == "schedule":
        removed = remove_schedule()
        print("Daily search schedule removed." if removed else "No previous daily schedule was installed.")


if __name__ == "__main__":
    main()
