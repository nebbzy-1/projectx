# Job Finder

A local Australian job search and application tracker. The interface runs in your browser from a small server on this Mac; listings, statuses, and notes are stored in SQLite locally.

## Install and start

Use Python 3.10 or newer. From the project folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m job_finder start
```

The start command opens Job Finder at `http://127.0.0.1:8765`. Leave the terminal window open while using it; press `Ctrl+C` there to stop the server. The first launch runs one initial import into Backlog. Later launches load saved jobs without searching automatically.

## Use the app

- **Latest** shows jobs found by a manual refresh. A blue dot marks jobs from the most recent refresh.
- **Applied** tracks roles marked applied, interviewing, rejected, or closed.
- **Backlog** holds new and saved roles.
- Filter any list by location or search text. Update a job's status and notes on its card.
- The left navigation is ordered **Latest**, **Applied**, **Backlog**. Latest keeps up to 26 items. If a refresh would put more than 25 in the list, the app asks before saving. Older items removed from Latest remain in Backlog and SQLite.

## Search settings

Edit `config.toml` to change the titles, locations, sources, and result limits. Defaults are Graduate Lawyer, Junior Lawyer, and Associate Lawyer in Perth, Melbourne, Sydney, and Brisbane, using Indeed and Glassdoor. The initial import requests up to 250 results per title/location pair across the enabled sites.

Job board results may be incomplete or blocked. LinkedIn scraping is disabled. SEEK/Gmail import, hosting, and email alerts are paused.

## Reset

Close the app, then remove the SQLite files to clear jobs, statuses, notes, source links, and refresh history:

```sh
rm -f data/jobs.sqlite3 data/jobs.sqlite3-shm data/jobs.sqlite3-wal
```

The next launch creates a new database and runs the initial import. The database file is tracked by Git in this repository; don't commit personal job data.

To recreate the Python environment while keeping your jobs, deactivate it and remove `.venv` first:

```sh
deactivate
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

If `deactivate` is unavailable, continue. To clear the jobs too, also run the database removal command above.

## Optional command line

```sh
python -m job_finder list
python -m job_finder status 1 applied
python -m job_finder note 1 "Applied on company website"
```

## AI handoff

```text
Job Finder is a local Python job search and application tracker for Australia. Launch with `python -m job_finder start`; it starts a local Flask server and opens the browser UI. Keep the terminal open and stop the server with Ctrl+C. The first launch imports configured jobs once into SQLite; later launches do not search automatically.

The left navigation is ordered Latest, Applied, Backlog. Backlog holds new and saved roles. The UI also has a location dropdown and text search, job status and notes, and manual Latest refresh. Latest searches since the initial import and then since the last successful refresh, keeps up to 26 items, and marks the newest batch with blue dots. User confirmation appears if the refresh would put the total above 25; removed Latest items remain in Backlog and SQLite.

Defaults in config.toml: Graduate Lawyer, Junior Lawyer, Associate Lawyer; Perth, Melbourne, Sydney, Brisbane; Indeed and Glassdoor. Initial import cap is 250 per title/location pair across enabled sources. LinkedIn scraping is disabled. SEEK/Gmail, hosting, and email alerts are paused.

SQLite data persists across app closes. Preserve it unless the user requests a reset, and never commit personal job data.
```

## Project maintenance

Update this README in the same change whenever app behavior, setup steps, dependencies, data storage, or commands change. Keep the install/start instructions and AI handoff aligned with the implementation.
