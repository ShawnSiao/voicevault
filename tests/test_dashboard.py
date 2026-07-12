from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicevault.collections import create_evidence_pack
from voicevault.dashboard import write_dashboard
from voicevault.importers import load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb
from voicevault.sync import sync_once


class DashboardTests(unittest.TestCase):
    def test_write_dashboard_creates_static_html_from_real_kb_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            sync_once(kb)
            create_evidence_pack(kb, title="NVDA Margin Evidence", query="NVDA margin", symbol="NVDA")

            output = write_dashboard(kb)
            html = output.read_text(encoding="utf-8")

            self.assertEqual(output, kb.exports_dir / "dashboard" / "index.html")
            self.assertIn("<title>VoiceVault Dashboard</title>", html)
            self.assertIn("声迹 VoiceVault", html)
            self.assertIn("sample-investor", html)
            self.assertIn("example-nvda-margin", html)
            self.assertIn("Statements", html)
            self.assertIn("Reports", html)
            self.assertIn("NVDA Margin Evidence", html)
            self.assertIn("Sync Status", html)
            self.assertIn("Capture Status", html)
            self.assertIn("Pending Files", html)
            self.assertIn("No runtime server required", html)


if __name__ == "__main__":
    unittest.main()
