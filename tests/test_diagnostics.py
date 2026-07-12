from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicevault.diagnostics import inspect_kb, repair_kb
from voicevault.importers import load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import KnowledgeBase, init_kb


class DiagnosticsTests(unittest.TestCase):
    def test_inspect_kb_reports_missing_index_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")

            report = inspect_kb(kb)

            self.assertFalse(report["ok"])
            self.assertFalse(report["index_exists"])
            self.assertIn("Index has not been built.", report["warnings"])

    def test_inspect_kb_reports_counts_after_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            report = inspect_kb(kb)

            self.assertTrue(report["ok"])
            self.assertTrue(report["index_exists"])
            self.assertEqual(report["role_count"], 1)
            self.assertEqual(report["statement_count"], 2)

    def test_repair_kb_creates_missing_required_directories_without_sample_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb_root = Path(temp_dir) / "voicevault"
            (kb_root / "content" / "roles").mkdir(parents=True)
            (kb_root / "content" / "events").mkdir(parents=True)
            (kb_root / "content" / "topics").mkdir(parents=True)
            (kb_root / "content" / "reports").mkdir(parents=True)
            (kb_root / "inbox").mkdir(parents=True)
            (kb_root / "exports").mkdir(parents=True)
            (kb_root / ".voicevault").mkdir(parents=True)
            kb = KnowledgeBase.from_path(kb_root)
            captures = kb.inbox_captures_dir
            archive = kb.inbox_archive_dir
            sources = kb.sources_dir
            sample_role = kb.roles_dir / "sample-investor"

            repaired = repair_kb(kb)

            self.assertIn(str(captures), repaired["created_dirs"])
            self.assertIn(str(archive), repaired["created_dirs"])
            self.assertIn(str(sources), repaired["created_dirs"])
            self.assertTrue(captures.is_dir())
            self.assertTrue(archive.is_dir())
            self.assertTrue(sources.is_dir())
            self.assertFalse(sample_role.exists())


if __name__ == "__main__":
    unittest.main()
