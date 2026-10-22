# Job Finder

A small local job search and tracking starter for Australian roles. It searches selected sites with [JobSpy](https://github.com/speedyapply/JobSpy), stores listings in a SQLite database, and marks listings as new when first seen.

JobSpy currently supports LinkedIn, Indeed, Glassdoor, ZipRecruiter, and several other boards. This starter enables Indeed and Glassdoor for Australia by default. LinkedIn scraping is disabled by default; check each board's terms and access rules before enabling automated collection.

## Setup (macOS)

Python 3.10 or newer is required.

```sh
cd /path/to/projectx
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Edit `config.toml` to set your search terms, location, sources, and result limit. Then run:

```sh
python -m job_finder search
```

The first search stores jobs in `data/jobs.sqlite3`. Later searches add new jobs, refresh details for known jobs, and preserve your application status and notes.

## Browse and update jobs

```sh
python -m job_finder list
python -m job_finder list --status new
python -m job_finder status 1 applied
python -m job_finder note 1 "Applied on company website"
```

Valid statuses are `new`, `saved`, `applied`, `interviewing`, `rejected`, and `closed`. Use the numeric ID shown by `list` for status and note commands.

## Data and config

- `config.toml`: search settings.
- `data/jobs.sqlite3`: local job master list, created on first run (ignored by Git).
- Search source errors are reported; partial results from other sources are still saved.

To search another supported site, add its JobSpy site name to `sites` in `config.toml`. Job board support and behavior can change over time, and search results may be incomplete or blocked.

---

## AI handoff context

Copy the block below into a new AI chat to explain this project and its intended direction.

```text
Project: Job Finder (working name)

Goal
Build a personal job discovery and application-tracking tool for Australian job seekers. It should collect listings that match a user's role keywords and location from multiple sources, combine them into one master list, avoid adding duplicates, highlight newly discovered or freshly posted jobs, and let the user track applications and notes. A possible future direction is a basic Mac app or hosted service that can notify interested users by email. That is not implemented yet.

Current state
- This repository contains a small Python command-line starter.
- It uses the python-jobspy package to search configured job boards.
- Search settings are in config.toml.
- Current defaults: query "software engineer", location "Perth, WA, Australia", sites "indeed" and "glassdoor", up to 25 results per site, listings up to 168 hours old, country "Australia" for Indeed/Glassdoor.
- Run a search with: python -m job_finder search
- List saved jobs with: python -m job_finder list
- Filter by status with: python -m job_finder list --status new
- Change a job's status with: python -m job_finder status JOB_ID STATUS
- Add/replace a note with: python -m job_finder note JOB_ID "your note"
- Supported statuses: new, saved, applied, interviewing, rejected, closed.
- Job records are stored locally in data/jobs.sqlite3. The database is created automatically and is ignored by Git.
- The starter deduplicates primarily by listing URL, falling back to company + title + location. It preserves existing status and notes when a listing is seen again.
- Python 3.10 or newer is required by JobSpy. On Apple silicon Macs with Homebrew, a typical environment setup is: brew install python; python3 -m venv .venv; source .venv/bin/activate; python -m pip install -r requirements.txt.

Important source notes
- JobSpy currently lists LinkedIn, Indeed, Glassdoor, ZipRecruiter, Bayt, Naukri, BDJobs and HelloWork; its README says Google Jobs is currently unavailable. Verify current support before relying on any source.
- JobSpy documents Australia support for Indeed and Glassdoor. LinkedIn is global, but its terms prohibit scraping and unauthorized automation; it is intentionally not enabled by default. ZipRecruiter is documented for the US/Canada, and many other sources target other regions.
- SEEK is not a JobSpy source. The planned approach is to use SEEK job-alert emails, delivered to a dedicated Gmail account or label, and import those emails into the same master list. Gmail API access is possible with Google OAuth; a personal read-only integration is a possible next step. Gmail's gmail.readonly scope is classified by Google as restricted, which adds requirements if the app is later offered as a hosted service to other users.
- Job board access, terms, and scraper reliability can change. Prefer official feeds/APIs, email alerts, or permitted imports where available; do not assume a scraper is stable or permitted.

Likely next steps
1. Make searches and deduplication configurable for the user's real target roles, locations, and Australian job sources.
2. Add SEEK alert email import, preferably limited to a dedicated label/mailbox, and parse listings into the existing job schema.
3. Improve duplicate matching across different source URLs and retain all source links for one role.
4. Add a basic user interface for browsing new jobs and updating application status.
5. Consider packaging for another Mac or hosting with scheduled searches and opt-in email notifications after the personal workflow is reliable.

When continuing work, inspect the current repository before changing it. Keep the first version simple and local unless asked to expand it. Do not claim a source works without verifying it; explain source limitations and test actual code paths when asked to verify implementation.
```
