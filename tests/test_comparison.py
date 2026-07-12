from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicevault.comparison import (
    compare_roles,
    default_comparison_dir,
    list_comparison_exports,
    review_comparison_export,
    write_comparison_outputs,
)
from voicevault.importers import load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb


class RoleComparisonTests(unittest.TestCase):
    def test_compare_roles_auto_builds_multi_role_comparison_from_local_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            _add_growth_role(kb)
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            result = compare_roles(kb, "NVDA margin AI", symbol="NVDA", roles="auto", limit=3, evidence_limit=2)

            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(result["comparison_type"], "local_evidence_role_comparison")
            self.assertEqual(result["answer_language"], "zh-CN")
            self.assertEqual(result["query"], "NVDA margin AI")
            self.assertEqual(result["filters"]["roles"], "auto")
            self.assertEqual(result["review"]["status"], "draft")
            self.assertEqual(result["review"]["reviewed_at"], "")
            self.assertGreaterEqual(result["coverage"]["role_count"], 2)
            self.assertGreaterEqual(result["coverage"]["evidence_count"], 2)
            self.assertGreaterEqual(result["routing"]["route_count"], 2)
            role_ids = {item["role_id"] for item in result["role_answers"]}
            self.assertIn("sample-investor", role_ids)
            self.assertIn("growth-investor", role_ids)
            self.assertTrue(all(item["answer"]["answer_language"] == "zh-CN" for item in result["role_answers"]))
            self.assertTrue(result["consensus"]["summary"])
            self.assertIsInstance(result["divergences"], list)
            self.assertNotIn("。；", result["comparison_answer"])
            self.assertNotIn("。；", result["consensus"]["summary"])
            self.assertIn("## 角色对比", result["comparison_markdown"])
            self.assertIn("sample-investor", result["comparison_markdown"])
            self.assertIn("growth-investor", result["comparison_markdown"])

    def test_compare_roles_keeps_explicit_no_evidence_roles_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            _add_growth_role(kb)
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            result = compare_roles(kb, "rare unmatched query", roles="sample-investor,growth-investor", limit=2)

            self.assertEqual(result["coverage"]["role_count"], 2)
            self.assertEqual(result["coverage"]["evidence_count"], 0)
            self.assertEqual([item["status"] for item in result["role_answers"]], ["no_evidence", "no_evidence"])
            self.assertIn("证据不足", result["comparison_answer"])

    def test_write_and_list_comparison_exports_preserves_contract_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            _add_growth_role(kb)
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            result = compare_roles(kb, "NVDA margin AI", symbol="NVDA", roles="auto", limit=3, evidence_limit=2)

            output = write_comparison_outputs(default_comparison_dir(kb, "NVDA margin AI"), result)
            exports = list_comparison_exports(kb)

            self.assertTrue(output["comparison_json"].is_file())
            self.assertTrue(output["comparison_markdown"].is_file())
            self.assertEqual(len(exports), 1)
            self.assertEqual(exports[0]["schema_version"], 1)
            self.assertEqual(exports[0]["query"], "NVDA margin AI")
            self.assertEqual(exports[0]["status"], "deliverable")
            self.assertGreaterEqual(exports[0]["role_count"], 2)
            self.assertGreaterEqual(exports[0]["evidence_count"], 2)
            self.assertEqual(exports[0]["comparison_json"], str(output["comparison_json"]))
            self.assertEqual(exports[0]["review_status"], "draft")
            self.assertFalse(exports[0]["adopted"])

    def test_review_comparison_export_marks_adopted_and_refreshes_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            _add_growth_role(kb)
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            result = compare_roles(kb, "NVDA margin AI", symbol="NVDA", roles="auto", limit=3, evidence_limit=2)
            output = write_comparison_outputs(default_comparison_dir(kb, "NVDA margin AI"), result)

            reviewed = review_comparison_export(
                output["comparison_json"],
                status="adopted",
                reviewer="codex-product-review",
                notes="Approved for release handoff.",
            )
            exports = list_comparison_exports(kb, review_status="adopted")

            self.assertEqual(reviewed["comparison"]["review"]["status"], "adopted")
            self.assertEqual(reviewed["comparison"]["review"]["reviewer"], "codex-product-review")
            self.assertEqual(reviewed["comparison"]["review"]["notes"], "Approved for release handoff.")
            self.assertTrue(reviewed["comparison"]["review"]["reviewed_at"].endswith("Z"))
            self.assertTrue(Path(reviewed["comparison_markdown"]).is_file())
            self.assertIn("## 审阅", Path(reviewed["comparison_markdown"]).read_text(encoding="utf-8"))
            self.assertEqual(len(exports), 1)
            self.assertEqual(exports[0]["review_status"], "adopted")
            self.assertEqual(exports[0]["reviewer"], "codex-product-review")
            self.assertEqual(exports[0]["review_notes"], "Approved for release handoff.")
            self.assertTrue(exports[0]["adopted"])
            self.assertEqual(list_comparison_exports(kb, review_status="draft"), [])


def _add_growth_role(kb) -> None:
    role_dir = kb.roles_dir / "growth-investor"
    role_dir.mkdir(parents=True)
    (role_dir / "profile.md").write_text(
        "---\n"
        "role_id: growth-investor\n"
        "display_name: Growth Investor\n"
        "status: reviewed\n"
        "---\n"
        "\n"
        "# Role Profile\n",
        encoding="utf-8",
        newline="\n",
    )
    (role_dir / "statements.csv").write_text(
        "statement_id,role_id,source_type,source_url,published_at,captured_at,title,body,symbols,topics,stance,time_horizon,confidence,notes\n"
        "growth-001,growth-investor,post,https://example.com/growth-nvda,2026-05-27,2026-05-28,NVDA AI demand,AI infrastructure demand can offset short term margin pressure for NVIDIA,NVDA,ai-infrastructure,bullish,long_term,medium,\n"
        "growth-002,growth-investor,post,https://example.com/growth-margin,2026-05-26,2026-05-27,NVDA margin context,Margin pressure matters less if AI revenue growth keeps compounding,NVDA,earnings,bullish,long_term,medium,\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    unittest.main()
