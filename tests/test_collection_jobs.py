from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from voicevault.app_db import AppDatabase
from voicevault import collection_jobs, person_archive
from voicevault.collection_jobs import (
    ActiveCollectionJobExists,
    CollectionAccountUnconfirmed,
    CollectionAccountNotFound,
    CollectionService,
    HandoffRejected,
    InvalidCollectionMode,
    InvalidCollectionTransition,
    LeaseRejected,
)
from voicevault.coverage import CoverageRepository, UtcInterval
from voicevault.person_archive import PersonRepository, PlatformAccountRepository


def instant(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 7, day, hour, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class CollectionJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = AppDatabase(data_dir=Path(self.temp_dir.name))
        self.database.initialize()
        person = PersonRepository(self.database).create("Alice")
        accounts = PlatformAccountRepository(self.database)
        self.account = accounts.bind(
            person.person_id,
            platform="xueqiu",
            external_user_id="12345",
            archive_basis_confirmed_at="2026-07-11T00:00:00Z",
        )
        self.unconfirmed_account = accounts.bind(
            person.person_id,
            platform="xueqiu",
            external_user_id="67890",
        )
        self.clock = FakeClock(instant(11, 8))
        self.service = CollectionService(
            self.database,
            instance_id="instance-a",
            clock=self.clock,
            handoff_ttl=timedelta(minutes=5),
            lease_ttl=timedelta(minutes=2),
        )

    def test_normal_fully_covered_creates_no_remote_action_without_handoff(self) -> None:
        requested = UtcInterval(instant(1), instant(5))
        CoverageRepository(self.database).record_validated_complete(self.account.account_id, requested)

        job = self.service.create_job(self.account.account_id, requested, mode="normal")

        self.assertEqual(job.status, "succeeded")
        self.assertEqual(job.outcome, "no_remote_action")
        self.assertEqual(job.remote_action_count, 0)
        self.assertEqual(job.segments, ())
        self.assertEqual(job.handoffs, ())

    def test_partial_coverage_creates_only_missing_segments(self) -> None:
        requested = UtcInterval(instant(1), instant(10))
        coverage = CoverageRepository(self.database)
        coverage.record_validated_complete(self.account.account_id, UtcInterval(instant(1), instant(3)))
        coverage.record_validated_complete(self.account.account_id, UtcInterval(instant(5), instant(7)))

        job = self.service.create_job(self.account.account_id, requested, mode="normal")

        self.assertEqual(job.status, "pending_codex")
        self.assertEqual(
            [segment.interval for segment in job.segments],
            [UtcInterval(instant(3), instant(5)), UtcInterval(instant(7), instant(10))],
        )
        self.assertEqual([segment.ordinal for segment in job.segments], [0, 1])
        self.assertEqual(len(job.handoffs), 1)
        self.assertEqual(job.handoffs[0].version, 1)
        self.assertEqual(job.handoffs[0].instance_id, "instance-a")

    def test_recheck_targets_full_requested_interval(self) -> None:
        requested = UtcInterval(instant(1), instant(5))
        CoverageRepository(self.database).record_validated_complete(self.account.account_id, requested)

        job = self.service.create_job(self.account.account_id, requested, mode="recheck")

        self.assertEqual([segment.interval for segment in job.segments], [requested])
        self.assertEqual(job.status, "pending_codex")

    def test_unconfirmed_account_cannot_create_collection_job(self) -> None:
        with self.assertRaises(CollectionAccountUnconfirmed):
            self.service.create_job(
                self.unconfirmed_account.account_id,
                UtcInterval(instant(1), instant(5)),
                mode="normal",
            )

    def test_unknown_account_cannot_create_collection_job(self) -> None:
        with self.assertRaises(CollectionAccountNotFound):
            self.service.create_job(
                "missing-account",
                UtcInterval(instant(1), instant(5)),
                mode="normal",
            )

    def test_persisted_unsafe_account_id_cannot_enter_manifest(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO platform_accounts(
                    account_id, person_id, platform, external_user_id,
                    archive_basis_confirmed_at, created_at, updated_at
                ) VALUES (?, ?, 'xueqiu', ?, ?, ?, ?)
                """,
                (
                    "unsafe-account",
                    self.account.person_id,
                    "https://xueqiu.com/u/123?cookie=bad",
                    "2026-07-11T00:00:00Z",
                    "2026-07-11T00:00:00Z",
                    "2026-07-11T00:00:00Z",
                ),
            )

        with self.assertRaises(person_archive.InvalidExternalUserId):
            self.service.create_job(
                "unsafe-account", UtcInterval(instant(1), instant(5)), mode="normal"
            )

        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM collection_jobs WHERE account_id = 'unsafe-account'"
                ).fetchone()[0],
                0,
            )

    def test_collection_mode_is_closed_to_normal_and_recheck(self) -> None:
        with self.assertRaises(InvalidCollectionMode):
            self.service.create_job(
                self.account.account_id,
                UtcInterval(instant(1), instant(5)),
                mode="custom",
            )

    def test_only_one_active_job_per_account(self) -> None:
        requested = UtcInterval(instant(1), instant(5))
        self.service.create_job(self.account.account_id, requested, mode="normal")

        with self.assertRaises(ActiveCollectionJobExists):
            self.service.create_job(self.account.account_id, requested, mode="recheck")

    def test_handoff_claim_is_one_time_and_instance_bound(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        handoff_id = job.handoffs[0].handoff_id
        other_instance = CollectionService(
            self.database,
            instance_id="instance-b",
            clock=self.clock,
            handoff_ttl=timedelta(minutes=5),
            lease_ttl=timedelta(minutes=2),
        )

        with self.assertRaises(HandoffRejected):
            other_instance.claim(handoff_id, "collector-a")
        claimed = self.service.claim(handoff_id, "collector-a")
        with self.assertRaises(HandoffRejected):
            self.service.claim(handoff_id, "collector-b")

        self.assertEqual(claimed.job.status, "claimed")
        self.assertEqual(claimed.job.collector_id, "collector-a")
        self.assertEqual(
            set(claimed.manifest),
            {"job_id", "account", "mode", "body_capture_policy", "segments", "lease"},
        )
        self.assertEqual(
            claimed.manifest["account"],
            {"account_id": self.account.account_id, "platform": "xueqiu", "external_user_id": "12345"},
        )
        self.assertNotIn("url", repr(claimed.manifest).lower())
        self.assertNotIn("cookie", repr(claimed.manifest).lower())
        self.assertIn("展开", claimed.manifest["body_capture_policy"]["expand_control"])

    def test_expired_handoff_is_rejected(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        self.clock.advance(timedelta(minutes=6))

        with self.assertRaises(HandoffRejected):
            self.service.claim(job.handoffs[0].handoff_id, "collector-a")

    def test_heartbeat_requires_collector_and_valid_lease(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        claimed = self.service.claim(job.handoffs[0].handoff_id, "collector-a")
        segment_id = claimed.job.segments[0].segment_id

        with self.assertRaises(LeaseRejected):
            self.service.heartbeat(claimed.job.job_id, "collector-b")
        heartbeat = self.service.heartbeat(
            claimed.job.job_id,
            "collector-a",
            checkpoint={"cursor": "post-100"},
            segment_progress={
                segment_id: {
                    "status": "running",
                    "items_seen": 3,
                    "checkpoint": {"earliest_visible_post_id": "post-100"},
                }
            },
            remote_action_count=2,
        )

        self.assertEqual(heartbeat.job.status, "running")
        self.assertEqual(heartbeat.job.checkpoint, {"cursor": "post-100"})
        self.assertEqual(
            heartbeat.job.segments[0].progress,
            {
                "status": "running",
                "items_seen": 3,
                "checkpoint": {"earliest_visible_post_id": "post-100"},
            },
        )
        self.assertEqual(
            heartbeat.job.segments[0].checkpoint,
            {"earliest_visible_post_id": "post-100"},
        )
        self.assertEqual(heartbeat.job.remote_action_count, 2)
        self.assertEqual(heartbeat.job.last_heartbeat_at, self.clock.now)
        self.assertFalse(heartbeat.cancel_requested)

        heartbeat = self.service.heartbeat(
            claimed.job.job_id, "collector-a", remote_action_count=1
        )
        self.assertEqual(heartbeat.job.remote_action_count, 2)

        self.clock.advance(timedelta(minutes=3))
        with self.assertRaises(LeaseRejected):
            self.service.heartbeat(claimed.job.job_id, "collector-a")

    def test_invalid_segment_status_rolls_back_entire_heartbeat(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        claimed = self.service.claim(job.handoffs[0].handoff_id, "collector-a")
        segment_id = claimed.job.segments[0].segment_id
        before = self.service.get_job(job.job_id)

        for invalid_status in ("bogus", 7):
            with self.subTest(status=invalid_status):
                with self.assertRaises(collection_jobs.InvalidSegmentProgress):
                    self.service.heartbeat(
                        job.job_id,
                        "collector-a",
                        checkpoint={"cursor": "must-roll-back"},
                        segment_progress={segment_id: {"status": invalid_status}},
                        remote_action_count=9,
                    )

        self.assertEqual(self.service.get_job(job.job_id), before)

    def test_invalid_segment_progress_does_not_bypass_lease_validation(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        claimed = self.service.claim(job.handoffs[0].handoff_id, "collector-a")

        with self.assertRaises(LeaseRejected):
            self.service.heartbeat(
                job.job_id,
                "collector-b",
                segment_progress={
                    claimed.job.segments[0].segment_id: {"status": "bogus"}
                },
            )

    def test_segment_status_cannot_move_back_to_pending(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        claimed = self.service.claim(job.handoffs[0].handoff_id, "collector-a")
        segment_id = claimed.job.segments[0].segment_id
        running = self.service.heartbeat(
            job.job_id,
            "collector-a",
            segment_progress={segment_id: {"status": "running"}},
        ).job

        with self.assertRaises(collection_jobs.InvalidSegmentProgress):
            self.service.heartbeat(
                job.job_id,
                "collector-a",
                checkpoint={"cursor": "must-roll-back"},
                segment_progress={segment_id: {"status": "pending"}},
                remote_action_count=9,
            )

        self.assertEqual(self.service.get_job(job.job_id), running)

    def test_cancel_before_claim_invalidates_handoff(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        handoff_id = job.handoffs[0].handoff_id

        cancelled = self.service.request_cancel(job.job_id)

        self.assertEqual(cancelled.status, "cancelled")
        self.assertIsNotNone(cancelled.handoffs[0].revoked_at)
        with self.assertRaises(HandoffRejected):
            self.service.claim(handoff_id, "collector-a")

    def test_running_cancel_is_observed_and_acknowledged(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        claimed = self.service.claim(job.handoffs[0].handoff_id, "collector-a")
        self.service.heartbeat(claimed.job.job_id, "collector-a")

        cancelling = self.service.request_cancel(job.job_id)
        heartbeat = self.service.heartbeat(job.job_id, "collector-a")
        cancelled = self.service.acknowledge_cancel(job.job_id, "collector-a")

        self.assertEqual(cancelling.status, "running")
        self.assertIsNotNone(cancelling.cancel_requested_at)
        self.assertTrue(heartbeat.cancel_requested)
        self.assertEqual(cancelled.status, "cancelled")
        with self.assertRaises(LeaseRejected):
            self.service.acknowledge_cancel(job.job_id, "collector-b")

    def test_expired_lease_becomes_interrupted(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        self.service.claim(job.handoffs[0].handoff_id, "collector-a")
        self.clock.advance(timedelta(minutes=3))

        interrupted_ids = self.service.reconcile_expired_leases()
        interrupted = self.service.get_job(job.job_id)

        self.assertEqual(interrupted_ids, (job.job_id,))
        self.assertEqual(interrupted.status, "interrupted")
        self.assertIsNone(interrupted.lease_expires_at)

    def test_cancel_request_wins_when_lease_expires(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        self.service.claim(job.handoffs[0].handoff_id, "collector-a")
        self.service.request_cancel(job.job_id)
        self.clock.advance(timedelta(minutes=3))

        reconciled_ids = self.service.reconcile_expired_leases()
        cancelled = self.service.get_job(job.job_id)

        self.assertEqual(reconciled_ids, (job.job_id,))
        self.assertEqual(cancelled.status, "cancelled")
        with self.assertRaises(InvalidCollectionTransition):
            self.service.resume(job.job_id)

    def test_cancel_request_blocks_non_cancel_terminal_or_pause_transition(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        self.service.claim(job.handoffs[0].handoff_id, "collector-a")
        self.service.request_cancel(job.job_id)
        transitions = (
            lambda: self.service.wait_for_human(
                job.job_id, "collector-a", error={"code": "verification_required"}
            ),
            lambda: self.service.rate_limit(
                job.job_id, "collector-a", error={"code": "rate_limited"}
            ),
            lambda: self.service.mark_partial(
                job.job_id, "collector-a", error={"code": "platform_layout_changed"}
            ),
            lambda: self.service.fail(
                job.job_id, "collector-a", error={"code": "provider_unavailable"}
            ),
        )

        for transition in transitions:
            with self.subTest(transition=transition):
                with self.assertRaises(InvalidCollectionTransition):
                    transition()

        unchanged = self.service.get_job(job.job_id)
        self.assertEqual(unchanged.status, "claimed")
        self.assertIsNotNone(unchanged.cancel_requested_at)
        self.assertTrue(self.service.heartbeat(job.job_id, "collector-a").cancel_requested)

    def test_resume_rejects_recoverable_record_with_cancel_request(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE collection_jobs
                SET status = 'interrupted', cancel_requested_at = ?
                WHERE job_id = ?
                """,
                ("2026-07-11T08:00:00.000000Z", job.job_id),
            )

        with self.assertRaises(InvalidCollectionTransition):
            self.service.resume(job.job_id)

        unchanged = self.service.get_job(job.job_id)
        self.assertEqual(unchanged.status, "interrupted")
        self.assertIsNotNone(unchanged.cancel_requested_at)
        self.assertEqual(unchanged.handoff_version, 1)

    def test_lease_expiry_uses_chronological_order_across_fractional_seconds(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        self.service.claim(job.handoffs[0].handoff_id, "collector-a")
        self.clock.advance(timedelta(minutes=2, microseconds=1))

        with self.assertRaises(LeaseRejected):
            self.service.heartbeat(job.job_id, "collector-a")

        self.assertEqual(self.service.reconcile_expired_leases(), (job.job_id,))

    def test_resume_issues_higher_handoff_version_and_old_handoff_stays_invalid(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        old_handoff = job.handoffs[0].handoff_id
        self.service.claim(old_handoff, "collector-a")
        self.clock.advance(timedelta(minutes=3))
        self.service.reconcile_expired_leases()
        restarted = CollectionService(
            self.database,
            instance_id="instance-b",
            clock=self.clock,
            handoff_ttl=timedelta(minutes=5),
            lease_ttl=timedelta(minutes=2),
        )

        resumed = restarted.resume(job.job_id)

        self.assertEqual(resumed.status, "pending_codex")
        self.assertEqual([handoff.version for handoff in resumed.handoffs], [1, 2])
        self.assertEqual(resumed.handoffs[-1].instance_id, "instance-b")
        with self.assertRaises(HandoffRejected):
            restarted.claim(old_handoff, "collector-b")
        self.assertEqual(restarted.claim(resumed.handoffs[-1].handoff_id, "collector-b").job.status, "claimed")

    def test_pending_job_with_valid_current_handoff_cannot_be_resumed(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )

        with self.assertRaises(InvalidCollectionTransition):
            self.service.resume(job.job_id)

        self.assertEqual(self.service.get_job(job.job_id).handoff_version, 1)

    def test_expired_pending_handoff_can_be_reissued_once_and_old_token_stays_gone(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        old_handoff = job.handoffs[0].handoff_id
        self.clock.advance(timedelta(minutes=6))

        resumed = self.service.resume(job.job_id)

        self.assertEqual(resumed.status, "pending_codex")
        self.assertEqual(resumed.handoff_version, 2)
        self.assertIsNotNone(resumed.handoffs[0].revoked_at)
        with self.assertRaises(HandoffRejected):
            self.service.claim(old_handoff, "collector-old")
        self.assertEqual(
            self.service.claim(resumed.handoffs[-1].handoff_id, "collector-new").job.status,
            "claimed",
        )

    def test_unclaimed_handoff_from_old_instance_can_be_reissued_before_expiry(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        old_handoff = job.handoffs[0].handoff_id
        restarted = CollectionService(
            self.database,
            instance_id="instance-b",
            clock=self.clock,
            handoff_ttl=timedelta(minutes=5),
            lease_ttl=timedelta(minutes=2),
        )

        resumed = restarted.resume(job.job_id)

        self.assertEqual(resumed.handoff_version, 2)
        self.assertEqual(resumed.handoffs[-1].instance_id, "instance-b")
        with self.assertRaises(HandoffRejected):
            restarted.claim(old_handoff, "collector-old")
        self.assertEqual(
            restarted.claim(resumed.handoffs[-1].handoff_id, "collector-new").job.status,
            "claimed",
        )

    def test_concurrent_stale_pending_resume_issues_only_one_new_handoff(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        self.clock.advance(timedelta(minutes=6))
        services = [
            CollectionService(
                self.database,
                instance_id="instance-a",
                clock=self.clock,
                handoff_ttl=timedelta(minutes=5),
                lease_ttl=timedelta(minutes=2),
            )
            for _ in range(2)
        ]

        def attempt(service: CollectionService) -> str:
            try:
                return service.resume(job.job_id).status
            except InvalidCollectionTransition:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(attempt, services))

        loaded = self.service.get_job(job.job_id)
        self.assertEqual(sorted(outcomes), ["pending_codex", "rejected"])
        self.assertEqual(loaded.handoff_version, 2)
        self.assertEqual(len(loaded.handoffs), 2)

    def test_resume_reconciles_expired_lease_and_cancel_request_wins(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        self.service.claim(job.handoffs[0].handoff_id, "collector-a")
        self.clock.advance(timedelta(minutes=3))

        resumed = self.service.resume(job.job_id)

        self.assertEqual(resumed.status, "pending_codex")
        self.assertEqual(resumed.handoff_version, 2)
        self.service.claim(resumed.handoffs[-1].handoff_id, "collector-b")
        self.service.request_cancel(job.job_id)
        self.clock.advance(timedelta(minutes=3))

        with self.assertRaises(InvalidCollectionTransition):
            self.service.resume(job.job_id)

        cancelled = self.service.get_job(job.job_id)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertIsNone(cancelled.collector_id)
        self.assertIsNone(cancelled.lease_expires_at)

    def test_state_and_checkpoint_survive_new_service_instance(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        claimed = self.service.claim(job.handoffs[0].handoff_id, "collector-a")
        self.service.heartbeat(
            job.job_id,
            "collector-a",
            checkpoint={"cursor": "post-100", "page": 3},
            segment_progress={claimed.job.segments[0].segment_id: {"items_seen": 7}},
        )
        reopened = CollectionService(
            AppDatabase(db_path=self.database.path),
            instance_id="instance-b",
            clock=self.clock,
            handoff_ttl=timedelta(minutes=5),
            lease_ttl=timedelta(minutes=2),
        )

        loaded = reopened.get_job(job.job_id)

        self.assertEqual(loaded.status, "running")
        self.assertEqual(loaded.checkpoint, {"cursor": "post-100", "page": 3})
        self.assertEqual(loaded.segments[0].progress, {"items_seen": 7})
        self.assertEqual(loaded.handoffs[0].collector_id, "collector-a")

    def test_error_survives_new_service_instance(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        self.service.claim(job.handoffs[0].handoff_id, "collector-a")
        self.service.wait_for_human(
            job.job_id,
            "collector-a",
            error={"code": "login_required", "details": {"step": "sign_in"}},
        )
        reopened = CollectionService(
            AppDatabase(db_path=self.database.path),
            instance_id="instance-b",
            clock=self.clock,
            handoff_ttl=timedelta(minutes=5),
            lease_ttl=timedelta(minutes=2),
        )

        loaded = reopened.get_job(job.job_id)

        self.assertEqual(loaded.status, "waiting_for_human")
        self.assertEqual(loaded.error, {"code": "login_required", "details": {"step": "sign_in"}})

    def test_pause_failure_and_partial_state_are_persisted_without_coverage(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        claimed = self.service.claim(job.handoffs[0].handoff_id, "collector-a")
        waiting = self.service.wait_for_human(
            job.job_id,
            "collector-a",
            error={"code": "verification_required", "challenge": "manual"},
            checkpoint={"cursor": "post-10"},
        )
        resumed = self.service.resume(job.job_id)
        self.service.claim(resumed.handoffs[-1].handoff_id, "collector-a")
        limited = self.service.rate_limit(
            job.job_id, "collector-a", error={"code": "rate_limited", "retry_after_seconds": 60}
        )
        resumed = self.service.resume(job.job_id)
        self.service.claim(resumed.handoffs[-1].handoff_id, "collector-a")
        partial = self.service.mark_partial(
            job.job_id, "collector-a", error={"code": "platform_layout_changed"}
        )
        resumed = self.service.resume(job.job_id)
        self.service.claim(resumed.handoffs[-1].handoff_id, "collector-a")
        failed = self.service.fail(job.job_id, "collector-a", error={"code": "provider_unavailable"})

        with self.database.connect() as connection:
            coverage_count = connection.execute("SELECT COUNT(*) FROM coverage_intervals").fetchone()[0]

        self.assertEqual(waiting.status, "waiting_for_human")
        self.assertEqual(waiting.checkpoint, {"cursor": "post-10"})
        self.assertEqual(limited.status, "rate_limited")
        self.assertEqual(partial.status, "partial")
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error, {"code": "provider_unavailable"})
        self.assertEqual(coverage_count, 0)

        with self.assertRaises(InvalidCollectionTransition):
            self.service.resume(job.job_id)

    def test_no_transition_writes_coverage(self) -> None:
        job = self.service.create_job(
            self.account.account_id, UtcInterval(instant(1), instant(5)), mode="normal"
        )
        self.service.claim(job.handoffs[0].handoff_id, "collector-a")
        self.service.heartbeat(job.job_id, "collector-a", checkpoint={"cursor": "post-2"})
        self.service.request_cancel(job.job_id)
        self.service.acknowledge_cancel(job.job_id, "collector-a")

        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM coverage_intervals").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
