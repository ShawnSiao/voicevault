from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from voicevault.app_db import AppDatabase
from voicevault.collection_jobs import CollectionService
from voicevault.collection_results import CollectionManifestInvalid, CoverageUnproven
from voicevault.collection_submit import (
    CollectionCancelPending,
    CollectionSubmitConflict,
    CollectionSubmitLeaseExpired,
    CollectionSubmitLeaseRejected,
)
from voicevault.coverage import CoverageRepository, page_date_range_to_utc
from voicevault.kb import init_kb
from voicevault.person_archive import PersonRepository, PlatformAccountRepository
from voicevault.server import create_server
from tests.test_collection_results import (
    COLLECTOR_ID,
    EXECUTION_START,
    HEARTBEAT_AT,
    JOB_ID,
    REQUEST_END,
    REQUEST_START,
    SEGMENT_ID,
    ResultFixture,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 11, 8, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class CollectionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.database = AppDatabase(data_dir=root / "runtime")
        self.database.initialize()
        self.person = PersonRepository(self.database).create("Alice")
        self.account = PlatformAccountRepository(self.database).bind(
            self.person.person_id,
            platform="xueqiu",
            external_user_id="12345",
            archive_basis_confirmed_at="2026-07-11T00:00:00Z",
        )
        self.clock = FakeClock()
        self.service = CollectionService(
            self.database,
            instance_id="instance-a",
            clock=self.clock,
            handoff_ttl=timedelta(minutes=5),
            lease_ttl=timedelta(minutes=2),
        )
        self.kb = init_kb(root / "kb")
        self._start_server()
        self.addCleanup(self._close)

    def _start_server(self) -> None:
        self.server = create_server(
            self.kb, port=0, app_database=self.database, collection_service=self.service, instance_id="instance-a"
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def _close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_create_list_get_and_no_remote_action(self) -> None:
        created = self._post(
            "/api/collection-jobs",
            {"account_id": self.account.account_id, "mode": "normal", "start_date": "2026-07-01", "end_date": "2026-07-02"},
            expected=201,
        )["job"]
        listed = self._get(f"/api/collection-jobs?{urlencode({'account_id': self.account.account_id, 'status': 'pending_codex', 'limit': 10})}")
        loaded = self._get(f"/api/collection-jobs/{created['job_id']}")["job"]

        self.assertEqual(listed["jobs"][0]["job_id"], created["job_id"])
        self.assertEqual(loaded["created_at"], "2026-07-11T08:00:00Z")
        self.assertEqual(loaded["updated_at"], "2026-07-11T08:00:00Z")
        self.assertEqual(len(loaded["segments"]), 1)
        self.assertNotIn("handoffs", loaded)
        self.assertIn("active_handoff", loaded)

        self._post(f"/api/collection-jobs/{created['job_id']}/cancel", {})
        CoverageRepository(self.database).record_validated_complete(
            self.account.account_id, page_date_range_to_utc("2026-07-01", "2026-07-02")
        )
        no_action = self._post(
            "/api/collection-jobs",
            {"account_id": self.account.account_id, "mode": "normal", "start_date": "2026-07-01", "end_date": "2026-07-02"},
            expected=201,
        )["job"]
        self.assertEqual((no_action["status"], no_action["outcome"], no_action["remote_action_count"]), ("succeeded", "no_remote_action", 0))
        self.assertNotIn("active_handoff", no_action)

    def test_active_handoff_disappears_after_claim_and_claim_is_gone_on_replay(self) -> None:
        job = self._create_job()
        handoff_id = job["active_handoff"]["handoff_id"]
        claimed = self._post(
            f"/api/collection-handoffs/{handoff_id}/claim", {"collector_id": "collector-a"}
        )
        loaded = self._get(f"/api/collection-jobs/{job['job_id']}")["job"]
        status, replay = self._error(
            "POST", f"/api/collection-handoffs/{handoff_id}/claim", {"collector_id": "collector-b"}
        )

        self.assertEqual(claimed["job"]["status"], "claimed")
        self.assertEqual(claimed["manifest"]["account"]["external_user_id"], "12345")
        self.assertIn("点击并等待正文完整渲染", claimed["manifest"]["body_capture_policy"]["expand_control"])
        self.assertEqual(claimed["lease"]["collector_id"], "collector-a")
        self.assertNotIn("active_handoff", loaded)
        self.assertEqual((status, replay["error"]["code"]), (410, "collection_handoff_gone"))

    def test_heartbeat_cancel_acknowledge_and_resume_across_requests(self) -> None:
        job = self._create_job()
        handoff = job["active_handoff"]["handoff_id"]
        self._post(f"/api/collection-handoffs/{handoff}/claim", {"collector_id": "collector-a"})
        segment_id = job["segments"][0]["segment_id"]
        heartbeat = self._post(
            f"/api/collection-jobs/{job['job_id']}/heartbeat",
            {
                "collector_id": "collector-a",
                "checkpoint": {"cursor": "post-1"},
                "segment_progress": {segment_id: {"status": "running", "items_seen": 2}},
                "remote_action_count": 3,
            },
        )
        self._post(f"/api/collection-jobs/{job['job_id']}/cancel", {})
        observed = self._post(
            f"/api/collection-jobs/{job['job_id']}/heartbeat", {"collector_id": "collector-a"}
        )
        cancelled = self._post(
            f"/api/collection-jobs/{job['job_id']}/cancel/acknowledge", {"collector_id": "collector-a"}
        )

        self.assertEqual(heartbeat["job"]["status"], "running")
        self.assertFalse(heartbeat["cancel_requested"])
        self.assertTrue(observed["cancel_requested"])
        self.assertEqual(cancelled["job"]["status"], "cancelled")

    def test_expired_lease_can_be_resumed_or_cancelled_through_existing_api(self) -> None:
        job = self._create_job()
        old_handoff = job["active_handoff"]["handoff_id"]
        self._post(
            f"/api/collection-handoffs/{old_handoff}/claim", {"collector_id": "collector-a"}
        )
        self.clock.advance(timedelta(minutes=3))

        listed = self._get("/api/collection-jobs")["jobs"][0]
        polled = self._get(f"/api/collection-jobs/{job['job_id']}")["job"]
        resumed = self._post(f"/api/collection-jobs/{job['job_id']}/resume", {})["job"]

        self.assertEqual(listed["status"], "interrupted")
        self.assertEqual(polled["status"], "interrupted")
        self.assertEqual(resumed["status"], "pending_codex")
        self.assertEqual(resumed["handoff_version"], 2)
        new_handoff = resumed["active_handoff"]["handoff_id"]
        self._post(
            f"/api/collection-handoffs/{new_handoff}/claim", {"collector_id": "collector-b"}
        )
        cancelling = self._post(f"/api/collection-jobs/{job['job_id']}/cancel", {})["job"]
        self.assertIsNotNone(cancelling["cancel_requested_at"])
        self.clock.advance(timedelta(minutes=3))

        cancelled = self._get(f"/api/collection-jobs/{job['job_id']}")["job"]

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(cancelled["lease_expires_at"])
        resume_status, resume_error = self._error(
            "POST", f"/api/collection-jobs/{job['job_id']}/resume", {}
        )
        self.assertEqual((resume_status, resume_error["error"]["code"]), (409, "collection_conflict"))

    def test_create_reconciles_expired_lease_before_enforcing_active_job_constraint(self) -> None:
        job = self._create_job()
        self._post(
            f"/api/collection-handoffs/{job['active_handoff']['handoff_id']}/claim",
            {"collector_id": "collector-a"},
        )
        self._post(f"/api/collection-jobs/{job['job_id']}/cancel", {})
        self.clock.advance(timedelta(minutes=3))

        replacement = self._create_job()

        self.assertEqual(self.service.get_job(job["job_id"]).status, "cancelled")
        self.assertEqual(replacement["status"], "pending_codex")
        self._post(
            f"/api/collection-handoffs/{replacement['active_handoff']['handoff_id']}/claim",
            {"collector_id": "collector-b"},
        )
        self.clock.advance(timedelta(minutes=3))

        status, conflict = self._error(
            "POST",
            "/api/collection-jobs",
            {
                "account_id": self.account.account_id,
                "mode": "normal",
                "start_date": "2026-07-01",
                "end_date": "2026-07-02",
            },
        )

        self.assertEqual((status, conflict["error"]["code"]), (409, "active_collection_job_exists"))
        self.assertEqual(self.service.get_job(replacement["job_id"]).status, "interrupted")

    def test_wait_rate_partial_fail_and_resume_map_to_domain_states(self) -> None:
        actions = (
            ("wait-for-human", "waiting_for_human", "verification_required"),
            ("rate-limit", "rate_limited", "rate_limited"),
            ("partial", "partial", "platform_layout_changed"),
        )
        job = self._create_job()
        for index, (action, expected, error_code) in enumerate(actions):
            handoff = job["active_handoff"]["handoff_id"]
            self._post(f"/api/collection-handoffs/{handoff}/claim", {"collector_id": "collector-a"})
            paused = self._post(
                f"/api/collection-jobs/{job['job_id']}/{action}",
                {"collector_id": "collector-a", "error": {"code": error_code}},
            )["job"]
            self.assertEqual(paused["status"], expected)
            job = self._post(f"/api/collection-jobs/{job['job_id']}/resume", {})["job"]
            self.assertEqual(job["status"], "pending_codex")
            self.assertIn("active_handoff", job)
        handoff = job["active_handoff"]["handoff_id"]
        self._post(f"/api/collection-handoffs/{handoff}/claim", {"collector_id": "collector-a"})
        failed = self._post(
            f"/api/collection-jobs/{job['job_id']}/fail",
            {"collector_id": "collector-a", "error": {"code": "provider_unavailable"}},
        )["job"]
        self.assertEqual(failed["status"], "failed")

    def test_submit_imports_valid_result_and_restart_replays_redacted_receipt(self) -> None:
        fixture, digest = self._stage_submit_job()
        envelope = {
            "collector_id": COLLECTOR_ID,
            "handoff_version": 1,
            "manifest_sha256": digest,
        }
        accepted = self._post(f"/api/collection-jobs/{JOB_ID}/submit", envelope)

        self.assertEqual(set(accepted), {"ok", "job", "submission"})
        self.assertEqual(accepted["job"]["status"], "succeeded")
        self.assertFalse(accepted["submission"]["replayed"])
        serialized = json.dumps(accepted)
        self.assertNotIn("voice 3002", serialized)
        self.assertNotIn("cookie", serialized.lower())
        self.assertNotIn("api_key", serialized.lower())
        self.assertNotIn(str(fixture.data_dir), serialized)

        self._close()
        self.service = CollectionService(
            self.database,
            instance_id="instance-a",
            clock=self.clock,
            handoff_ttl=timedelta(minutes=5),
            lease_ttl=timedelta(minutes=2),
        )
        self._start_server()
        replay = self._post(f"/api/collection-jobs/{JOB_ID}/submit", envelope)
        self.assertTrue(replay["submission"]["replayed"])
        self.assertEqual(
            {key: value for key, value in replay["submission"].items() if key != "replayed"},
            {key: value for key, value in accepted["submission"].items() if key != "replayed"},
        )

    def test_submit_rejects_open_or_malformed_envelopes_without_calling_service(self) -> None:
        valid = {"collector_id": "collector-a", "handoff_version": 1, "manifest_sha256": "a" * 64}
        invalid = (
            {"collector_id": "collector-a"},
            {**valid, "manifest": {}},
            {**valid, "handoff_version": True},
            {**valid, "manifest_sha256": "A" * 64},
            {**valid, "manifest_sha256": f" {'a' * 64} "},
        )
        with patch.object(self.server.api_router.submissions, "submit") as submit:
            for envelope in invalid:
                status, payload = self._error(
                    "POST", f"/api/collection-jobs/{JOB_ID}/submit", envelope
                )
                self.assertEqual((status, payload["error"]["code"]), (400, "invalid_request"))
        submit.assert_not_called()

    def test_submit_maps_manifest_domain_conflict_and_unexpected_failures_without_paths(self) -> None:
        envelope = {"collector_id": "collector-a", "handoff_version": 1, "manifest_sha256": "a" * 64}
        cases = (
            (CollectionManifestInvalid("invalid"), 422, "collection_manifest_invalid"),
            (CoverageUnproven("unproven"), 422, "coverage_unproven"),
            (CollectionSubmitLeaseRejected("rejected"), 409, "collection_submit_lease_rejected"),
            (CollectionSubmitLeaseExpired("expired"), 409, "collection_submit_lease_expired"),
            (CollectionCancelPending("cancel"), 409, "collection_cancel_pending"),
            (CollectionSubmitConflict("conflict"), 409, "collection_submit_conflict"),
            (OSError(f"private {self.database.path}"), 500, "collection_submit_failed"),
        )
        for error, expected_status, expected_code in cases:
            with self.subTest(code=expected_code), patch.object(
                self.server.api_router.submissions, "submit", side_effect=error
            ):
                status, payload = self._error(
                    "POST", f"/api/collection-jobs/{JOB_ID}/submit", envelope
                )
                self.assertEqual((status, payload["error"]["code"]), (expected_status, expected_code))
                self.assertNotIn(str(self.database.path), json.dumps(payload))

        status, payload = self._error(
            "POST", "/api/collection-jobs/11111111-1111-4111-8111-111111111111/submit", envelope
        )
        self.assertEqual((status, payload["error"]["code"]), (404, "collection_job_not_found"))

    def test_submit_tampered_manifest_is_422_and_keeps_lease(self) -> None:
        fixture, digest = self._stage_submit_job()
        before = self.service.get_job(JOB_ID)
        fixture.posts_path.write_bytes(b"tampered\n")
        status, payload = self._error(
            "POST",
            f"/api/collection-jobs/{JOB_ID}/submit",
            {"collector_id": COLLECTOR_ID, "handoff_version": 1, "manifest_sha256": digest},
        )
        after = self.service.get_job(JOB_ID)
        self.assertEqual((status, payload["error"]["code"]), (422, "collection_manifest_invalid"))
        self.assertEqual((after.status, after.collector_id, after.lease_expires_at), (before.status, before.collector_id, before.lease_expires_at))

    def test_validation_not_found_conflict_and_limit_errors_are_stable(self) -> None:
        bad_status, bad = self._error("POST", "/api/collection-jobs", {"account_id": self.account.account_id})
        missing_status, missing = self._error("GET", "/api/collection-jobs/missing")
        self._create_job()
        conflict_status, conflict = self._error(
            "POST",
            "/api/collection-jobs",
            {"account_id": self.account.account_id, "mode": "normal", "start_date": "2026-07-01", "end_date": "2026-07-02"},
        )
        limit_status, limit = self._error("GET", "/api/collection-jobs?limit=999999")
        self.assertEqual((bad_status, bad["error"]["code"]), (400, "invalid_request"))
        self.assertEqual((missing_status, missing["error"]["code"]), (404, "collection_job_not_found"))
        self.assertEqual((conflict_status, conflict["error"]["code"]), (409, "active_collection_job_exists"))
        self.assertEqual((limit_status, limit["error"]["code"]), (400, "invalid_request"))

    def _create_job(self) -> dict:
        return self._post(
            "/api/collection-jobs",
            {"account_id": self.account.account_id, "mode": "normal", "start_date": "2026-07-01", "end_date": "2026-07-02"},
            expected=201,
        )["job"]

    def _stage_submit_job(self) -> tuple[ResultFixture, str]:
        fixture = ResultFixture(self.database.path.parent.parent, self.database.path.parent.name)
        fixture.manifest["target"].update(
            person_id=self.person.person_id,
            account_id=self.account.account_id,
            external_user_id=self.account.external_user_id,
        )
        for post in fixture.posts:
            post["author_external_user_id"] = self.account.external_user_id
            post["source_url"] = (
                f"https://xueqiu.com/{self.account.external_user_id}/{post['external_post_id']}"
            )
        created_at = "2026-07-04T00:00:00Z"
        lease_expires_at = "2026-07-11T08:20:00Z"
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO collection_jobs(
                    job_id, account_id, mode, status, requested_start_at,
                    requested_end_at, remote_action_count, handoff_version,
                    collector_id, lease_expires_at, last_heartbeat_at,
                    created_at, updated_at
                ) VALUES (?, ?, 'normal', 'running', ?, ?, 1, 1, ?, ?, ?, ?, ?)
                """,
                (
                    JOB_ID,
                    self.account.account_id,
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
                ) VALUES ('handoff-a', ?, 1, 'instance-a', ?, ?, ?, ?)
                """,
                (
                    JOB_ID,
                    lease_expires_at,
                    EXECUTION_START.isoformat().replace("+00:00", "Z"),
                    COLLECTOR_ID,
                    created_at,
                ),
            )
        return fixture, fixture.write()

    def _get(self, path: str) -> dict:
        with urlopen(f"{self.base_url}{path}", timeout=5) as response:
            return json.loads(response.read())

    def _post(self, path: str, payload: dict, *, expected: int = 200) -> dict:
        request = Request(
            f"{self.base_url}{path}", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
        )
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, expected)
            return json.loads(response.read())

    def _error(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=5)
        return raised.exception.code, json.loads(raised.exception.read())


if __name__ == "__main__":
    unittest.main()
