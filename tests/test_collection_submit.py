from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from tests.test_collection_results import (
    ACCOUNT_ID,
    COLLECTOR_ID,
    EXECUTION_START,
    HEARTBEAT_AT,
    JOB_ID,
    NOW,
    PERSON_ID,
    REQUEST_END,
    REQUEST_START,
    SEGMENT_ID,
    ResultFixture,
)
from voicevault.app_db import AppDatabase
from voicevault.collection_jobs import CollectionService
from voicevault.collection_submit import (
    CollectionCancelPending,
    CollectionSubmissionService,
    CollectionSubmitConflict,
    CollectionSubmitLeaseExpired,
    CollectionSubmitLeaseRejected,
)
from voicevault.collection_results import (
    CollectionManifestInvalid,
    CoverageUnproven,
    ValidatedCheckpoint,
)


class CollectionSubmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_dir = Path(self.temp_dir.name)
        self.database = AppDatabase(data_dir=self.data_dir)
        self.database.initialize()
        self.fixture = ResultFixture(self.data_dir.parent, self.data_dir.name)
        self._insert_running_job()
        self.service = CollectionSubmissionService(self.database, clock=lambda: NOW)

    def _insert_running_job(self, *, mode: str = "normal") -> None:
        created_at = "2026-07-04T00:00:00Z"
        lease_expires_at = "2026-07-04T00:20:00Z"
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO persons(person_id, display_name, created_at, updated_at)
                VALUES (?, 'Alice', ?, ?)
                """,
                (PERSON_ID, created_at, created_at),
            )
            connection.execute(
                """
                INSERT INTO platform_accounts(
                    account_id, person_id, platform, external_user_id,
                    archive_basis_confirmed_at, created_at, updated_at
                ) VALUES (?, ?, 'xueqiu', '123456', ?, ?, ?)
                """,
                (ACCOUNT_ID, PERSON_ID, created_at, created_at, created_at),
            )
            connection.execute(
                """
                INSERT INTO collection_jobs(
                    job_id, account_id, mode, status, requested_start_at,
                    requested_end_at, remote_action_count, handoff_version,
                    collector_id, lease_expires_at, last_heartbeat_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'running', ?, ?, 1, 1, ?, ?, ?, ?, ?)
                """,
                (
                    JOB_ID,
                    ACCOUNT_ID,
                    mode,
                    REQUEST_START.isoformat().replace("+00:00", "Z"),
                    REQUEST_END.isoformat().replace("+00:00", "Z"),
                    COLLECTOR_ID,
                    lease_expires_at,
                    HEARTBEAT_AT.isoformat().replace("+00:00", "Z"),
                    created_at,
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO collection_segments(
                    segment_id, job_id, ordinal, start_at, end_at, status,
                    created_at, updated_at
                ) VALUES (?, ?, 0, ?, ?, 'running', ?, ?)
                """,
                (
                    SEGMENT_ID,
                    JOB_ID,
                    REQUEST_START.isoformat().replace("+00:00", "Z"),
                    REQUEST_END.isoformat().replace("+00:00", "Z"),
                    created_at,
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO collection_handoffs(
                    handoff_id, job_id, version, instance_id, expires_at,
                    claimed_at, collector_id, created_at
                ) VALUES (?, ?, 1, 'instance-a', ?, ?, ?, ?)
                """,
                (
                    "handoff-a",
                    JOB_ID,
                    lease_expires_at,
                    EXECUTION_START.isoformat().replace("+00:00", "Z"),
                    COLLECTOR_ID,
                    created_at,
                ),
            )

    def _submit(self, digest: str):
        return self.service.submit(
            JOB_ID,
            collector_id=COLLECTOR_ID,
            handoff_version=1,
            manifest_sha256=digest,
        )

    def _job_row(self) -> sqlite3.Row:
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT * FROM collection_jobs WHERE job_id = ?", (JOB_ID,)
            ).fetchone()

    def _counts(self) -> dict[str, int]:
        tables = (
            "posts",
            "post_revisions",
            "post_observations",
            "capture_evidence",
            "post_evidence",
            "collection_job_evidence",
            "collection_checkpoints",
            "coverage_intervals",
            "collection_submissions",
        )
        with self.database.connect() as connection:
            return {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in tables
            }

    def _assert_trigger_rolls_back(self, trigger: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(trigger)
        before = self._job_row()

        with self.assertRaises(sqlite3.IntegrityError):
            self._submit(self.fixture.write())

        self.assertTrue(all(value == 0 for value in self._counts().values()))
        after = self._job_row()
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["collector_id"], before["collector_id"])
        self.assertEqual(after["lease_expires_at"], before["lease_expires_at"])
        self.assertTrue(any((self.data_dir / "evidence").rglob("*.meta")))

    def test_normal_complete_imports_archive_evidence_checkpoints_coverage_and_job(self) -> None:
        result = self._submit(self.fixture.write())

        self.assertFalse(result.replayed)
        self.assertEqual(
            (result.post_count, result.revision_count, result.observation_count),
            (3, 3, 3),
        )
        self.assertEqual((result.evidence_count, result.coverage_written), (5, 1))
        self.assertEqual(result.job_status, "succeeded")
        self.assertEqual(
            self._counts(),
            {
                "posts": 3,
                "post_revisions": 3,
                "post_observations": 3,
                "capture_evidence": 5,
                "post_evidence": 0,
                "collection_job_evidence": 5,
                "collection_checkpoints": 2,
                "coverage_intervals": 1,
                "collection_submissions": 1,
            },
        )
        job = self._job_row()
        self.assertIsNone(job["collector_id"])
        self.assertIsNone(job["lease_expires_at"])
        self.assertEqual(job["result_manifest_sha256"], result.manifest_sha256)
        self.assertIsNotNone(job["submitted_at"])

    def test_recheck_complete_never_writes_coverage(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE collection_jobs SET mode = 'recheck' WHERE job_id = ?", (JOB_ID,)
            )
        self.fixture.manifest["mode"] = "recheck"
        result = self._submit(self.fixture.write())
        self.assertEqual((result.job_status, result.coverage_written), ("succeeded", 0))
        self.assertEqual(self._counts()["coverage_intervals"], 0)

    def test_partial_imports_validated_content_without_coverage(self) -> None:
        self.fixture.make_partial()
        result = self._submit(self.fixture.write())

        self.assertEqual((result.job_status, result.coverage_written), ("partial", 0))
        self.assertEqual(self._counts()["coverage_intervals"], 0)
        self.assertGreater(self._counts()["post_revisions"], 0)

    def test_resumed_submission_appends_checkpoint_sequences(self) -> None:
        checkpoint = ValidatedCheckpoint(
            checkpoint_id=str(uuid.uuid4()),
            segment_id=SEGMENT_ID,
            sequence=0,
            observed_at=HEARTBEAT_AT,
            action_type="reload",
            triggered_remote_load=True,
            remote_action_ordinal=1,
            visible_post_ids=(),
            earliest_non_pinned_at=None,
            latest_non_pinned_at=None,
            anchor_post_id=None,
            start_kind="resume_checkpoint",
            completion_reason=None,
            boundary_post_id=None,
            reached_end=False,
            evidence_keys=(),
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO collection_checkpoints(
                    checkpoint_id, job_id, segment_id, sequence, observed_at,
                    action_type, triggered_remote_load, remote_action_ordinal,
                    visible_post_ids_json, canonical_json, created_at
                ) VALUES (?, ?, ?, 0, ?, 'initial_view', 1, 1, '[]', '{}', ?)
                """,
                (
                    str(uuid.uuid4()),
                    JOB_ID,
                    SEGMENT_ID,
                    HEARTBEAT_AT.isoformat().replace("+00:00", "Z"),
                    HEARTBEAT_AT.isoformat().replace("+00:00", "Z"),
                ),
            )
            CollectionSubmissionService._insert_checkpoints(
                connection,
                job_id=JOB_ID,
                checkpoints=(checkpoint,),
                created_at=HEARTBEAT_AT.isoformat().replace("+00:00", "Z"),
            )
        with self.database.connect() as connection:
            sequences = [
                row[0]
                for row in connection.execute(
                    """
                    SELECT sequence FROM collection_checkpoints
                    WHERE job_id = ? AND segment_id = ?
                    ORDER BY sequence
                    """,
                    (JOB_ID, SEGMENT_ID),
                )
            ]
        self.assertEqual(sequences, [0, 1])

    def test_partial_cancel_saves_content_and_cancels_but_complete_cancel_is_rejected(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE collection_jobs SET cancel_requested_at = ? WHERE job_id = ?",
                ("2026-07-04T00:09:30Z", JOB_ID),
            )
        self.fixture.make_partial("cancel_requested")
        partial = self._submit(self.fixture.write())
        self.assertEqual((partial.job_status, partial.coverage_written), ("cancelled", 0))
        self.assertGreater(self._counts()["post_revisions"], 0)

    def test_complete_cancel_pending_preserves_no_business_rows(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE collection_jobs SET cancel_requested_at = ? WHERE job_id = ?",
                ("2026-07-04T00:09:30Z", JOB_ID),
            )
        before = self._job_row()

        with self.assertRaises(CollectionCancelPending):
            self._submit(self.fixture.write())

        self.assertTrue(all(value == 0 for value in self._counts().values()))
        after = self._job_row()
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["lease_expires_at"], before["lease_expires_at"])

    def test_expired_and_invalid_lease_are_rejected_with_expiry_convergence(self) -> None:
        digest = self.fixture.write()
        with self.assertRaises(ValueError):
            self._submit("A" * 64)
        with self.assertRaises(CollectionSubmitLeaseRejected):
            self.service.submit(
                JOB_ID,
                collector_id="collector-b",
                handoff_version=1,
                manifest_sha256=digest,
            )
        self.assertEqual(self._job_row()["status"], "running")
        with self.assertRaises(CollectionSubmitLeaseRejected):
            self.service.submit(
                JOB_ID,
                collector_id=COLLECTOR_ID,
                handoff_version=2,
                manifest_sha256=digest,
            )
        self.assertEqual(self._job_row()["status"], "running")

        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE collection_jobs SET lease_expires_at = ? WHERE job_id = ?",
                (NOW.isoformat().replace("+00:00", "Z"), JOB_ID),
            )
        with self.assertRaises(CollectionSubmitLeaseExpired):
            self._submit(digest)
        expired = self._job_row()
        self.assertEqual(expired["status"], "interrupted")
        self.assertIsNone(expired["collector_id"])
        self.assertIsNone(expired["lease_expires_at"])

    def test_invalid_manifest_keeps_running_lease_unchanged(self) -> None:
        digest = self.fixture.write()
        before = self._job_row()
        self.fixture.posts_path.write_bytes(b"tampered\n")

        with self.assertRaises(CollectionManifestInvalid):
            self._submit(digest)

        after = self._job_row()
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["collector_id"], before["collector_id"])
        self.assertEqual(after["lease_expires_at"], before["lease_expires_at"])

    def test_unproven_coverage_keeps_running_lease_unchanged(self) -> None:
        self.fixture.checkpoints[-1]["evidence_keys"] = []
        self.fixture.artifacts["screenshots"] = [self.fixture.artifacts["screenshots"][0]]
        before = self._job_row()

        with self.assertRaises(CoverageUnproven):
            self._submit(self.fixture.write())

        after = self._job_row()
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["collector_id"], before["collector_id"])
        self.assertEqual(after["lease_expires_at"], before["lease_expires_at"])

    def test_exact_replay_returns_frozen_receipt_and_different_digest_conflicts(self) -> None:
        first = self._submit(self.fixture.write())
        counts = self._counts()
        replay = self._submit(first.manifest_sha256)

        self.assertTrue(replay.replayed)
        self.assertEqual(replay.submission_id, first.submission_id)
        self.assertEqual(replay.receipt, first.receipt)
        self.assertEqual(self._counts(), counts)

        self.fixture.manifest["submission_id"] = str(uuid.uuid4())
        different_digest = self.fixture.write()
        with self.assertRaises(CollectionSubmitConflict):
            self._submit(different_digest)
        self.assertEqual(self._counts(), counts)

    def test_partial_v1_replay_survives_resume_to_v2_without_reading_current_manifest(self) -> None:
        self.fixture.make_partial()
        original_digest = self.fixture.write()
        first = self._submit(original_digest)
        jobs = CollectionService(
            self.database,
            instance_id="instance-a",
            clock=lambda: NOW,
            handoff_ttl=timedelta(minutes=5),
            lease_ttl=timedelta(minutes=2),
        )
        resumed = jobs.resume(JOB_ID)
        self.assertEqual(resumed.handoff_version, 2)

        replay = self._submit(original_digest)

        self.assertTrue(replay.replayed)
        self.assertEqual(replay.receipt, first.receipt)
        with self.assertRaises(CollectionSubmitConflict):
            self._submit("f" * 64)

    def test_two_concurrent_identical_submits_create_one_submission_and_one_replay(self) -> None:
        digest = self.fixture.write()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: self._submit(digest), range(2)))

        self.assertEqual(sorted(result.replayed for result in results), [False, True])
        self.assertEqual(results[0].receipt, results[1].receipt)
        self.assertEqual(self._counts()["collection_submissions"], 1)

    def test_revision_failure_rolls_back_every_business_write(self) -> None:
        self._assert_trigger_rolls_back(
            "CREATE TRIGGER fail_submit BEFORE INSERT ON post_revisions "
            "BEGIN SELECT RAISE(ABORT, 'revision fail'); END"
        )

    def test_coverage_failure_rolls_back_every_business_write(self) -> None:
        self._assert_trigger_rolls_back(
            "CREATE TRIGGER fail_submit BEFORE INSERT ON coverage_intervals "
            "BEGIN SELECT RAISE(ABORT, 'coverage fail'); END"
        )

    def test_job_update_failure_rolls_back_every_business_write(self) -> None:
        self._assert_trigger_rolls_back(
            "CREATE TRIGGER fail_submit BEFORE UPDATE OF status ON collection_jobs "
            "WHEN NEW.status = 'succeeded' "
            "BEGIN SELECT RAISE(ABORT, 'job update fail'); END"
        )


if __name__ == "__main__":
    unittest.main()
