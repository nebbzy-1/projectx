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

The start command opens Job Finder at `http://127.0.0.1:8765`. Leave the terminal window open while using it; press `Ctrl+C` there to stop the server. The first launch runs one initial search and saves every result to the Deleted archive; the fit filter routes suitable roles into Backlog. Later launches load saved jobs without searching automatically.

## Use the app

- **Latest** shows jobs found by a manual refresh. A blue dot marks jobs from the most recent refresh.
- **Applied** tracks roles marked applied, interviewing, rejected, or closed.
- **Backlog** holds new and saved roles.
- **Deleted** is the raw JobSpy archive after duplicate merging. Every unique listing stays here, including roles also routed to Backlog or Applied. Each card shows its current route; low-fit roles can be restored to Backlog.
- **Check listings** checks saved Indeed, Glassdoor, and SEEK URLs and removes only listings clearly confirmed expired or unavailable. If a board blocks the request or the result is unclear, the job is kept. This applies regardless of application status.
- **Reset job search** offers a partial reset that reruns the full configured import, keeps/deduplicates existing jobs, then checks saved source URLs for expiry. Full reset clears saved jobs, notes, statuses, source links, and Latest history, then runs a fresh initial import. Both keep your smart-filter profile.
- Filter any list by location or search text. Update a job's status and notes on its card.
- Job cards stay within the page width. Status, notes, and Save controls sit together; **View job description** expands across the full card below them. Dropdown indicators use the same small, rounded chevron style.
- The left navigation is ordered **Latest**, **Applied**, **Backlog**, **Deleted**. Latest keeps up to 26 items. If a refresh would put more than 25 in the list, the app asks before saving. Older items removed from Latest remain in Backlog and SQLite.

## Smart fit filter

Open **Smart fit filter** at the top of Backlog to edit your circumstances and maximum required PQE. The default profile is for a recently admitted graduate lawyer who has completed PLT, with a maximum of 1 year PQE. Save the profile to recheck existing jobs; new imports and refreshes use it automatically.

Every unique fetched listing is saved in SQLite and remains visible in the **Deleted** raw archive; copies from multiple boards are merged into one job with source links. The smart filter routes explicit PQE requirements above your limit and senior-only titles out of **Backlog** as low fit; junior cues are marked **Likely fit**, and unclear jobs stay in Backlog as **Review**. The Deleted archive labels each row as Backlog, Applied, or set aside, and low-fit jobs can be restored. If you list practice interests in your profile, the filter compares them with the job description and flags mismatches for review. The profile is saved in the ignored local file `data/smart_profile.json`; no external AI service is used.

The future AI-based ranking design is documented in [`prompts/job_fit_ranker.md`](prompts/job_fit_ranker.md), with its response contract in [`prompts/job_fit_result.schema.json`](prompts/job_fit_result.schema.json). These are design references only; the running app still uses the local rule-based filter and does not call an AI service.

## SEEK alerts via Gmail

The Backlog page has a **SEEK alerts from Gmail** panel. It reads only messages matching `gmail.query` in `config.toml` (default `label:SEEK`), imports SEEK job links into the same SQLite job pool, and merges duplicates with Indeed/Glassdoor results. It can import already-existing labelled messages as well as future alerts. Sync is manual; it does not run in the background.

### One-time Gmail setup

1. In Gmail, select the eight existing SEEK alerts and use **Label as → Create new → SEEK**. To label future alerts automatically, open one alert and choose **More → Filter messages like this**, then create a filter that applies the `SEEK` label. You can also choose “Also apply filter to matching conversations” to label matching messages already in Gmail.
2. Open [Google Cloud Console](https://console.cloud.google.com/). Create a project, enable **Gmail API**, and configure **Google Auth Platform**. Set the audience to **External**, leave publishing status as **Testing**, and add the Gmail account you will connect as a test user.
3. In **Data Access**, add the scope `https://www.googleapis.com/auth/gmail.modify`. Google classifies it as a restricted, broad Gmail scope. The app's code uses it to read the labelled alert messages and, only after you press the cleanup button, move old imported alerts to Gmail Trash; it has no send-email feature and does not permanently delete mail.
4. In **Clients**, create an OAuth client with application type **Desktop app**. Download the client JSON and save it in the project as `data/gmail_oauth_client.json`. Do not commit it. The app uses Google's local loopback OAuth flow, so you do not need to configure a redirect URI.
5. Install the project requirements, start the app, open it at `http://localhost:8765`, expand **SEEK alerts from Gmail**, and click **Connect Gmail**. Sign in to the same Gmail account and approve the requested access. Then click **Sync SEEK emails**; it will import the eight labelled messages on the first sync.

The Gmail search query is editable in `config.toml`, for example `label:SEEK`. Gmail supports label and sender search syntax in API queries; use a Gmail label to keep the importer focused on SEEK alerts. The importer reads direct `/job/<id>` links and the title, employer, and location lines in the plain-text alert. It skips logo tracking links and alert footer buttons, and merges repeated listings across alerts and job boards. SEEK alert emails contain summary cards rather than the full job description, so the description dropdown may be empty or show only the alert snippet; open the SEEK link for the full listing. Emails that have not produced any job links are retried on the next sync. When the parser is updated, previously imported alerts are reprocessed automatically; you do not need to clear the database. If the consent screen is in Testing, Google limits access to listed test users and Gmail-scope refresh tokens expire after seven days, so you may need to reconnect weekly. Google may show an unverified-app notice while this private project is in Testing. See Google's [Gmail API Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python), [message search guide](https://developers.google.com/workspace/gmail/api/guides/filtering), [scope guide](https://developers.google.com/workspace/gmail/api/auth/scopes), and [OAuth token guidance](https://developers.google.com/identity/protocols/oauth2).

Message bodies are parsed in memory; the app saves extracted job fields and Gmail message IDs locally, not full email bodies. OAuth client and token files are ignored by Git. **Trash old imported alerts** moves only previously imported SEEK messages older than `gmail.stale_after_days` (90 days by default) to Gmail Trash. It requires confirmation and uses Gmail's recoverable Trash behavior.

## Search settings

Edit `config.toml` to change the titles, locations, sources, and result limits. Defaults are Graduate Lawyer, Junior Lawyer, and Associate Lawyer in Perth, Melbourne, Sydney, and Brisbane, using Indeed and Glassdoor. The initial import requests up to 250 results per title/location pair across the enabled sites.

Availability checks visit saved Indeed, Glassdoor, and SEEK listing URLs. A 404/410 response or a clear “no longer available” message confirms expiry; a board block, timeout, ambiguous page, unsupported source, or missing URL keeps the job. A partial reset first reruns the full configured search, deduplicates/upserts its results, then checks saved source URLs; absence from a capped search alone never deletes a job. Some sites may block checks, which means the app will retain uncertain listings.

Job board results may be incomplete or blocked. LinkedIn scraping is disabled. SEEK imports use Gmail alerts; hosting and outbound email notifications are not configured.

## Reset

Use **Reset job search → Full reset** in the UI to clear saved jobs, statuses, notes, source links, Latest history, and Gmail message import history while retaining the Gmail authorization and smart-filter profile. To clear data manually, close the app and remove the SQLite files:

```sh
rm -f data/jobs.sqlite3 data/jobs.sqlite3-shm data/jobs.sqlite3-wal
```

The next launch creates a new database and runs the initial import. The database file is tracked by Git in this repository; don't commit personal job data.

The smart filter profile is stored separately and remains when the database is cleared. Remove it too only if you want to reset your profile:

```sh
rm -f data/smart_profile.json
```

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
Job Finder is a local Python job search and application tracker for Australia. Launch with `python -m job_finder start`; it starts a local Flask server and opens the browser UI. Keep the terminal open and stop the server with Ctrl+C. The first launch imports configured jobs once into SQLite and the Deleted raw archive; the fit filter routes suitable roles into Backlog. Later launches do not search automatically. The future AI ranking prompt and JSON Schema are in `prompts/job_fit_ranker.md` and `prompts/job_fit_result.schema.json`; they are design references only, not connected to the running app.

The left navigation is ordered Latest, Applied, Backlog, Deleted. Every unique fetched listing is kept in SQLite and visible in Deleted as a raw archive after duplicate merging; each row shows whether it is in Backlog, Applied, or set aside as low fit. Low-fit jobs can be restored to Backlog. Check listings and partial reset use saved Indeed, Glassdoor, and SEEK URLs and remove only clearly confirmed expired listings, regardless of application status; blocked or ambiguous checks keep jobs. Full reset clears saved job records and search history, then runs the initial import again. These controls preserve the separate candidate profile in ignored `data/smart_profile.json`. Job cards stay within the page width; status, notes, and Save controls sit together, and descriptions expand across the full card below. The UI also has a location dropdown and text search, manual Latest refresh, and a local smart fit filter. Explicit seniority mismatches above the profile PQE limit are excluded from Backlog. Junior signals are marked Likely fit; unclear jobs remain Review. No external AI service is used.

Latest searches since the initial import and then since the last successful refresh, keeps up to 26 visible jobs, and marks the newest batch with blue dots. User confirmation appears if the refresh would put the total above 25; removed Latest items remain in Backlog and SQLite.

Defaults in config.toml: Graduate Lawyer, Junior Lawyer, Associate Lawyer; Perth, Melbourne, Sydney, Brisbane; Indeed and Glassdoor. Initial import cap is 250 per title/location pair across enabled sources. LinkedIn scraping is disabled. SEEK can be imported manually from messages labelled `SEEK` in Gmail after local OAuth setup. Hosting and outbound email alerts are not configured.

Gmail sync imports SEEK job links from the configured Gmail query (`label:SEEK` by default) into the same SQLite pool and deduplicates them with other sources. Gmail OAuth client/token files stay in ignored `data/`; old imported alert emails can be manually moved to Trash after the configured age. Full reset clears the local imported-message IDs but retains the OAuth token.

SQLite data persists across app closes. Preserve it unless the user requests a reset, and never commit personal job data.
```

## Later ideas

- Consider an optional AI review after local screening, with an explicit privacy and cost choice.
- Prioritisation
- Look into free hosting once the local app flow is stable.
- Check saved Indeed/Glassdoor/SEEK listing URLs and remove only confirmed expired listings.

## Project maintenance

Update this README in the same change whenever app behavior, setup steps, dependencies, data storage, or commands change. Keep the install/start instructions and AI handoff aligned with the implementation.
