from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voicevault.answer import answer_query, default_answer_dir, write_answer_outputs
from voicevault.answer_regression import (
    audit_answer_regression_coverage,
    audit_answer_regression,
    default_answer_regression_changelog_path,
    default_answer_regression_suite_path,
    delete_answer_regression_question,
    export_answer_regression_suite,
    import_answer_regression_suite,
    load_answer_regression_changelog,
    upsert_answer_regression_question,
)
from voicevault.importers import load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb


class AnswerRegressionTests(unittest.TestCase):
    def test_audit_answer_regression_passes_fixed_question_with_role_answer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", role_id="sample-investor", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            _write_suite(
                kb,
                [
                    {
                        "id": "nvda-margin",
                        "query": "NVDA margin",
                        "role_id": "sample-investor",
                        "symbol": "NVDA",
                        "expected_role_id": "sample-investor",
                        "min_evidence": 1,
                        "requires_role_answer": True,
                    }
                ],
            )

            audit = audit_answer_regression(kb)

            self.assertTrue(audit["ok"])
            self.assertEqual(audit["schema_version"], 1)
            self.assertEqual(audit["summary"]["total"], 1)
            self.assertEqual(audit["summary"]["passed"], 1)
            self.assertEqual(audit["summary"]["review"], 0)
            self.assertEqual(audit["summary"]["failed"], 0)
            item = audit["items"][0]
            self.assertEqual(item["id"], "nvda-margin")
            self.assertEqual(item["status"], "pass")
            self.assertEqual(item["score"], 100)
            self.assertEqual(item["query"], "NVDA margin")
            self.assertEqual(item["expected_role_id"], "sample-investor")
            self.assertEqual(item["actual_role_id"], "sample-investor")
            self.assertEqual(item["failed_checks"], [])
            self.assertEqual(item["payload"]["role_id"], "sample-investor")
            self.assertTrue(item["answer_json"].endswith("answer.json"))

    def test_audit_answer_regression_flags_missing_fixed_question_answer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            _write_suite(
                kb,
                [
                    {
                        "id": "nvda-ai",
                        "query": "NVDA AI",
                        "role_id": "sample-investor",
                        "symbol": "NVDA",
                        "expected_role_id": "sample-investor",
                        "min_evidence": 1,
                    }
                ],
            )

            audit = audit_answer_regression(kb)

            self.assertFalse(audit["ok"])
            self.assertEqual(audit["summary"]["failed"], 1)
            self.assertEqual(audit["summary"]["missing_answers"], 1)
            item = audit["items"][0]
            self.assertEqual(item["status"], "fail")
            self.assertIn("answer_export", item["failed_checks"])
            self.assertEqual(item["recommended_endpoint"], "/api/answer")
            self.assertEqual(item["payload"]["query"], "NVDA AI")
            self.assertEqual(item["payload"]["role_id"], "sample-investor")
            self.assertEqual(item["payload"]["symbol"], "NVDA")
            self.assertFalse(item["payload"]["auto_route"])

    def test_upsert_and_delete_answer_regression_question_manage_suite_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")

            created = upsert_answer_regression_question(
                kb,
                {
                    "query": "NVDA margin",
                    "role_id": "sample-investor",
                    "symbol": "NVDA",
                    "expected_role_id": "sample-investor",
                    "min_evidence": 1,
                    "requires_role_answer": True,
                },
            )
            updated = upsert_answer_regression_question(
                kb,
                {
                    "id": created["question"]["id"],
                    "query": "NVDA margin",
                    "role_id": "sample-investor",
                    "symbol": "NVDA",
                    "topic": "margins",
                    "expected_role_id": "sample-investor",
                    "min_evidence": 2,
                    "requires_role_answer": True,
                },
            )
            deleted = delete_answer_regression_question(kb, created["question"]["id"])

            self.assertEqual(created["suite"]["schema_version"], 1)
            self.assertEqual(created["question"]["id"], "nvda-margin")
            self.assertEqual(updated["question"]["topic"], "margins")
            self.assertEqual(updated["question"]["min_evidence"], 2)
            self.assertEqual(len(updated["suite"]["questions"]), 1)
            self.assertEqual(deleted["deleted_id"], "nvda-margin")
            self.assertEqual(deleted["suite"]["questions"], [])
            self.assertTrue(default_answer_regression_suite_path(kb).is_file())

    def test_question_metadata_and_changelog_track_suite_governance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")

            created = upsert_answer_regression_question(
                kb,
                {
                    "query": "NVDA margin",
                    "role_id": "sample-investor",
                    "symbol": "NVDA",
                    "expected_role_id": "sample-investor",
                    "source_url": "https://example.com/regression/nvda-margin",
                    "rationale": "Protects margin role routing behavior.",
                    "updated_by": "qa-owner",
                    "min_evidence": 1,
                },
            )
            updated = upsert_answer_regression_question(
                kb,
                {
                    "id": created["question"]["id"],
                    "query": "NVDA margin",
                    "role_id": "sample-investor",
                    "symbol": "NVDA",
                    "topic": "margins",
                    "expected_role_id": "sample-investor",
                    "source_url": "https://example.com/regression/nvda-margin-v2",
                    "rationale": "Protects margin role routing and evidence thresholds.",
                    "updated_by": "qa-owner",
                    "min_evidence": 2,
                },
            )
            deleted = delete_answer_regression_question(kb, created["question"]["id"], updated_by="qa-owner")

            changelog = load_answer_regression_changelog(kb)
            actions = [change["action"] for change in changelog["changes"]]
            saved_question = updated["suite"]["questions"][0]

            self.assertEqual(saved_question["source_url"], "https://example.com/regression/nvda-margin-v2")
            self.assertEqual(saved_question["rationale"], "Protects margin role routing and evidence thresholds.")
            self.assertEqual(saved_question["updated_by"], "qa-owner")
            self.assertTrue(saved_question["created_at"])
            self.assertTrue(saved_question["updated_at"])
            self.assertEqual(created["change"]["action"], "create")
            self.assertEqual(updated["change"]["action"], "update")
            self.assertEqual(deleted["change"]["action"], "delete")
            self.assertEqual(actions, ["create", "update", "delete"])
            self.assertEqual(changelog["changes"][-1]["before"]["id"], "nvda-margin")
            self.assertTrue(default_answer_regression_changelog_path(kb).is_file())

    def test_export_and_import_answer_regression_suite_support_batch_governance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            created = upsert_answer_regression_question(
                kb,
                {
                    "id": "nvda-margin",
                    "query": "NVDA margin",
                    "role_id": "sample-investor",
                    "symbol": "NVDA",
                    "expected_role_id": "sample-investor",
                    "source_url": "https://example.com/regression/nvda-margin",
                    "rationale": "Protects the original margin route.",
                    "updated_by": "qa-owner",
                    "min_evidence": 1,
                },
            )
            export_path = Path(temp_dir) / "answer-regression-export.json"

            exported = export_answer_regression_suite(kb, out_path=export_path)
            import_payload = dict(exported)
            import_payload["questions"] = [
                {
                    **created["question"],
                    "topic": "margins",
                    "source_url": "https://example.com/regression/nvda-margin-v2",
                    "rationale": "Protects the imported margin route.",
                    "min_evidence": 2,
                },
                {
                    "id": "nvda-ai",
                    "query": "NVDA AI demand",
                    "role_id": "sample-investor",
                    "symbol": "NVDA",
                    "expected_role_id": "sample-investor",
                    "source_url": "https://example.com/regression/nvda-ai",
                    "rationale": "Protects batch-created AI demand coverage.",
                    "updated_by": "qa-import",
                    "min_evidence": 1,
                },
            ]

            dry_run = import_answer_regression_suite(kb, import_payload, dry_run=True, updated_by="qa-import")
            applied = import_answer_regression_suite(kb, import_payload, dry_run=False, updated_by="qa-import")
            changelog = load_answer_regression_changelog(kb)

            self.assertTrue(export_path.is_file())
            self.assertEqual(exported["schema_version"], 1)
            self.assertEqual(exported["question_count"], 1)
            self.assertFalse(dry_run["applied"])
            self.assertEqual(dry_run["summary"]["create"], 1)
            self.assertEqual(dry_run["summary"]["update"], 1)
            self.assertEqual(dry_run["suite"]["questions"], created["suite"]["questions"])
            self.assertTrue(applied["applied"])
            self.assertEqual(applied["summary"]["create"], 1)
            self.assertEqual(applied["summary"]["update"], 1)
            self.assertEqual(applied["suite"]["questions"][0]["topic"], "margins")
            self.assertEqual(applied["suite"]["questions"][1]["id"], "nvda-ai")
            self.assertEqual(
                [change["action"] for change in changelog["changes"][-2:]],
                ["import_update", "import_create"],
            )

    def test_answer_regression_coverage_requires_passed_questions_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", role_id="sample-investor", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)

            failing = audit_answer_regression_coverage(kb, min_questions=1)
            _write_suite(
                kb,
                [
                    {
                        "id": "nvda-margin",
                        "query": "NVDA margin",
                        "role_id": "sample-investor",
                        "symbol": "NVDA",
                        "expected_role_id": "sample-investor",
                        "source_url": "https://example.com/regression/nvda-margin",
                        "rationale": "Protects margin role routing behavior.",
                        "created_at": "2026-05-31T00:00:00Z",
                        "updated_at": "2026-05-31T00:00:00Z",
                        "updated_by": "qa-owner",
                        "min_evidence": 1,
                        "requires_role_answer": True,
                    }
                ],
            )
            passing = audit_answer_regression_coverage(kb, min_questions=1)

            self.assertFalse(failing["ok"])
            self.assertEqual(failing["summary"]["total"], 0)
            self.assertEqual(failing["summary"]["min_questions"], 1)
            self.assertFalse(next(check for check in failing["checks"] if check["id"] == "minimum_questions")["ok"])
            self.assertTrue(passing["ok"])
            self.assertEqual(passing["summary"]["passed"], 1)
            self.assertEqual(passing["summary"]["missing_provenance"], 0)


def _write_suite(kb, questions: list[dict[str, object]]) -> Path:
    suite_path = default_answer_regression_suite_path(kb)
    suite_path.parent.mkdir(parents=True, exist_ok=True)
    suite_path.write_text(
        json.dumps({"schema_version": 1, "questions": questions}, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    return suite_path


if __name__ == "__main__":
    unittest.main()
