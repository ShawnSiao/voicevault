from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voicevault.action_runs import record_action_run
from voicevault.answer import answer_query, default_answer_dir, write_answer_outputs
from voicevault.comparison import compare_roles, default_comparison_dir, write_comparison_outputs
from voicevault.importers import load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb
from voicevault.remediation import build_remediation_queue


class RemediationQueueTests(unittest.TestCase):
    def test_build_remediation_queue_prioritizes_recoverable_product_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            failed = record_action_run(
                kb,
                action_type="answer",
                status="failed",
                payload={"query": "NVDA margin", "symbol": "NVDA", "limit": 2},
                error="temporary answer failure",
                source="local_api",
            )
            no_evidence = answer_query(kb, "unmatched product gap", limit=2)
            write_answer_outputs(default_answer_dir(kb, "unmatched product gap"), no_evidence)
            comparison = compare_roles(kb, "NVDA margin", symbol="NVDA", roles="all", limit=3, evidence_limit=1)
            write_comparison_outputs(default_comparison_dir(kb, "NVDA margin"), comparison)

            queue = build_remediation_queue(kb)
            items_by_type = {item["action_type"]: item for item in queue["items"]}

            self.assertEqual(queue["schema_version"], 1)
            self.assertTrue(queue["ok"])
            self.assertGreaterEqual(queue["summary"]["total"], 3)
            self.assertGreaterEqual(queue["summary"]["ready"], 3)
            self.assertEqual(items_by_type["retry_action_run"]["endpoint"], "/api/action-runs/retry")
            self.assertEqual(items_by_type["retry_action_run"]["payload"]["run_id"], failed["run_id"])
            self.assertEqual(items_by_type["retry_action_run"]["severity"], "high")
            self.assertEqual(items_by_type["rerun_answer"]["endpoint"], "/api/answer")
            self.assertEqual(items_by_type["rerun_answer"]["payload"]["query"], "unmatched product gap")
            self.assertEqual(items_by_type["review_comparison"]["endpoint"], "/api/comparison/review")
            self.assertEqual(items_by_type["review_comparison"]["payload"]["status"], "adopted")

    def test_build_remediation_queue_includes_answer_quality_repairs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            legacy = answer_query(kb, "NVDA infrastructure", role_id="sample-investor", symbol="NVDA", limit=2)
            legacy.pop("role_answer")
            write_answer_outputs(default_answer_dir(kb, "NVDA infrastructure"), legacy)

            queue = build_remediation_queue(kb)
            quality_item = next(item for item in queue["items"] if item["action_type"] == "improve_answer")

            self.assertEqual(quality_item["status"], "ready")
            self.assertEqual(quality_item["endpoint"], "/api/answer")
            self.assertEqual(quality_item["payload"]["query"], "NVDA infrastructure")
            self.assertEqual(quality_item["payload"]["role_id"], "sample-investor")

    def test_build_remediation_queue_includes_answer_regression_repairs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            (kb.content_dir / "evaluations").mkdir(parents=True, exist_ok=True)
            (kb.content_dir / "evaluations" / "questions.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "questions": [
                            {
                                "id": "fixed-nvda-margin",
                                "query": "NVDA margin",
                                "role_id": "sample-investor",
                                "symbol": "NVDA",
                                "expected_role_id": "sample-investor",
                                "min_evidence": 1,
                                "requires_role_answer": True,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            queue = build_remediation_queue(kb)
            regression_item = next(item for item in queue["items"] if item["action_type"] == "fix_answer_regression")

            self.assertEqual(regression_item["status"], "ready")
            self.assertEqual(regression_item["severity"], "high")
            self.assertEqual(regression_item["endpoint"], "/api/answer")
            self.assertEqual(regression_item["payload"]["query"], "NVDA margin")
            self.assertEqual(regression_item["payload"]["role_id"], "sample-investor")
            self.assertEqual(regression_item["source"]["kind"], "answer_regression")
            self.assertEqual(queue["summary"]["answer_regression_repairs"], 1)


if __name__ == "__main__":
    unittest.main()
