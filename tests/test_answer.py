from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voicevault.answer import answer_query, default_answer_dir, list_answer_exports, prune_answer_exports, write_answer_outputs
from voicevault.importers import load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb


class AnswerTests(unittest.TestCase):
    def test_answer_query_returns_cited_evidence_and_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            result = answer_query(kb, "NVDA margin", symbol="NVDA", limit=3)

            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(result["query"], "NVDA margin")
            self.assertEqual(result["answer_type"], "local_evidence_answer")
            self.assertEqual(result["answer_language"], "zh-CN")
            self.assertGreaterEqual(result["coverage"]["evidence_count"], 1)
            self.assertIn("[1]", result["answer_markdown"])
            self.assertIn("## 结论", result["answer_markdown"])
            self.assertIn("## 关键证据", result["answer_markdown"])
            self.assertIn("声迹在本地索引中找到", result["answer"])
            self.assertNotIn("VoiceVault found", result["answer"])
            self.assertTrue(result["key_points"])
            self.assertIn("[1]", result["key_points"][0]["refs"])
            self.assertIn("citations", result)
            self.assertEqual(result["citations"][0]["ref"], "[1]")
            self.assertIn("statement_id", result["citations"][0])
            self.assertIn("uncertainty", result)
            self.assertTrue(result["uncertainty"])

    def test_answer_query_returns_structured_role_answer_for_selected_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            result = answer_query(kb, "NVDA margin", role_id="sample-investor", symbol="NVDA", limit=2)

            self.assertEqual(result["role_answer"]["schema_version"], 1)
            self.assertEqual(result["role_answer"]["mode"], "single_role")
            self.assertEqual(result["role_answer"]["role_id"], "sample-investor")
            self.assertEqual(result["role_answer"]["display_name"], "Sample Investor")
            self.assertEqual(result["role_answer"]["profile_status"], "reviewed")
            self.assertIn("Sample Investor", result["role_answer"]["answer"])
            self.assertIn("[1]", result["role_answer"]["evidence_refs"])
            self.assertIn("local_public_statements_only", result["role_answer"]["source_scope"])
            self.assertIn("## 角色回答", result["answer_markdown"])

    def test_answer_query_is_explicit_when_no_evidence_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            result = answer_query(kb, "unmatched rare query", limit=3)

            self.assertEqual(result["coverage"]["evidence_count"], 0)
            self.assertEqual(result["confidence"], "low")
            self.assertEqual(result["key_points"], [])
            self.assertIn("没有找到可引用的本地证据", result["answer_markdown"])
            self.assertEqual(result["citations"], [])

    def test_write_answer_outputs_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            result = answer_query(kb, "NVDA margin", role_id="sample-investor", symbol="NVDA", limit=2)

            output = write_answer_outputs(kb.exports_dir / "answers" / "nvda-margin", result)

            self.assertTrue(output["answer_json"].is_file())
            self.assertTrue(output["answer_markdown"].is_file())
            payload = json.loads(output["answer_json"].read_text(encoding="utf-8"))
            markdown = output["answer_markdown"].read_text(encoding="utf-8")
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["query"], "NVDA margin")
            self.assertEqual(payload["answer_language"], "zh-CN")
            self.assertIn("# 证据答案", markdown)
            self.assertIn("## 引用", markdown)

    def test_list_answer_exports_discovers_written_answers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            result = answer_query(kb, "NVDA margin", role_id="sample-investor", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), result)

            exports = list_answer_exports(kb)

            self.assertEqual(exports[0]["query"], "NVDA margin")
            self.assertEqual(exports[0]["schema_version"], 1)
            self.assertEqual(exports[0]["contract_errors"], [])
            self.assertEqual(exports[0]["evidence_count"], result["coverage"]["evidence_count"])
            self.assertEqual(exports[0]["citation_count"], len(result["citations"]))
            self.assertTrue(exports[0]["evidence_backed"])
            self.assertEqual(exports[0]["status"], "deliverable")
            self.assertEqual(exports[0]["key_points"], result["key_points"])
            self.assertTrue(exports[0]["answer_json"].endswith("answer.json"))
            self.assertTrue(exports[0]["answer_markdown"].endswith("answer.md"))

    def test_list_answer_exports_preserves_role_routing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            result = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            result["selected_role_id"] = "sample-investor"
            result["selection_mode"] = "auto"
            result["role_routing"] = {
                "schema_version": 1,
                "suggested_role_id": "sample-investor",
                "confidence": "medium",
                "routes": [{"role_id": "sample-investor", "evidence_count": 2}],
            }
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), result)

            exports = list_answer_exports(kb)

            self.assertEqual(exports[0]["selected_role_id"], "sample-investor")
            self.assertEqual(exports[0]["selection_mode"], "auto")
            self.assertEqual(exports[0]["role_routing"]["suggested_role_id"], "sample-investor")
            self.assertEqual(exports[0]["role_answer"]["role_id"], "sample-investor")
            self.assertEqual(exports[0]["role_answer"]["mode"], "single_role")

    def test_list_answer_exports_marks_no_evidence_and_legacy_exports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            no_evidence = answer_query(kb, "unmatched rare query", limit=2)
            write_answer_outputs(default_answer_dir(kb, "unmatched rare query"), no_evidence)
            legacy_dir = kb.exports_dir / "answers" / "legacy-answer"
            legacy_dir.mkdir(parents=True)
            (legacy_dir / "answer.json").write_text(
                json.dumps(
                    {
                        "query": "legacy answer",
                        "generated_at": "2026-05-30T00:00:00Z",
                        "confidence": "medium",
                        "coverage": {"evidence_count": 1, "total_matches": 1},
                        "citations": [{"ref": "[1]"}],
                    }
                ),
                encoding="utf-8",
            )

            exports = {item["query"]: item for item in list_answer_exports(kb)}

            self.assertEqual(exports["unmatched rare query"]["status"], "no_evidence")
            self.assertEqual(exports["legacy answer"]["status"], "legacy_contract")

    def test_prune_answer_exports_previews_and_removes_invalid_exports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            valid = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            invalid = answer_query(kb, "unmatched rare query", limit=2)
            valid_output = write_answer_outputs(default_answer_dir(kb, "NVDA margin"), valid)
            invalid_output = write_answer_outputs(default_answer_dir(kb, "unmatched rare query"), invalid)

            preview = prune_answer_exports(kb, status="invalid", dry_run=True)

            self.assertTrue(preview["dry_run"])
            self.assertEqual(preview["matched"], 1)
            self.assertEqual(preview["removed"], 0)
            self.assertTrue(invalid_output["answer_json"].is_file())

            result = prune_answer_exports(kb, status="invalid", dry_run=False)

            self.assertFalse(result["dry_run"])
            self.assertEqual(result["matched"], 1)
            self.assertEqual(result["removed"], 1)
            self.assertFalse(invalid_output["answer_json"].exists())
            self.assertTrue(valid_output["answer_json"].is_file())

    def test_default_answer_dir_preserves_chinese_query_slug(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")

            path = default_answer_dir(kb, "英伟达 AI")

            self.assertEqual(path.name, "英伟达-ai")


if __name__ == "__main__":
    unittest.main()
