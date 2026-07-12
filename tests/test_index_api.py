from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from voicevault.app_db import AppDatabase
from voicevault.index_jobs import IndexJobService
from voicevault.index_service import IndexBuildResult
from voicevault.kb import init_kb
from voicevault.person_archive import PersonRepository
from voicevault.server import create_server


NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


class FakeIndexService:
    def rebuild_person(self, person_id: str) -> IndexBuildResult:
        return IndexBuildResult("degraded", "fulltext_only", "generation-a", None)


class ManualExecutor:
    def __init__(self, service: IndexJobService) -> None:
        self.service = service
        self.submitted: list[str] = []

    def submit(self, job_id: str) -> None:
        self.submitted.append(job_id)

    def execute_next(self) -> None:
        self.service.run(self.submitted.pop(0))


class RaisingExecutor:
    def submit(self, job_id: str) -> None:
        raise RuntimeError(f"private executor detail for {job_id}")


class IndexApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.database = AppDatabase(data_dir=self.root / "runtime")
        self.database.initialize()
        self.person = PersonRepository(self.database).create("Alice")
        self.service = IndexJobService(self.database, FakeIndexService(), clock=lambda: NOW)
        self.executor = ManualExecutor(self.service)
        self.server = create_server(
            init_kb(self.root / "kb"),
            port=0,
            app_database=self.database,
            index_job_service=self.service,
            index_executor=self.executor,
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

    def test_index_job_api_capabilities_and_person_summary(self) -> None:
        accepted = self._post("/api/index-jobs", {"person_id": self.person.person_id}, 202)
        job_id = accepted["job"]["job_id"]
        self.assertEqual(self.executor.submitted, [job_id])
        self.assertEqual(self._get(f"/api/index-jobs/{job_id}")["job"]["status"], "pending")
        self.executor.execute_next()
        self.assertEqual(self._get(f"/api/index-jobs/{job_id}")["job"]["retrieval_mode"], "fulltext_only")
        self.assertEqual(len(self._get("/api/index-jobs")["jobs"]), 1)

        capabilities = self._get("/api/capabilities")["capabilities"]
        serialized = json.dumps(capabilities)
        self.assertEqual(capabilities["resource_api"], "ready")
        for forbidden in ("api_key", "secret", "local_path", str(self.database.path)):
            self.assertNotIn(forbidden, serialized)

        person = self._get("/api/persons")["persons"][0]
        self.assertEqual(person["archive"]["post_count"], 0)
        self.assertIsNone(person["index_head"])

    def test_index_job_request_is_closed_and_duplicate_is_conflict(self) -> None:
        status, payload = self._error("POST", "/api/index-jobs", {"person_id": self.person.person_id, "extra": True})
        self.assertEqual((status, payload["error"]["code"]), (400, "invalid_request"))
        self._post("/api/index-jobs", {"person_id": self.person.person_id}, 202)
        status, payload = self._error("POST", "/api/index-jobs", {"person_id": self.person.person_id})
        self.assertEqual((status, payload["error"]["code"]), (409, "active_index_job_exists"))

    def test_executor_failure_converges_job_and_same_person_can_retry(self) -> None:
        self.server.api_router.index_executor = RaisingExecutor()

        status, payload = self._error(
            "POST", "/api/index-jobs", {"person_id": self.person.person_id}
        )
        failed = self._get("/api/index-jobs")["jobs"][0]

        self.assertEqual((status, payload["error"]["code"]), (500, "index_job_submission_failed"))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], {"code": "index_job_submission_failed"})
        self.assertNotIn("private executor detail", json.dumps(payload) + json.dumps(failed))

        self.server.api_router.index_executor = self.executor
        retried = self._post(
            "/api/index-jobs", {"person_id": self.person.person_id}, 202
        )
        self.assertEqual(retried["job"]["status"], "pending")

    def _get(self, path: str) -> dict:
        with urlopen(f"{self.base_url}{path}", timeout=5) as response:
            return json.loads(response.read())

    def _post(self, path: str, payload: dict, expected: int) -> dict:
        request = Request(f"{self.base_url}{path}", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, expected)
            return json.loads(response.read())

    def _error(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        request = Request(f"{self.base_url}{path}", data=json.dumps(payload).encode() if payload is not None else None, headers={"Content-Type": "application/json"}, method=method)
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=5)
        return raised.exception.code, json.loads(raised.exception.read())


if __name__ == "__main__":
    unittest.main()
