from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicevault.collections import create_evidence_pack, list_reports
from voicevault.importers import load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb


class CollectionTests(unittest.TestCase):
    def test_create_evidence_pack_writes_markdown_from_search_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            output = create_evidence_pack(
                kb,
                title="NVDA Margin Evidence",
                query="NVDA margin",
                symbol="NVDA",
                topic="earnings",
                limit=5,
            )
            text = output.read_text(encoding="utf-8")

            self.assertEqual(output, kb.reports_dir / "nvda-margin-evidence.md")
            self.assertIn("# NVDA Margin Evidence", text)
            self.assertIn("query: NVDA margin", text)
            self.assertIn("symbols:", text)
            self.assertIn("NVDA margin watch", text)
            self.assertIn("https://example.com/sample-nvda-margin", text)
            self.assertIn("## Uncertainty", text)

    def test_list_reports_reads_report_metadata_and_match_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            output = create_evidence_pack(
                kb,
                title="NVDA Margin Evidence",
                query="NVDA margin",
                symbol="NVDA",
                topic="earnings",
            )

            reports = list_reports(kb)

            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]["title"], "NVDA Margin Evidence")
            self.assertEqual(reports[0]["path"], str(output))
            self.assertEqual(reports[0]["query"], "NVDA margin")
            self.assertEqual(reports[0]["symbols"], ["NVDA"])
            self.assertEqual(reports[0]["topics"], ["earnings"])
            self.assertGreaterEqual(reports[0]["matches"], 1)


if __name__ == "__main__":
    unittest.main()
