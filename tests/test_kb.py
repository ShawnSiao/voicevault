from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicevault.kb import init_kb


class KnowledgeBaseInitTests(unittest.TestCase):
    def test_init_creates_obsidian_openable_structure_and_templates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb_root = Path(temp_dir) / "voicevault"

            kb = init_kb(kb_root)

            expected_dirs = [
                kb_root / "content" / "roles",
                kb_root / "content" / "events",
                kb_root / "content" / "topics",
                kb_root / "content" / "reports",
                kb_root / "content" / "sources",
                kb_root / "inbox",
                kb_root / "inbox" / "captures",
                kb_root / "inbox" / "archive",
                kb_root / "exports",
                kb_root / ".voicevault",
                kb_root / "content" / "roles" / "sample-investor" / "theses",
            ]
            for path in expected_dirs:
                self.assertTrue(path.is_dir(), str(path))

            self.assertEqual(kb.root, kb_root)
            self.assertTrue((kb_root / "content" / "roles" / "sample-investor" / "profile.md").is_file())
            self.assertTrue((kb_root / "content" / "roles" / "sample-investor" / "statements.csv").is_file())
            self.assertTrue((kb_root / "content" / "events" / "example-event.md").is_file())

    def test_init_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb_root = Path(temp_dir) / "voicevault"

            first = init_kb(kb_root)
            second = init_kb(kb_root)

            self.assertEqual(first.root, second.root)
            self.assertTrue((kb_root / "content" / "events" / "example-event.md").is_file())


if __name__ == "__main__":
    unittest.main()
