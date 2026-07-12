from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicevault.importers import load_event, load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb


class IndexTests(unittest.TestCase):
    def test_rebuild_stores_statements_and_queries_relevant_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            statements = load_statements_from_kb(kb)
            index = VoiceVaultIndex(kb)

            count = index.rebuild(statements)
            roles = index.list_roles()
            event = load_event(kb.events_dir / "example-event.md")
            relevant = index.query_relevant(event)

            self.assertEqual(count, len(statements))
            self.assertTrue(kb.index_path.is_file())
            self.assertIn("sample-investor", roles)
            self.assertGreaterEqual(index.count_statements(), 1)
            self.assertIn("sample-investor", relevant)
            self.assertGreaterEqual(len(relevant["sample-investor"]), 1)


if __name__ == "__main__":
    unittest.main()
