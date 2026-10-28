from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from job_finder.database import (
    connect,
    get_metadata,
    list_job_sources,
    list_jobs,
    list_tab_jobs,
    latest_job_count,
    save_latest_jobs,
    set_metadata,
    set_note,
    set_status,
    upsert_jobs,
    upsert_jobs_with_ids,
)
from job_finder.runner import (
    INITIAL_IMPORT_KEY,
    LATEST_REFRESH_KEY,
    commit_latest_refresh,
    preview_latest_refresh,
    run_search,
)
from job_finder.search import SearchBatch


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.connection = connect(Path(self.temp.name) / "jobs.sqlite3")

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def test_cross_source_duplicate_preserves_status_and_all_links(self):
        indeed = {
            "site": "indeed",
            "title": "Graduate Lawyer",
            "company": "Acme Legal",
            "location": "Perth, WA, Australia",
            "job_url": "https://indeed.com/viewjob?jk=abc&utm_source=search",
            "date_posted": "2026-10-20",
        }
        glassdoor = {
            **indeed,
            "site": "glassdoor",
            "title": " graduate-lawyer ",
            "job_url": "https://glassdoor.com/job/abc?ref=search",
        }

        self.assertEqual(upsert_jobs(self.connection, [indeed]), (1, 0))
        self.assertTrue(set_status(self.connection, 1, "applied"))
        self.assertTrue(set_note(self.connection, 1, "Followed up"))
        self.assertEqual(upsert_jobs(self.connection, [glassdoor]), (0, 1))

        jobs = list_jobs(self.connection)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "applied")
        self.assertEqual(jobs[0]["notes"], "Followed up")
        self.assertEqual(len(list_job_sources(self.connection, jobs[0]["id"])), 2)

    def test_existing_database_is_migrated_and_source_link_backfilled(self):
        self.connection.close()
        legacy = sqlite3.connect(Path(self.temp.name) / "legacy.sqlite3")
        legacy.execute(
            """CREATE TABLE jobs (
                id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL, company TEXT, location TEXT, source TEXT, url TEXT,
                description TEXT, date_posted TEXT, first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
                notes TEXT NOT NULL DEFAULT '')"""
        )
        legacy.execute(
            """INSERT INTO jobs(fingerprint,title,company,location,source,url,first_seen,last_seen)
               VALUES ('url:https://indeed.com/job/1','Graduate Lawyer','Acme','Perth','indeed',
                       'https://indeed.com/job/1','2026-10-20','2026-10-20')"""
        )
        legacy.commit()
        legacy.close()

        self.connection = connect(Path(self.temp.name) / "legacy.sqlite3")
        jobs = list_jobs(self.connection)
        self.assertTrue(jobs[0]["match_key"])
        self.assertEqual(len(list_job_sources(self.connection, jobs[0]["id"])), 1)

    def test_latest_feed_keeps_26_and_marks_only_recent_batch(self):
        records = [
            {"site": "indeed", "title": f"Role {index}", "company": "Acme", "location": "Perth", "job_url": f"https://example.com/{index}"}
            for index in range(28)
        ]
        _, _, job_ids = upsert_jobs_with_ids(self.connection, records)
        first_batch_time = "2026-10-23T00:00:00+00:00"
        save_latest_jobs(self.connection, job_ids[:26], first_batch_time)
        self.assertEqual(len(list_tab_jobs(self.connection, "latest", limit=100)), 26)

        second_batch_time = "2026-10-23T01:00:00+00:00"
        save_latest_jobs(self.connection, job_ids[26:], second_batch_time)
        latest = list_tab_jobs(self.connection, "latest", limit=100)
        self.assertEqual(len(latest), 26)
        self.assertEqual(sum(bool(job["latest_new"]) for job in latest), 2)
        self.assertEqual(get_metadata(self.connection, "last_latest_refresh_at"), second_batch_time)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.connection = connect(Path(self.temp.name) / "jobs.sqlite3")
        self.config = {
            "titles": ["Graduate Lawyer", "Junior Lawyer"],
            "locations": ["Perth", "Sydney"],
            "sites": ["indeed", "glassdoor"],
            "results_per_site": {"indeed": 25, "glassdoor": 25},
            "hours_old": 24,
            "initial_import_enabled": True,
            "initial_results_per_title_location": 250,
        }

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def test_initial_import_runs_once_then_daily_searches(self):
        with patch("job_finder.runner.search_jobs", return_value=SearchBatch([], {})) as search:
            first = run_search(self.connection, self.config)
            self.assertTrue(first.initial_import)
            self.assertEqual(search.call_count, 8)  # 2 titles x 2 locations x 2 sources
            self.assertEqual(search.call_args_list[0].args[3], 125)
            self.assertIsNone(search.call_args_list[0].kwargs["hours_old"])
            self.assertIsNotNone(get_metadata(self.connection, INITIAL_IMPORT_KEY))

            search.reset_mock()
            second = run_search(self.connection, self.config)
            self.assertFalse(second.initial_import)
            self.assertEqual(search.call_count, 8)  # one request per title/location/site
            self.assertEqual(search.call_args_list[0].kwargs["hours_old"], 24)
            self.assertEqual(search.call_args_list[0].kwargs["per_site"], 25)

    def test_failed_initial_import_does_not_mark_seed_complete(self):
        with patch(
            "job_finder.runner.search_jobs",
            return_value=SearchBatch([], {"indeed": "temporary error"}),
        ):
            summary = run_search(self.connection, self.config)
        self.assertTrue(summary.errors)
        self.assertIsNone(get_metadata(self.connection, INITIAL_IMPORT_KEY))

    def test_latest_preview_uses_seed_or_last_refresh_time_and_commits_new_jobs(self):
        baseline = (datetime.now(timezone.utc) - timedelta(hours=2, minutes=1)).isoformat()
        set_metadata(self.connection, INITIAL_IMPORT_KEY, baseline)
        candidate = {
            "site": "indeed",
            "title": "Graduate Lawyer",
            "company": "New Legal",
            "location": "Perth, WA, Australia",
            "job_url": "https://example.com/new-role",
        }
        batches = [SearchBatch([candidate], {})] + [SearchBatch([], {}) for _ in range(7)]
        with patch("job_finder.runner.search_jobs", side_effect=batches) as search:
            preview = preview_latest_refresh(self.connection, self.config)
        self.assertEqual(preview.hours_old, 3)
        self.assertEqual(len(preview.jobs), 1)
        self.assertEqual(search.call_count, 8)
        self.assertEqual(commit_latest_refresh(self.connection, preview.jobs), 1)
        self.assertEqual(latest_job_count(self.connection), 1)
        self.assertIsNotNone(get_metadata(self.connection, LATEST_REFRESH_KEY))


if __name__ == "__main__":
    unittest.main()
