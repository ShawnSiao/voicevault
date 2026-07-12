from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voicevault.action_runs import record_action_run, read_action_run, read_action_runs
from voicevault.kb import init_kb


class ActionRunTests(unittest.TestCase):
    def test_record_action_run_persists_completed_and_failed_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")

            completed = record_action_run(
                kb,
                action_type="answer",
                status="completed",
                payload={"query": "NVDA margin", "role_id": "sample-investor", "limit": 2},
                result={
                    "artifact_kind": "answer",
                    "artifact_path": str(kb.exports_dir / "answers" / "nvda-margin" / "answer.json"),
                    "artifact_markdown": str(kb.exports_dir / "answers" / "nvda-margin" / "answer.md"),
                    "evidence_count": 2,
                },
                source="local_api",
            )
            failed = record_action_run(
                kb,
                action_type="compare",
                status="failed",
                payload={"query": "NVDA margin", "roles": "auto"},
                error="comparison failed",
                source="local_api",
            )

            status = read_action_runs(kb)
            raw = json.loads((kb.state_dir / "action-runs.json").read_text(encoding="utf-8"))

            self.assertEqual(raw["schema_version"], 1)
            self.assertEqual(status["status_path"], str(kb.state_dir / "action-runs.json"))
            self.assertTrue(status["ok"])
            self.assertEqual(status["summary"]["total"], 2)
            self.assertEqual(status["summary"]["completed"], 1)
            self.assertEqual(status["summary"]["failed"], 1)
            self.assertEqual(status["summary"]["retryable_failed"], 1)
            self.assertEqual(status["summary"]["malformed"], 0)
            self.assertEqual(status["runs"][0]["run_id"], failed["run_id"])
            self.assertEqual(status["runs"][1]["run_id"], completed["run_id"])
            self.assertEqual(status["runs"][0]["status"], "failed")
            self.assertEqual(status["runs"][0]["error"], "comparison failed")
            self.assertTrue(status["runs"][0]["retryable"])
            self.assertFalse(status["runs"][1]["retryable"])
            self.assertIn("resolved_by", status["runs"][0])
            self.assertIn("resolved_at", status["runs"][0])
            self.assertEqual(status["runs"][1]["result"]["artifact_kind"], "answer")
            self.assertEqual(status["runs"][1]["result"]["evidence_count"], 2)
            self.assertTrue(status["runs"][1]["started_at"])
            self.assertTrue(status["runs"][1]["completed_at"])
            self.assertEqual(read_action_run(kb, failed["run_id"])["run_id"], failed["run_id"])
            self.assertIsNone(read_action_run(kb, "missing-run"))

    def test_failed_action_run_gets_audit_error_when_caller_omits_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")

            failed = record_action_run(
                kb,
                action_type="role_agent",
                status="failed",
                payload={"query": "NVDA margin", "role_id": "sample-investor"},
                result={"llm_status": "failed"},
            )

            status = read_action_runs(kb)

            self.assertEqual(failed["status"], "failed")
            self.assertTrue(failed["error"])
            self.assertEqual(status["runs"][0]["error"], failed["error"])

    def test_read_action_runs_backfills_error_for_legacy_failed_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            (kb.state_dir / "action-runs.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "runs": [
                            {
                                "schema_version": 1,
                                "run_id": "role_agent:legacy",
                                "action_type": "role_agent",
                                "status": "failed",
                                "retryable": False,
                                "result": {"llm_status": "failed"},
                                "error": "",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            status = read_action_runs(kb)

            self.assertTrue(status["runs"][0]["error"])
            self.assertIn("llm_status=failed", status["runs"][0]["error"])


if __name__ == "__main__":
    unittest.main()
