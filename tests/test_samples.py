from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicevault.importers import load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb
from voicevault.samples import preview_sample_removal, remove_sample_content


class SampleContentTests(unittest.TestCase):
    def test_remove_sample_content_deletes_only_known_sample_role_and_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            real_role = kb.roles_dir / "real-role"
            real_role.mkdir()
            (real_role / "statements.csv").write_text(
                "statement_id,role_id,source_type,source_url,published_at,captured_at,title,body,symbols,topics,stance,time_horizon,confidence,notes\n"
                "real-001,real-role,post,https://example.com/real,2026-05-01,2026-05-02,Real note,Real body,NVDA,ai-infrastructure,bullish,long_term,medium,\n",
                encoding="utf-8",
            )
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            sample_export = kb.exports_dir / "example-event"
            sample_export.mkdir()
            (sample_export / "analysis.json").write_text("{}", encoding="utf-8")

            result = remove_sample_content(kb)

            self.assertFalse((kb.roles_dir / "sample-investor").exists())
            self.assertFalse((kb.events_dir / "example-event.md").exists())
            self.assertFalse(sample_export.exists())
            self.assertTrue(real_role.exists())
            self.assertIn("sample-investor", result["removed_roles"])
            self.assertIn("example-event.md", result["removed_events"])
            self.assertIn("example-event", result["removed_exports"])
            self.assertEqual(VoiceVaultIndex(kb).list_roles(), ["real-role"])

    def test_preview_sample_removal_does_not_delete_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            sample_export = kb.exports_dir / "example-event"
            sample_export.mkdir()
            (sample_export / "analysis.json").write_text("{}", encoding="utf-8")

            result = preview_sample_removal(kb)

            self.assertEqual(result["removed_roles"], ["sample-investor"])
            self.assertEqual(result["removed_events"], ["example-event.md"])
            self.assertEqual(result["removed_exports"], ["example-event"])
            self.assertTrue(result["dry_run"])
            self.assertTrue((kb.roles_dir / "sample-investor").exists())
            self.assertTrue((kb.events_dir / "example-event.md").exists())
            self.assertTrue(sample_export.exists())


if __name__ == "__main__":
    unittest.main()
