from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voicevault.analysis import analyze_event
from voicevault.analysis_exports import list_analysis_exports
from voicevault.exporters import write_analysis_outputs
from voicevault.importers import load_event, load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb


class AnalysisTests(unittest.TestCase):
    def test_analyze_event_returns_role_analysis_and_exports_json_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            event = load_event(kb.events_dir / "example-event.md")

            result = analyze_event(kb, event, roles="all")
            out_dir = kb.exports_dir / "example-event"
            written = write_analysis_outputs(out_dir, result)

            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(result["event"]["event_id"], "example-nvda-margin")
            self.assertEqual(len(result["role_analyses"]), 1)
            self.assertEqual(result["role_analyses"][0]["role_id"], "sample-investor")
            self.assertNotEqual(result["role_analyses"][0]["stance"], "unclear")
            self.assertTrue(written.json_path.is_file())
            self.assertTrue(written.markdown_path.is_file())
            parsed = json.loads(written.json_path.read_text(encoding="utf-8"))
            self.assertIn("role_analyses", parsed)
            self.assertIn("# VoiceVault Role Analysis", written.markdown_path.read_text(encoding="utf-8"))

    def test_list_analysis_exports_summarizes_generated_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            event = load_event(kb.events_dir / "example-event.md")
            result = analyze_event(kb, event, roles="all")
            written = write_analysis_outputs(kb.exports_dir / "example-event", result)

            exports = list_analysis_exports(kb)
            item = exports[0]

            self.assertEqual(len(exports), 1)
            self.assertEqual(item["status"], "ready")
            self.assertEqual(item["schema_version"], 1)
            self.assertEqual(item["event_id"], "example-nvda-margin")
            self.assertEqual(item["title"], "NVIDIA Margin Guidance")
            self.assertEqual(item["role_count"], 1)
            self.assertGreaterEqual(item["evidence_count"], 1)
            self.assertEqual(item["analysis_json"], str(written.json_path))
            self.assertEqual(item["analysis_markdown"], str(written.markdown_path))
            self.assertIn("Based on retrieved public evidence", item["synthesis_markdown"])
            self.assertEqual(item["role_summaries"][0]["role_id"], "sample-investor")
            self.assertEqual(item["role_summaries"][0]["stance"], result["role_analyses"][0]["stance"])
            self.assertGreaterEqual(item["role_summaries"][0]["evidence_count"], 1)
            self.assertGreaterEqual(len(item["evidence_summaries"]), 1)
            self.assertEqual(item["evidence_summaries"][0]["role_id"], "sample-investor")
            self.assertTrue(item["evidence_summaries"][0]["statement_id"])
            self.assertTrue(item["evidence_summaries"][0]["source_url"])
            self.assertTrue(item["evidence_summaries"][0]["excerpt"])
            self.assertGreaterEqual(len(item["role_summaries"][0]["supporting_evidence"]), 1)
            self.assertEqual(
                item["role_summaries"][0]["supporting_evidence"][0]["statement_id"],
                item["evidence_summaries"][0]["statement_id"],
            )

    def test_list_analysis_exports_marks_contract_missing_fields_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            bad_dir = kb.exports_dir / "bad-contract"
            bad_dir.mkdir(parents=True)
            (bad_dir / "analysis.json").write_text(
                json.dumps({"event": {}, "role_analyses": [], "evidence": []}),
                encoding="utf-8",
            )

            exports = list_analysis_exports(kb)

            self.assertEqual(exports[0]["status"], "malformed")
            self.assertIn("event.event_id", exports[0]["error"])
            self.assertIn("role_analyses", exports[0]["error"])

    def test_analyze_event_returns_unclear_when_role_has_no_relevant_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            other_role = kb.roles_dir / "macro-observer"
            other_role.mkdir()
            (other_role / "statements.csv").write_text(
                "statement_id,role_id,source_type,source_url,published_at,captured_at,title,body,symbols,topics,stance,time_horizon,confidence,notes\n"
                ",macro-observer,post,https://example.com/macro,2026-05-01,2026-05-02,Rates note,Only watching rates,TLT,rates,neutral,medium_term,low,\n",
                encoding="utf-8",
            )
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            event = load_event(kb.events_dir / "example-event.md")

            result = analyze_event(kb, event, roles=["macro-observer"])

            self.assertEqual(result["role_analyses"][0]["stance"], "unclear")
            self.assertIn("No relevant evidence", result["role_analyses"][0]["uncertainty"][0])


if __name__ == "__main__":
    unittest.main()
