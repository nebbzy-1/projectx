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
- **Prioritized** shows up to 50 jobs: manual picks first, then the highest-scoring open jobs after Codex prioritization. Clear mandatory mismatches found in Backlog are moved to Deleted; you can move them back.
- **Applied** tracks roles marked applied, interviewing, rejected, or closed.
- **Backlog** holds new and saved roles.
- **Deleted** holds jobs you manually move out of consideration and clear mandatory mismatches found by Codex. Items remain recoverable until you use **Clear deleted jobs**, which permanently removes them and their saved links and assessments.
- **Profile** sits below the job lists and above **Check listings** in the sidebar. It lets you edit candidate details used by Codex prioritization plus search titles and locations. Candidate details are saved locally in `data/candidate_profile.json`; title/location edits are saved in `data/search_settings.json`.
- Job-board links are grouped by source. If multiple SEEK URLs were merged into one role, expand the SEEK count to open each listing.
- Prioritized cards also retain each job's saved note, source links, and Latest dot.
- **Check listings** (in the left sidebar) runs a recent Indeed and Glassdoor search, imports labelled SEEK alert emails from Gmail, checks saved source URLs for expiry, and adds new active jobs to Latest. It requests up to 1,500 Indeed/Glassdoor results total by default (configurable from 1,000 to 2,000) and keeps up to 30 jobs in Latest. The completion log shows expired jobs deleted, Indeed/Glassdoor additions, and SEEK additions. Listings are deleted only when a board clearly confirms they have expired; blocked or unclear results are kept.
- Completed operation messages stay visible on the current page and clear when you switch job-list tabs. The browser checks task status only while a background task is running.
- **Reset job search** offers a partial reset that reruns the full initial pool search, keeps/deduplicates existing jobs, then checks saved source URLs for expiry. Full reset clears saved jobs, notes, statuses, source links, and Latest history, then runs a fresh initial search. Both preserve the separate AI ranking profile.
- Filter any list by location or search text. Use a job's dropdown to move it to Backlog, Prioritized, or Deleted, or to update its application status; notes are saved on the same card.
- Job cards stay within the page width. Status, notes, and Save controls sit together; **View job description** expands across the full card below them. Dropdown indicators use the same small, rounded chevron style.
- The left navigation is ordered **Prioritized**, **Latest**, **Applied**, **Backlog**, **Deleted**. The **Profile** button is below the lists and above **Check listings**. Check listings also updates Latest; there is no separate Latest refresh action. Older items trimmed from Latest stay in SQLite with their current list placement.

## AI Prioritized tab

The **Prioritized** tab shows up to 50 jobs: manual picks appear first, followed by Codex's highest-scoring open jobs. Codex reads the candidate details saved on **Profile**. It moves only clearly confirmed mandatory mismatches from Backlog to Deleted; it leaves manually prioritized jobs in place. These moves are recoverable. Use each job's dropdown to move jobs between lists or update application status. Moving an applied job to a list reopens it as a new job.

Install and sign in to the [Codex CLI](https://learn.chatgpt.com/docs/codex/cli) on this Mac. If needed, install it with `npm install -g @openai/codex`, then run `codex login`. Job Finder finds `codex` on `PATH` or in common install locations; restart the app after installing if the button still says the CLI is needed. The Prioritized tab does not need an OpenAI API key. It uses the authentication and usage limits configured for the Codex CLI.

Press **Prioritize top 50** to start the backend run. The app asks for confirmation before sending your saved profile, optional resume summary, and open job details to your signed-in Codex CLI session. Codex first screens all open job titles, then reviews descriptions for up to 100 shortlisted records in groups of 50. It tries to assess every role, accepts partial results, combines usable assessments, and displays the best 50. Live web search is enabled for the first detailed group only, for up to 10 employer checks. A mandatory PQE requirement beyond the candidate's maximum is a blocker; preferred PQE, ambiguous titles, and unclear or conflicting locations are not. Only detailed-review blockers on Backlog jobs are moved to Deleted. Jobs outside the shortlist and manual Prioritized picks stay where they are. The maximum PQE value remains internal and is not editable on the Profile page.

Job text and candidate data are processed by the Codex CLI session. The app does not send source URLs, application notes, Gmail messages, or Gmail metadata. It removes `OPENAI_API_KEY` from the Codex subprocess environment so an old project API key cannot silently switch this workflow to API-key usage. Codex's configured authentication and usage limits apply. The prioritizer uses read-only sandbox settings and a temporary directory. It returns only compact scores, fit tiers, confidence, and blocker flags; no written rationale is saved. It accepts partial results, ignores duplicate, unknown, or malformed entries, and keeps still-valid old scores for jobs omitted from a partial response. The app sorts valid scores itself. If there are no usable results, existing priorities stay untouched. Confirmed blockers and new assessments are saved together, then up to 50 remaining jobs appear in Prioritized. A detailed-review blocker moves a Backlog job to Deleted; low scores and title-only exclusions do not. If Codex returns invalid output that stops processing, the raw response is printed in the terminal. The model defaults to `gpt-6-luna` in `[ai]` in `config.toml`. The ranking instructions and output schemas are [`prompts/job_fit_ranker.md`](prompts/job_fit_ranker.md), [`prompts/job_fit_shortlist.schema.json`](prompts/job_fit_shortlist.schema.json), and [`prompts/job_fit_result.schema.json`](prompts/job_fit_result.schema.json).

## Secret keys in `.env`

Keep Google OAuth client credentials in the project-root `.env` file. The tracked [`.env.example`](.env.example) lists supported variable names without real secrets. For a fresh checkout, copy it to `.env` and fill the values; never commit `.env` or paste its contents into a chat.

Gmail setup uses `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`; optional Google project and endpoint fields are also listed in `.env.example`. This project’s Google OAuth client credentials have been migrated into `.env`. Gmail's generated refresh token remains in the ignored `data/gmail_token.json` file because the OAuth library updates it when access is renewed. Both `.env` and the token file stay on this Mac.

## SEEK alerts via Gmail

**Check listings** reads messages matching the SEEK Gmail query on the Profile page (default `label:SEEK`), imports SEEK job links into the same SQLite job pool, and merges duplicates with Indeed/Glassdoor results. There is no separate SEEK sync panel or button. The first sync can import already-labelled messages as well as future alerts.

### One-time Gmail setup

1. In Gmail, select the eight existing SEEK alerts and use **Label as → Create new → SEEK**. To label future alerts automatically, open one alert and choose **More → Filter messages like this**, then create a filter that applies the `SEEK` label. You can also choose “Also apply filter to matching conversations” to label matching messages already in Gmail.
2. Open [Google Cloud Console](https://console.cloud.google.com/). Create a project, enable **Gmail API**, and configure **Google Auth Platform**. Set the audience to **External**, leave publishing status as **Testing**, and add the Gmail account you will connect as a test user.
3. In **Data Access**, add the scope `https://www.googleapis.com/auth/gmail.modify`. Google classifies it as a restricted, broad Gmail scope. The app uses it to read labelled alert messages and does not send email.
4. In **Clients**, create an OAuth client with application type **Desktop app**. Copy its `client_id` and `client_secret` into `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`; copy `project_id` into `GOOGLE_PROJECT_ID` if present. The app uses Google's local loopback OAuth flow, so you do not need to configure a redirect URI. Keep the downloaded client JSON out of Git and delete the downloaded copy after moving its credentials.
5. Install the project requirements and start the app. Open `http://127.0.0.1:8765/gmail/oauth/connect`, sign in to the Gmail account, and approve access. The next time you press **Check listings**, it will import the labelled messages. Use the same URL to reconnect if Gmail authorization expires.

The Gmail search query defaults to `label:SEEK` and is configured in `config.toml`. Gmail supports label and sender search syntax in API queries; use a Gmail label to keep the importer focused on SEEK alerts. The importer reads direct `/job/<id>` links and the title, employer, and location lines in the plain-text alert. It skips logo tracking links and alert footer buttons, and merges repeated listings across alerts and job boards. SEEK alert emails contain summary cards rather than the full job description, so the description dropdown may be empty or show only the alert snippet; open the SEEK link for the full listing. Emails that have not produced any job links are retried the next time you press **Check listings**. When the parser is updated, previously imported alerts are reprocessed automatically; you do not need to clear the database. If the consent screen is in Testing, Google limits access to listed test users and Gmail-scope refresh tokens expire after seven days, so you may need to reconnect weekly. Google may show an unverified-app notice while this private project is in Testing. See Google's [Gmail API Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python), [message search guide](https://developers.google.com/workspace/gmail/api/guides/filtering), [scope guide](https://developers.google.com/workspace/gmail/api/auth/scopes), and [OAuth token guidance](https://developers.google.com/identity/protocols/oauth2).

Message bodies are parsed in memory; the app saves extracted job fields and Gmail message IDs locally, not full email bodies. OAuth client secrets in `.env` and the generated Gmail token file are ignored by Git.

## Search settings

Use the **Profile** page to change job titles and locations. The page shows Indeed and Glassdoor sources; other search settings remain in `config.toml` at their defaults: 25 results per board search, 24-hour minimum search window, 1,500 Check listings cap, 250 initial results per title/location pair, initial import enabled, up to 250 SEEK emails per sync, and `label:SEEK`. The AI model defaults to `gpt-6-luna`. Title/location edits are saved locally in `data/search_settings.json` and apply to the next search. The initial import cap is used by the first seed or a partial reset; changing titles or locations does not rerun or clear the existing database. The defaults in `config.toml` are Graduate Lawyer, Junior Lawyer, and Associate Lawyer in Perth, Melbourne, Sydney, and Brisbane. **Check listings** shares its 1,500 result budget across each title, location, and enabled site. It searches at least the configured 24-hour window and automatically widens that window to cover time since the last successful board refresh. To restore the default titles and locations, close the app and remove `data/search_settings.json`.

Availability checks visit saved Indeed, Glassdoor, and SEEK listing URLs. A 404/410 response or a clear “no longer available” message confirms expiry; a board block, timeout, ambiguous page, unsupported source, or missing URL keeps the job. A partial reset first reruns the full configured search, deduplicates/upserts its results, then checks saved source URLs; absence from a capped search alone never deletes a job. Some sites may block checks, which means the app will retain uncertain listings.

Job board results may be incomplete or blocked. LinkedIn scraping is disabled. SEEK imports use Gmail alerts; hosting and outbound email notifications are not configured.

## Reset

Use **Reset job search → Full reset** in the UI to clear saved jobs, statuses, notes, source links, Latest history, and Gmail message import history while retaining the Gmail authorization, AI ranking profile, search settings, and `.env`. To clear job data manually, close the app and remove the SQLite files:

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

Navigation is Prioritized, Latest, Applied, Backlog, Deleted. The Profile button appears below the job lists and above Check listings. It edits candidate circumstances and optional resume summary, saved in `data/candidate_profile.json`; the current AI prioritizer reads this profile, which is also intended to guide a later backlog-wide AI filter. The existing maximum PQE value is retained internally and is not editable on the page. Only search titles and locations are editable in Profile and are saved in `data/search_settings.json`. Sources and other search/AI limits stay at defaults in `config.toml`. Each unique job is saved once in SQLite and starts in Backlog. Moving a job to Deleted removes it from Backlog, Prioritized, and Latest. Clearing Deleted permanently removes only those jobs and their related links/assessments. The Check listings button searches Indeed and Glassdoor across configured titles and locations, requests up to 1,500 results total, imports SEEK jobs from labelled Gmail alerts, then checks all saved source links and removes only jobs clearly confirmed expired. It displays separate logs for expired jobs deleted, Indeed/Glassdoor additions, SEEK additions, and jobs added to Latest. The board search covers at least the most recent 24 hours and widens to cover time since the previous successful board refresh. Ambiguous expiry checks keep the job. The Gmail connection is local OAuth; the user must connect it first. The button also works without Gmail and logs that SEEK sync was skipped.

Full reset clears saved jobs and search history before running the initial import. Partial reset reruns the full initial pool search and then checks expiry. Both keep Gmail authorization, `.env`, and the AI ranking profile in `data/candidate_profile.json`. The local UI also has text and location filters, Check listings (which updates Latest), a Deleted-only clear action, list placement and application-status controls, notes, and expandable job descriptions.

The Prioritized tab can contain manual picks and the top 50 Codex-ranked open jobs. Candidate circumstances and optional resume summary are edited on Profile and saved to `data/candidate_profile.json`; the maximum PQE value remains internal. Profile also edits search titles and locations in `data/search_settings.json`; other settings stay at defaults in `config.toml`. Codex screens all open job titles, then reviews descriptions for up to 100 plausible roles and returns compact results. Clear mandatory mismatches found in Backlog move to Deleted; manual picks stay in place. The bot does not permanently delete jobs. Results are validated and stored in SQLite; the prompt and output schemas are in `prompts/job_fit_ranker.md` and the two schema files.

Defaults in `config.toml`: Graduate Lawyer, Junior Lawyer, Associate Lawyer; Perth, Melbourne, Sydney, Brisbane; Indeed and Glassdoor. Initial import cap is 250 results per title/location pair across enabled sources. LinkedIn scraping is disabled. Gmail queries `label:SEEK` by default; old imported alert emails can be moved to Gmail Trash with the separate cleanup action. Hosting and outbound email alerts are not configured.

SQLite data persists across app closes. Preserve it unless the user requests a reset, and never commit personal job data.
```

## Later ideas

- Build a small labeled set to review AI ranking quality and tune its scoring rubric.
- Look into free hosting once the local app flow is stable.

## Project maintenance

Update this README in the same change whenever app behavior, setup steps, dependencies, data storage, or commands change. Keep the install/start instructions and AI handoff aligned with the implementation.
