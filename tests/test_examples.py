from __future__ import annotations

import unittest
from pathlib import Path


class ExampleScriptTests(unittest.TestCase):
    def test_daily_report_script_builds_index_even_when_doctor_reports_missing_index(self) -> None:
        script = Path("examples/daily-us-market-report/run-voicevault-analysis.ps1").read_text(encoding="utf-8")

        self.assertIn("voicevault doctor --kb $KnowledgeBase", script)
        self.assertIn("$doctorExitCode = $LASTEXITCODE", script)
        self.assertIn("if ($doctorExitCode -notin @(0, 1))", script)
        self.assertIn("voicevault build --kb $KnowledgeBase", script)
        self.assertLess(script.index("voicevault doctor --kb $KnowledgeBase"), script.index("voicevault build --kb $KnowledgeBase"))


if __name__ == "__main__":
    unittest.main()
