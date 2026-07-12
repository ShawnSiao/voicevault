from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voicevault.answer import answer_query, default_answer_dir, write_answer_outputs
from voicevault.answer_quality import audit_answer_quality
from voicevault.importers import load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb


class AnswerQualityTests(unittest.TestCase):
    def test_audit_answer_quality_flags_missing_role_answer_without_breaking_deliverability(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            current = answer_query(kb, "NVDA margin", role_id="sample-investor", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), current)
            legacy = answer_query(kb, "NVDA infrastructure", role_id="sample-investor", symbol="NVDA", limit=2)
            legacy.pop("role_answer")
            legacy["answer_markdown"] = legacy["answer_markdown"].replace("## 角色回答\n\n", "")
            write_answer_outputs(default_answer_dir(kb, "NVDA infrastructure"), legacy)

            audit = audit_answer_quality(kb)
            items_by_query = {item["query"]: item for item in audit["items"]}

            self.assertEqual(audit["schema_version"], 1)
            self.assertFalse(audit["ok"])
            self.assertEqual(audit["summary"]["total"], 2)
            self.assertEqual(audit["summary"]["passed"], 1)
            self.assertEqual(audit["summary"]["review"], 1)
            self.assertEqual(audit["summary"]["missing_role_answer"], 1)
            self.assertEqual(items_by_query["NVDA margin"]["status"], "pass")
            self.assertEqual(items_by_query["NVDA infrastructure"]["status"], "review")
            self.assertEqual(items_by_query["NVDA infrastructure"]["recommended_endpoint"], "/api/answer")
            self.assertEqual(items_by_query["NVDA infrastructure"]["payload"]["query"], "NVDA infrastructure")
            self.assertIn("role_answer", items_by_query["NVDA infrastructure"]["failed_checks"])


if __name__ == "__main__":
    unittest.main()
