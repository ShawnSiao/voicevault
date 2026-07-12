from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tests import test_questions as question_fixtures
from tests.test_answer_provider import proposed
from voicevault.answer_provider import FakeAnswerProvider
from voicevault.app_db import AppDatabase
from voicevault.kb import init_kb
from voicevault.question_service import QuestionService
from voicevault.questions import QuestionRepository
from voicevault.server import create_server


NOW = question_fixtures.NOW


class ManualQuestionExecutor:
    def __init__(self, service: QuestionService) -> None:
        self.service = service
        self.submitted: list[str] = []

    def submit(self, run_id: str) -> None:
        self.submitted.append(run_id)

    def execute_next(self) -> None:
        self.service.execute(self.submitted.pop(0))


class QuestionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.database = AppDatabase(data_dir=self.root / "runtime")
        self.database.initialize()
        question_fixtures.QuestionRepositoryTests._insert_retrieval_fixture(
            self, "retrieval-a", status="succeeded"
        )
        self.service = QuestionService(
            self.database,
            QuestionRepository(),
            providers={"openai_compatible": FakeAnswerProvider(proposed())},
            clock=lambda: NOW,
        )
        self.executor = ManualQuestionExecutor(self.service)
        self.server = create_server(
            init_kb(self.root / "kb"),
            port=0,
            app_database=self.database,
            question_service=self.service,
            question_executor=self.executor,
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

    def test_codex_task_create_evidence_submit_and_get_workflow(self) -> None:
        accepted = self._post("/api/question-runs", {"retrieval_run_id": "retrieval-a"}, expected=202)
        run_id = accepted["run"]["run_id"]

        self.assertEqual(accepted["run"]["status"], "pending_codex")
        self.assertEqual(self.executor.submitted, [])
        self.assertEqual(accepted["status_url"], f"/api/question-runs/{run_id}")
        self.assertEqual(accepted["evidence_url"], f"/api/question-runs/{run_id}/evidence")
        self.assertIn("current Codex desktop task", accepted["codex_instruction"])

        evidence = self._get(accepted["evidence_url"])
        serialized = json.dumps(evidence)
        self.assertEqual(evidence["bundle"]["evidence"][0]["evidence_id"], "E1")
        for forbidden in ("api_key", "local_path", "fulltext_rank", "vector_rank", "similarity"):
            self.assertNotIn(forbidden, serialized)

        completed = self._post(
            f"/api/question-runs/{run_id}/answer",
            proposed().to_mapping(),
            expected=200,
        )
        self.assertEqual(completed["run"]["status"], "succeeded")
        self.assertEqual(self._get(accepted["status_url"])["run"], completed["run"])

    def test_answer_is_closed_and_invalid_citation_returns_readable_run(self) -> None:
        accepted = self._post("/api/question-runs", {"retrieval_run_id": "retrieval-a"}, expected=202)
        run_id = accepted["run"]["run_id"]
        malformed = proposed().to_mapping() | {"extra": True}
        status, payload = self._error("POST", f"/api/question-runs/{run_id}/answer", malformed)
        self.assertEqual((status, payload["error"]["code"]), (400, "invalid_request"))

        invalid = proposed().to_mapping()
        invalid["citations"][0]["evidence_id"] = "E999"
        invalid["combined_citation_ids"] = ["E999"]
        invalid["person_views"][0]["citation_ids"] = ["E999"]
        status, payload = self._error("POST", f"/api/question-runs/{run_id}/answer", invalid)
        self.assertEqual((status, payload["error"]["code"]), (422, "citation_invalid"))
        self.assertEqual(payload["run"]["status"], "citation_invalid")
        self.assertEqual(self._get(f"/api/question-runs/{run_id}")["run"]["status"], "citation_invalid")

    def test_openai_provider_uses_injected_executor_only(self) -> None:
        accepted = self._post(
            "/api/question-runs",
            {"retrieval_run_id": "retrieval-a", "provider": "openai_compatible"},
            expected=202,
        )
        self.assertEqual(len(self.executor.submitted), 1)
        self.assertNotIn("codex_instruction", accepted)
        self.executor.execute_next()
        self.assertEqual(self._get(accepted["status_url"])["run"]["status"], "succeeded")

    def test_errors_and_restart_reconciliation_are_stable(self) -> None:
        status, payload = self._error("POST", "/api/question-runs", {"retrieval_run_id": "missing"})
        self.assertEqual((status, payload["error"]["code"]), (409, "retrieval_run_not_ready"))
        status, payload = self._error("GET", "/api/question-runs/missing")
        self.assertEqual((status, payload["error"]["code"]), (404, "question_run_not_found"))
        status, payload = self._error("POST", "/api/question-runs", {"retrieval_run_id": "retrieval-a", "extra": True})
        self.assertEqual((status, payload["error"]["code"]), (400, "invalid_request"))

        pending_codex = self.service.create("retrieval-a")
        running = self.service.create("retrieval-a", provider="openai_compatible")
        with self.database.transaction() as connection:
            QuestionRepository().mark_running(connection, running.run_id, started_at=NOW)
        restarted = create_server(
            init_kb(self.root / "kb-restarted"),
            port=0,
            app_database=self.database,
            question_service=self.service,
            question_executor=self.executor,
        )
        try:
            self.assertEqual(self.service.get(running.run_id).status, "interrupted")
            self.assertEqual(self.service.get(pending_codex.run_id).status, "pending_codex")
        finally:
            restarted.server_close()

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
