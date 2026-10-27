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

The start command opens Job Finder at `http://127.0.0.1:8765`. Leave the terminal window open while using it; press `Ctrl+C` there to stop the server. The first launch runs one initial search and saves each unique result once in Backlog. Later launches load saved jobs without searching automatically.

## Use the app

- **Latest** shows up to 30 jobs added by **Check listings**. A blue dot marks jobs from the most recent check.
- **Prioritized** shows up to 30 jobs: manual picks first, then highest-scoring un-applied jobs after you run AI ranking. AI ranking itself does not move, archive, or delete jobs.
- **Applied** tracks roles marked applied, interviewing, rejected, or closed.
- **Backlog** holds new and saved roles.
- **Deleted** holds jobs you manually move out of consideration. It is separate from Backlog and Prioritized. **Clear deleted jobs** permanently removes only jobs in this list, along with their saved links and AI assessments.
- Job-board links are grouped by source. If multiple SEEK URLs were merged into one role, expand the SEEK count to open each listing.
- **Check listings** (in the left sidebar) runs a recent Indeed and Glassdoor search, imports labelled SEEK alert emails from Gmail, checks saved source URLs for expiry, and adds new active jobs to Latest. It requests up to 1,500 Indeed/Glassdoor results total by default (configurable from 1,000 to 2,000) and keeps up to 30 jobs in Latest. The completion log shows expired jobs deleted, Indeed/Glassdoor additions, and SEEK additions. Listings are deleted only when a board clearly confirms they have expired; blocked or unclear results are kept.
- **Reset job search** offers a partial reset that reruns the full initial pool search, keeps/deduplicates existing jobs, then checks saved source URLs for expiry. Full reset clears saved jobs, notes, statuses, source links, and Latest history, then runs a fresh initial search. Both preserve the separate AI ranking profile.
- Filter any list by location or search text. Use a job's dropdown to move it to Backlog, Prioritized, or Deleted, or to update its application status; notes are saved on the same card.
- Job cards stay within the page width. Status, notes, and Save controls sit together; **View job description** expands across the full card below them. Dropdown indicators use the same small, rounded chevron style.
- The left navigation is ordered **Prioritized**, **Latest**, **Applied**, **Backlog**, **Deleted**. Check listings also updates Latest; there is no separate Latest refresh action. Older items trimmed from Latest stay in SQLite with their current list placement.

## AI Prioritized tab

The **Prioritized** tab shows up to 30 jobs: manual picks appear first, followed by the highest AI scores. AI ranking assesses open jobs against your circumstances and optional resume summary; the assessment itself does not move jobs. Use each job's dropdown to move it between Backlog, Prioritized, and Deleted, or to update its application status. Moving an applied job to one of those lists reopens it as a new job.

To enable it, create an [OpenAI API key](https://platform.openai.com/api-keys) and paste it into the Prioritized tab, or put it after `OPENAI_API_KEY=` in the project-root `.env` file. API usage is billed separately from a ChatGPT subscription. The `.env` file is ignored by Git and saved with owner-only permissions. A shell-provided `OPENAI_API_KEY` takes precedence. Never commit or share `.env`.

AI ranking runs only when you press **Prioritize top 30**. Before sending new or changed jobs, the app asks for confirmation. It sends the candidate circumstances, maximum required PQE, allowed locations, optional resume summary, and job title, employer, location, posting date, and scraped description to OpenAI. Descriptions are limited to 12,000 characters; source URLs, application notes, and Gmail contents are not sent. The OpenAI API processes this data and charges apply under that API account. Jobs are assessed in batches; successful batches are saved in SQLite so an interrupted ranking can resume. Assessments are reused until the job, profile, allowed locations, model, prompt, or rubric changes. Moving a job to Deleted removes it from future ranking candidates. Full reset clears the assessments with the jobs.

The app uses the OpenAI Responses API with Structured Outputs to require a validated JSON assessment per job. The default model and batch size are in `[ai]` in `config.toml`. The scoring prompt and response contract are [`prompts/job_fit_ranker.md`](prompts/job_fit_ranker.md) and [`prompts/job_fit_result.schema.json`](prompts/job_fit_result.schema.json).

## Secret keys in `.env`

Keep API keys and Google OAuth client credentials in the project-root `.env` file. The tracked [`.env.example`](.env.example) lists supported variable names without real secrets. For a fresh checkout, copy it to `.env` and fill the values; never commit `.env` or paste its contents into a chat.

The Prioritized tab can save `OPENAI_API_KEY` into `.env` for you. Gmail setup uses `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`; optional Google project and endpoint fields are also listed in `.env.example`. This project’s Google OAuth client credentials have been migrated into `.env`. Gmail's generated refresh token remains in the ignored `data/gmail_token.json` file because the OAuth library updates it when access is renewed. Both `.env` and the token file stay on this Mac.

## SEEK alerts via Gmail

**Check listings** reads messages matching `gmail.query` in `config.toml` (default `label:SEEK`), imports SEEK job links into the same SQLite job pool, and merges duplicates with Indeed/Glassdoor results. There is no separate SEEK sync panel or button. The first sync can import already-labelled messages as well as future alerts.

### One-time Gmail setup

1. In Gmail, select the eight existing SEEK alerts and use **Label as → Create new → SEEK**. To label future alerts automatically, open one alert and choose **More → Filter messages like this**, then create a filter that applies the `SEEK` label. You can also choose “Also apply filter to matching conversations” to label matching messages already in Gmail.
2. Open [Google Cloud Console](https://console.cloud.google.com/). Create a project, enable **Gmail API**, and configure **Google Auth Platform**. Set the audience to **External**, leave publishing status as **Testing**, and add the Gmail account you will connect as a test user.
3. In **Data Access**, add the scope `https://www.googleapis.com/auth/gmail.modify`. Google classifies it as a restricted, broad Gmail scope. The app uses it to read labelled alert messages and does not send email.
4. In **Clients**, create an OAuth client with application type **Desktop app**. Copy its `client_id` and `client_secret` into `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`; copy `project_id` into `GOOGLE_PROJECT_ID` if present. The app uses Google's local loopback OAuth flow, so you do not need to configure a redirect URI. Keep the downloaded client JSON out of Git and delete the downloaded copy after moving its credentials.
5. Install the project requirements and start the app. Open `http://127.0.0.1:8765/gmail/oauth/connect`, sign in to the Gmail account, and approve access. The next time you press **Check listings**, it will import the labelled messages. Use the same URL to reconnect if Gmail authorization expires.

The Gmail search query is editable in `config.toml`, for example `label:SEEK`. Gmail supports label and sender search syntax in API queries; use a Gmail label to keep the importer focused on SEEK alerts. The importer reads direct `/job/<id>` links and the title, employer, and location lines in the plain-text alert. It skips logo tracking links and alert footer buttons, and merges repeated listings across alerts and job boards. SEEK alert emails contain summary cards rather than the full job description, so the description dropdown may be empty or show only the alert snippet; open the SEEK link for the full listing. Emails that have not produced any job links are retried the next time you press **Check listings**. When the parser is updated, previously imported alerts are reprocessed automatically; you do not need to clear the database. If the consent screen is in Testing, Google limits access to listed test users and Gmail-scope refresh tokens expire after seven days, so you may need to reconnect weekly. Google may show an unverified-app notice while this private project is in Testing. See Google's [Gmail API Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python), [message search guide](https://developers.google.com/workspace/gmail/api/guides/filtering), [scope guide](https://developers.google.com/workspace/gmail/api/auth/scopes), and [OAuth token guidance](https://developers.google.com/identity/protocols/oauth2).

Message bodies are parsed in memory; the app saves extracted job fields and Gmail message IDs locally, not full email bodies. OAuth client secrets in `.env` and the generated Gmail token file are ignored by Git.

## Search settings

Edit `config.toml` to change the titles, locations, sources, and result limits. Defaults are Graduate Lawyer, Junior Lawyer, and Associate Lawyer in Perth, Melbourne, Sydney, and Brisbane, using Indeed and Glassdoor. The initial import requests up to 250 results per title/location pair across the enabled sites. **Check listings** uses `search.check_results_total` (1,500 by default, limited to 1,000–2,000) and shares that request budget across each title, location, and enabled site. It searches at least the configured `search.hours_old` window (24 hours by default), and automatically widens that window to cover time since the last successful board refresh.

Availability checks visit saved Indeed, Glassdoor, and SEEK listing URLs. A 404/410 response or a clear “no longer available” message confirms expiry; a board block, timeout, ambiguous page, unsupported source, or missing URL keeps the job. A partial reset first reruns the full configured search, deduplicates/upserts its results, then checks saved source URLs; absence from a capped search alone never deletes a job. Some sites may block checks, which means the app will retain uncertain listings.

Job board results may be incomplete or blocked. LinkedIn scraping is disabled. SEEK imports use Gmail alerts; hosting and outbound email notifications are not configured.

## Reset

Use **Reset job search → Full reset** in the UI to clear saved jobs, statuses, notes, source links, Latest history, and Gmail message import history while retaining the Gmail authorization and AI ranking profile. It also preserves `.env`. To clear job data manually, close the app and remove the SQLite files:

```sh
rm -f data/jobs.sqlite3 data/jobs.sqlite3-shm data/jobs.sqlite3-wal
```

The next launch creates a new database and runs the initial import. The local database is ignored by Git because it can contain personal job and Gmail metadata. Keep it on your machine and don't commit or share it.

The AI ranking profile is stored separately and remains when the database is cleared. Remove it too only if you want to reset your profile:

```sh
rm -f data/candidate_profile.json
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
Job Finder is a local Python job search and application tracker for Australia. Launch with `python -m job_finder start`; it starts a local Flask server and opens the browser UI. Keep the terminal open and stop the server with Ctrl+C. The first launch imports the configured pool. Later launches do not search automatically.

Navigation is Prioritized, Latest, Applied, Backlog, Deleted. Each unique job is saved once in SQLite and starts in Backlog. Moving a job to Deleted removes it from Backlog, Prioritized, and Latest. Clearing Deleted permanently removes only those jobs and their related links/assessments. The Check listings button searches Indeed and Glassdoor across configured titles and locations, requests up to 1,500 results total by default (configurable from 1,000 to 2,000), imports SEEK jobs from labelled Gmail alerts, then checks all saved source links and removes only jobs clearly confirmed expired. It displays separate logs for expired jobs deleted, Indeed/Glassdoor additions, SEEK additions, and jobs added to Latest. The board search covers at least the most recent 24 hours and widens to cover time since the previous successful board refresh. Ambiguous expiry checks keep the job. The Gmail connection is local OAuth; the user must connect it first. The button also works without Gmail and logs that SEEK sync was skipped.

Full reset clears saved jobs and search history before running the initial import. Partial reset reruns the full initial pool search and then checks expiry. Both keep Gmail authorization, `.env`, and the AI ranking profile in `data/candidate_profile.json`. The local UI also has text and location filters, Check listings (which updates Latest), a Deleted-only clear action, list placement and application-status controls, notes, and expandable job descriptions.

The Prioritized tab can contain manual picks and the top AI-ranked open jobs. AI ranking sends open un-applied job details and the AI candidate profile, including an optional resume summary, to OpenAI. Ranking does not itself move or delete jobs. OpenAI API billing is separate from ChatGPT. AI assessments use `prompts/job_fit_ranker.md` and are cached in SQLite by job/profile/allowed-locations/model/prompt version.

Defaults in `config.toml`: Graduate Lawyer, Junior Lawyer, Associate Lawyer; Perth, Melbourne, Sydney, Brisbane; Indeed and Glassdoor. Initial import cap is 250 results per title/location pair across enabled sources. LinkedIn scraping is disabled. Gmail queries `label:SEEK` by default; old imported alert emails can be moved to Gmail Trash with the separate cleanup action. Hosting and outbound email alerts are not configured.

SQLite data persists across app closes. Preserve it unless the user requests a reset, and never commit personal job data.
```

## Later ideas

- Build a small labeled set to review AI ranking quality and tune its scoring rubric.
- Look into free hosting once the local app flow is stable.
- Bot uses common sense when applying priorotisation
- bot searches internet to filter the best roles for the graduate/job request 
- Chnage priorotized AI sign

## Project maintenance

Update this README in the same change whenever app behavior, setup steps, dependencies, data storage, or commands change. Keep the install/start instructions and AI handoff aligned with the implementation.
