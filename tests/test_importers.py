from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicevault.importers import load_event, load_role_statements, split_list
from voicevault.markdown import parse_frontmatter


class ImporterTests(unittest.TestCase):
    def test_split_list_accepts_commas_semicolons_and_lists(self) -> None:
        self.assertEqual(split_list("NVDA, MSFT; AAPL"), ["NVDA", "MSFT", "AAPL"])
        self.assertEqual(split_list(["earnings", "ai-infrastructure"]), ["earnings", "ai-infrastructure"])
        self.assertEqual(split_list(""), [])

    def test_parse_frontmatter_supports_scalars_and_lists(self) -> None:
        text = """---
title: NVIDIA Earnings
symbols:
  - NVDA
  - MSFT
topics:
  - earnings
---

# Body
"""

        metadata, body = parse_frontmatter(text)

        self.assertEqual(metadata["title"], "NVIDIA Earnings")
        self.assertEqual(metadata["symbols"], ["NVDA", "MSFT"])
        self.assertEqual(metadata["topics"], ["earnings"])
        self.assertEqual(body.strip(), "# Body")

    def test_load_role_statements_reads_csv_and_markdown_theses(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            role_dir = Path(temp_dir) / "sample-investor"
            theses_dir = role_dir / "theses"
            theses_dir.mkdir(parents=True)
            (role_dir / "statements.csv").write_text(
                "statement_id,role_id,source_type,source_url,published_at,captured_at,title,body,symbols,topics,stance,time_horizon,confidence,notes\n"
                ",sample-investor,post,https://example.com/a,2026-05-01,2026-05-02,NVDA note,Margin pressure matters,NVDA,earnings,mixed,short_term,medium,watch guidance\n",
                encoding="utf-8",
            )
            (theses_dir / "ai.md").write_text(
                """---
title: AI infrastructure thesis
source_url: https://example.com/thesis
published_at: 2026-05-03
symbols:
  - NVDA
topics:
  - ai-infrastructure
stance: bullish
time_horizon: long_term
confidence: high
---

Demand durability matters more than one quarter of margins.
""",
                encoding="utf-8",
            )

            statements = load_role_statements(role_dir)

            self.assertEqual(len(statements), 2)
            self.assertTrue(statements[0].statement_id.startswith("stmt_"))
            self.assertEqual(statements[0].role_id, "sample-investor")
            self.assertEqual(statements[0].symbols, ["NVDA"])
            self.assertEqual(statements[1].topics, ["ai-infrastructure"])
            self.assertIn("Demand durability", statements[1].body)

    def test_load_role_statements_reads_obsidian_statement_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            role_dir = Path(temp_dir) / "sample-investor"
            statements_dir = role_dir / "statements" / "x"
            statements_dir.mkdir(parents=True)
            (statements_dir / "2026-05-29-x-stmt_123.md").write_text(
                """---
statement_id: stmt_123
role_id: sample-investor
source_type: post
source_platform: x
source_user_id: sample_handle
source_author: Sample Investor
source_url: https://x.com/sample/status/123
published_at: 2026-05-29T12:00:00Z
captured_at: 2026-05-30T01:00:00Z
title: NVDA demand note
symbols:
  - NVDA
topics:
  - ai-infrastructure
stance: bullish
time_horizon: long_term
confidence: medium
---

# NVDA demand note

AI infrastructure demand remains durable.
""",
                encoding="utf-8",
            )

            statements = load_role_statements(role_dir)

            self.assertEqual(len(statements), 1)
            self.assertEqual(statements[0].statement_id, "stmt_123")
            self.assertEqual(statements[0].role_id, "sample-investor")
            self.assertEqual(statements[0].source_platform, "x")
            self.assertEqual(statements[0].source_user_id, "sample_handle")
            self.assertEqual(statements[0].source_author, "Sample Investor")
            self.assertEqual(statements[0].body, "AI infrastructure demand remains durable.")

    def test_load_event_reads_markdown_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            event_path = Path(temp_dir) / "event.md"
            event_path.write_text(
                """---
event_id: 2026-05-30-nvda-earnings
date: 2026-05-30
symbols:
  - NVDA
topics:
  - earnings
---

# NVIDIA Earnings

NVIDIA beat revenue expectations.
""",
                encoding="utf-8",
            )

            event = load_event(event_path)

            self.assertEqual(event.event_id, "2026-05-30-nvda-earnings")
            self.assertEqual(event.title, "NVIDIA Earnings")
            self.assertEqual(event.symbols, ["NVDA"])
            self.assertIn("beat revenue", event.summary)


if __name__ == "__main__":
    unittest.main()
