from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from voicevault.app_db import AppDatabase
from voicevault.index_jobs import ActiveIndexJobExists, IndexJobService
from voicevault.index_service import IndexBuildResult
from voicevault.person_archive import PersonRepository


NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


class FakeIndexService:
    def __init__(self, result: IndexBuildResult) -> None:
        self.result = result
        self.people: list[str] = []

    def rebuild_person(self, person_id: str) -> IndexBuildResult:
        self.people.append(person_id)
        return self.result


class IndexJobServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = AppDatabase(data_dir=Path(self.temp_dir.name))
        self.database.initialize()
        self.person = PersonRepository(self.database).create("Alice")
        self.builder = FakeIndexService(
            IndexBuildResult("degraded", "fulltext_only", "generation-a", None)
        )
        self.service = IndexJobService(self.database, self.builder, clock=lambda: NOW)

    def test_active_job_is_unique_and_degraded_build_succeeds_fulltext_only(self) -> None:
        pending = self.service.create(self.person.person_id)
        with self.assertRaises(ActiveIndexJobExists):
            self.service.create(self.person.person_id)

        completed = self.service.run(pending.job_id)
        replacement = self.service.create(self.person.person_id)

        self.assertEqual((completed.status, completed.retrieval_mode), ("succeeded", "fulltext_only"))
        self.assertEqual(completed.generation_id, "generation-a")
        self.assertEqual(self.builder.people, [self.person.person_id])
        self.assertEqual(replacement.status, "pending")
        self.assertEqual(self.service.get(completed.job_id), completed)

    def test_restart_reconciles_only_running_and_terminal_resources_are_stable(self) -> None:
        running = self.service.create(self.person.person_id)
        other = PersonRepository(self.database).create("Bob")
        pending = self.service.create(other.person_id)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE index_jobs SET status = 'running', started_at = ? WHERE job_id = ?",
                (NOW.isoformat(), running.job_id),
            )

        self.assertEqual(self.service.reconcile_incomplete(), 1)
        self.assertEqual(self.service.get(running.job_id).status, "interrupted")
        self.assertEqual(self.service.get(pending.job_id).status, "pending")
        self.assertEqual(
            [job.job_id for job in self.service.list()],
            [running.job_id, pending.job_id],
        )

    def test_fail_incomplete_converges_pending_job_and_allows_retry(self) -> None:
        pending = self.service.create(self.person.person_id)

        failed = self.service.fail_incomplete(
            pending.job_id, "index_job_submission_failed"
        )
        replacement = self.service.create(self.person.person_id)

        self.assertEqual((failed.status, failed.retrieval_mode), ("failed", "none"))
        self.assertEqual(failed.error, {"code": "index_job_submission_failed"})
        self.assertEqual(failed.completed_at, NOW.isoformat())
        self.assertEqual(replacement.status, "pending")


if __name__ == "__main__":
    unittest.main()
