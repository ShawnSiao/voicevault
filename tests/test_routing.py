from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicevault.importers import load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb
from voicevault.routing import suggest_roles


class RoleRoutingTests(unittest.TestCase):
    def test_suggest_roles_returns_ranked_role_from_local_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            result = suggest_roles(kb, "NVDA margin", symbol="NVDA", limit=3)

            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(result["query"], "NVDA margin")
            self.assertEqual(result["suggested_role_id"], "sample-investor")
            self.assertIn(result["confidence"], {"low", "medium", "high"})
            self.assertGreaterEqual(result["route_count"], 1)
            self.assertGreater(result["routes"][0]["score"], 0)
            self.assertGreaterEqual(result["routes"][0]["evidence_count"], 1)
            self.assertEqual(result["routes"][0]["role_id"], "sample-investor")
            self.assertTrue(result["routes"][0]["reason"])
            self.assertTrue(result["routes"][0]["evidence"])

    def test_suggest_roles_is_explicit_when_no_role_has_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            result = suggest_roles(kb, "unmatched rare query", limit=3)

            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(result["suggested_role_id"], "")
            self.assertEqual(result["confidence"], "none")
            self.assertEqual(result["route_count"], 0)
            self.assertEqual(result["routes"], [])
            self.assertIn("No indexed role evidence", result["no_match_reason"])


if __name__ == "__main__":
    unittest.main()
