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
