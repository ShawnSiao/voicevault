from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from voicevault.app_db import AppDatabase
from voicevault.embedding import EmbeddingUnavailable
from voicevault.fulltext_index import LocalFullTextIndexProvider
from voicevault.index_service import IndexService
from voicevault.kb import init_kb
from voicevault.person_archive import PersonRepository, PlatformAccountRepository
from voicevault.retrieval import RetrievalRepository, RetrievalRequest
from voicevault.retrieval_service import RetrievalService
from voicevault.server import create_server
from voicevault.vector_index import LocalVectorIndexProvider


UTC = timezone.utc
GENERATION = "44444444-4444-4444-8444-444444444444"


class ManualExecutor:
    def __init__(self, service: RetrievalService) -> None:
        self.service = service
        self.submitted: list[str] = []

    def submit(self, run_id: str) -> None:
        self.submitted.append(run_id)

    def execute_next(self) -> None:
        self.service.execute(self.submitted.pop(0))


class RaisingExecutor:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def submit(self, run_id: str) -> None:
        raise self.error


class RetrievalApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.database = AppDatabase(data_dir=self.root / "runtime")
        self.database.initialize()
        self.person = PersonRepository(self.database).create("Alice")
        self.account = PlatformAccountRepository(self.database).bind(
            self.person.person_id,
            platform="xueqiu",
            external_user_id="12345",
            archive_basis_confirmed_at="2026-07-11T00:00:00Z",
        )
        now = datetime(2026, 7, 11, tzinfo=UTC)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?)",
                ("post-a", self.account.account_id, "external-a", now.isoformat(), "https://example.test/a", now.isoformat()),
            )
            connection.execute(
                "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, NULL)",
                ("revision-a", "post-a", hashlib.sha256(b"common evidence").hexdigest(), "common evidence", now.isoformat()),
            )
            connection.execute(
                "INSERT INTO content_dispositions VALUES (?, 'active', NULL, ?, NULL)",
                ("post-a", now.isoformat()),
            )
        self.fulltext = LocalFullTextIndexProvider(self.root / "runtime")
        self.vector = LocalVectorIndexProvider(self.root / "runtime")
        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION)):
            IndexService(
                self.database,
                self.fulltext,
                self.vector,
                None,
                clock=lambda: now,
            ).rebuild_person(self.person.person_id)
        self.service = RetrievalService(
            self.database,
            RetrievalRepository(),
            self.fulltext,
            self.vector,
            None,
            clock=lambda: now,
        )
        self.executor = ManualExecutor(self.service)
        self.kb = init_kb(self.root / "kb")
        self.server = create_server(
            self.kb,
            port=0,
            app_database=self.database,
            retrieval_service=self.service,
            retrieval_executor=self.executor,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"
        self.addCleanup(self._close)

    def _close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_post_and_get_persist_complete_stable_resource(self) -> None:
        payload = {
            "query": "common",
            "person_ids": [self.person.person_id],
            "platforms": ["xueqiu"],
            "published_from": "2026-07-10T00:00:00Z",
            "published_to": "2026-07-12T00:00:00+00:00",
            "revision_scope": "current",
            "limit": 2,
            "min_hits_per_person": 1,
            "max_chunks_per_post": 1,
        }

        accepted = self._post("/api/retrieval-runs", payload, expected=202)
        pending = self._get(accepted["status_url"])["run"]
        self.executor.execute_next()
        completed = self._get(accepted["status_url"])["run"]

        self.assertEqual(set(accepted), {"ok", "run", "status_url"})
        self.assertEqual(accepted["run"], {"run_id": completed["run_id"], "status": "pending"})
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["retrieval_mode"], "fulltext_only")
        self.assertEqual(completed["request"]["person_ids"], [self.person.person_id])
        self.assertEqual(completed["persons"][0]["generation_status"], "degraded")
        self.assertEqual(completed["hits"][0]["post_id"], "post-a")
        serialized = json.dumps(completed)
        for forbidden in ("common evidence", "similarity", "bm25", str(self.database.path)):
            self.assertNotIn(forbidden, serialized)

    def test_request_is_closed_and_validates_people_arrays_limits_and_utc_times(self) -> None:
        valid = {"query": "common", "person_ids": [self.person.person_id]}
        invalid = (
            {**valid, "extra": True},
            {"query": "common"},
            {**valid, "person_ids": "not-an-array"},
            {**valid, "person_ids": [str(uuid.uuid4()) for _ in range(11)]},
            {**valid, "published_from": "2026-07-11T00:00:00"},
            {**valid, "published_from": "2026-07-11T08:00:00+08:00"},
            {**valid, "limit": 51},
        )

        for payload in invalid:
            with self.subTest(payload=payload):
                status, body = self._error("POST", "/api/retrieval-runs", payload)
                self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"))

    def test_unknown_person_and_unusable_head_are_rejected_without_creating_runs(self) -> None:
        missing_status, missing = self._error(
            "POST",
            "/api/retrieval-runs",
            {"query": "common", "person_ids": [str(uuid.uuid4())]},
        )
        self.assertEqual((missing_status, missing["error"]["code"]), (404, "person_not_found"))
        for generation_status in ("pending", "building", "stale", "failed"):
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE index_generations SET status = ? WHERE generation_id = ?",
                    (generation_status, GENERATION),
                )
            status, payload = self._error(
                "POST",
                "/api/retrieval-runs",
                {"query": "common", "person_ids": [self.person.person_id]},
            )
            self.assertEqual((status, payload["error"]["code"]), (409, "index_stale"))
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM retrieval_runs").fetchone()[0], 0)

    def test_unknown_run_is_stable_404_and_route_is_owned(self) -> None:
        for run_id in (str(uuid.uuid4()), "not-a-uuid"):
            status, payload = self._error("GET", f"/api/retrieval-runs/{run_id}")
            self.assertEqual((status, payload["error"]["code"]), (404, "retrieval_run_not_found"))
        self.assertTrue(self.server.api_router.owns("/api/retrieval-runs"))

    def test_corrupt_persisted_run_returns_sanitized_500_and_server_remains_available(self) -> None:
        accepted = self._post(
            "/api/retrieval-runs",
            {"query": "common", "person_ids": [self.person.person_id]},
            expected=202,
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE retrieval_runs SET request_json = ? WHERE run_id = ?",
                (json.dumps({"query": f"private {self.database.path}"}), accepted["run"]["run_id"]),
            )

        status, payload = self._error("GET", accepted["status_url"])

        self.assertEqual((status, payload["error"]["code"]), (500, "retrieval_run_failed"))
        self.assertNotIn(str(self.database.path), json.dumps(payload))
        self.assertNotIn("private", json.dumps(payload))
        self.assertTrue(self._get("/api/persons")["ok"])

    def test_retrieval_storage_exception_returns_sanitized_500(self) -> None:
        private_error = sqlite3.OperationalError(f"private {self.database.path}")
        with patch.object(self.service, "get_run", side_effect=private_error):
            status, payload = self._error("GET", f"/api/retrieval-runs/{uuid.uuid4()}")

        self.assertEqual((status, payload["error"]["code"]), (500, "retrieval_run_failed"))
        self.assertNotIn(str(self.database.path), json.dumps(payload))

    def test_submit_failures_are_mapped_and_sanitized(self) -> None:
        cases = (
            (EmbeddingUnavailable("private provider"), 503, "provider_unavailable"),
            (OSError(f"private {self.database.path}"), 500, "retrieval_run_failed"),
        )
        for error, expected_status, expected_code in cases:
            self.server.api_router.retrieval_executor = RaisingExecutor(error)
            status, payload = self._error(
                "POST",
                "/api/retrieval-runs",
                {"query": "common", "person_ids": [self.person.person_id]},
            )
            self.assertEqual((status, payload["error"]["code"]), (expected_status, expected_code))
            self.assertNotIn(str(self.database.path), json.dumps(payload))

    def test_default_server_without_embedding_environment_uses_fulltext_only_service(self) -> None:
        with patch.dict(
            os.environ,
            {
                "VOICEVAULT_EMBEDDING_BASE_URL": "",
                "VOICEVAULT_EMBEDDING_MODEL": "",
                "VOICEVAULT_EMBEDDING_API_KEY": "",
            },
        ):
            default_server = create_server(self.kb, port=0, app_database=self.database)
        try:
            default_service = default_server.api_router.retrieval_service
            pending = default_service.create_run(
                RetrievalRequest("common", (self.person.person_id,))
            )
            self.assertIsNone(default_service.embedding_provider)
            self.assertEqual(pending.persons[0].retrieval_mode, "fulltext_only")
        finally:
            default_server.server_close()

    def _get(self, path: str) -> dict:
        with urlopen(f"{self.base_url}{path}", timeout=5) as response:
            return json.loads(response.read())

    def _post(self, path: str, payload: dict, *, expected: int) -> dict:
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
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
