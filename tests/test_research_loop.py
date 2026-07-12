from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voicevault.analysis import analyze_event
from voicevault.events import create_event
from voicevault.exporters import write_analysis_outputs
from voicevault.importers import load_event
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb
from voicevault.profile import generate_profile
from voicevault.sync import sync_once


class ResearchLoopTests(unittest.TestCase):
    def test_capture_to_obsidian_to_profile_to_analysis_export_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            capture_path = kb.inbox_captures_dir / "market-commentary.jsonl"
            capture_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "statement_id": "nvda-demand-001",
                                "role_id": "growth-analyst",
                                "platform": "x",
                                "platform_user_id": "growthdesk",
                                "author": "Growth Desk",
                                "url": "https://x.com/growthdesk/status/1001",
                                "published_at": "2026-05-29T14:00:00Z",
                                "captured_at": "2026-05-30T01:00:00Z",
                                "title": "NVDA demand durability",
                                "text": "AI infrastructure demand remains durable for NVDA despite near-term margin noise.",
                                "symbols": ["NVDA"],
                                "topics": ["ai-infrastructure", "margins"],
                                "stance": "bullish",
                                "time_horizon": "long_term",
                                "confidence": "medium",
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "statement_id": "nvda-margin-001",
                                "role_id": "valuation-skeptic",
                                "platform": "snowball",
                                "user_id": "value_guard",
                                "display_name": "Value Guard",
                                "source_url": "https://xueqiu.com/value_guard/2002",
                                "published_at": "2026-05-29T15:30:00Z",
                                "captured_at": "2026-05-30T01:10:00Z",
                                "title": "NVDA margin risk",
                                "body": "NVDA margin guidance matters because expectations already price in perfect AI demand.",
                                "symbols": "NVDA",
                                "topics": "margins;valuation",
                                "stance": "bearish",
                                "time_horizon": "short_term",
                                "confidence": "medium",
                            },
                            ensure_ascii=False,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            event_path = create_event(
                kb,
                event_id="2026-05-30-nvda-margin-guidance",
                title="NVIDIA Margin Guidance",
                date="2026-05-30",
                symbols=["NVDA"],
                topics=["ai-infrastructure", "margins"],
                summary="Investors debate whether AI infrastructure demand offsets softer margin guidance.",
            )

            sync_result = sync_once(kb)
            growth_profile = generate_profile(kb, "growth-analyst")
            skeptic_profile = generate_profile(kb, "valuation-skeptic")
            event = load_event(event_path)
            result = analyze_event(kb, event, roles=["growth-analyst", "valuation-skeptic"])
            written = write_analysis_outputs(kb.exports_dir / event.event_id, result)
            parsed_json = json.loads(written.json_path.read_text(encoding="utf-8"))
            markdown = written.markdown_path.read_text(encoding="utf-8")

            self.assertEqual(sync_result.captures_seen, 2)
            self.assertEqual(sync_result.notes_written, 2)
            self.assertEqual(VoiceVaultIndex(kb).count_statements(), sync_result.statements_indexed)
            self.assertTrue((kb.roles_dir / "growth-analyst" / "statements" / "x").is_dir())
            self.assertTrue((kb.roles_dir / "valuation-skeptic" / "statements" / "snowball").is_dir())
            self.assertTrue(growth_profile.is_file())
            self.assertTrue(skeptic_profile.is_file())
            self.assertIn("profile_status: generated_unreviewed", growth_profile.read_text(encoding="utf-8"))
            self.assertEqual(parsed_json["event"]["event_id"], "2026-05-30-nvda-margin-guidance")
            self.assertEqual({item["role_id"] for item in parsed_json["role_analyses"]}, {"growth-analyst", "valuation-skeptic"})
            self.assertEqual(
                {item["role_id"]: item["stance"] for item in parsed_json["role_analyses"]},
                {"growth-analyst": "bullish", "valuation-skeptic": "bearish"},
            )
            evidence_by_id = {item["statement_id"]: item for item in parsed_json["evidence"]}
            self.assertEqual(evidence_by_id["nvda-demand-001"]["source_platform"], "x")
            self.assertEqual(evidence_by_id["nvda-demand-001"]["source_author"], "Growth Desk")
            self.assertEqual(evidence_by_id["nvda-margin-001"]["source_platform"], "snowball")
            self.assertEqual(evidence_by_id["nvda-margin-001"]["source_author"], "Value Guard")
            self.assertTrue(parsed_json["disagreements"])
            self.assertGreaterEqual(len(parsed_json["evidence"]), 2)
            self.assertIn("https://x.com/growthdesk/status/1001", json.dumps(parsed_json, ensure_ascii=False))
            self.assertIn("# VoiceVault Role Analysis", markdown)
            self.assertIn("NVIDIA Margin Guidance", markdown)


if __name__ == "__main__":
    unittest.main()
