from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicevault.importers import load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb
from voicevault.roles import create_role, evaluate_role_coverage, list_role_summaries
from voicevault.sources import create_source


class RoleSummaryTests(unittest.TestCase):
    def test_list_role_summaries_includes_profile_status_and_statement_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            summaries = list_role_summaries(kb)

            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0]["role_id"], "sample-investor")
            self.assertEqual(summaries[0]["display_name"], "Sample Investor")
            self.assertEqual(summaries[0]["profile_status"], "reviewed")
            self.assertEqual(summaries[0]["statement_count"], 2)

    def test_create_role_writes_generated_profile_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")

            result = create_role(
                kb,
                role_id="public-analyst",
                display_name="Public Analyst",
                platform="x",
                source_url="https://x.com/public_analyst",
                tags=["semiconductors", "macro"],
                notes="公开观点来源，待补充 statement。",
            )

            profile_path = Path(result["generated_profile_path"])
            self.assertTrue(profile_path.is_file())
            self.assertTrue((kb.roles_dir / "public-analyst" / "statements").is_dir())
            text = profile_path.read_text(encoding="utf-8")
            self.assertIn("role_id: public-analyst", text)
            self.assertIn("display_name: Public Analyst", text)
            self.assertIn("profile_status: generated_unreviewed", text)
            self.assertIn("source_url: https://x.com/public_analyst", text)
            self.assertIn("- semiconductors", text)
            self.assertIn("公开观点来源", text)

            summaries = {item["role_id"]: item for item in list_role_summaries(kb)}
            self.assertEqual(summaries["public-analyst"]["display_name"], "Public Analyst")
            self.assertEqual(summaries["public-analyst"]["profile_status"], "generated_unreviewed")

    def test_create_role_refuses_existing_profile_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_role(kb, role_id="public-analyst", display_name="Public Analyst")

            with self.assertRaises(FileExistsError):
                create_role(kb, role_id="public-analyst", display_name="Duplicate")

    def test_list_role_summaries_uses_source_display_name_when_profile_name_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            role_dir = kb.roles_dir / "source-named-analyst"
            role_dir.mkdir(parents=True)
            (role_dir / "profile.md").write_text(
                "---\n"
                "role_id: source-named-analyst\n"
                "profile_status: reviewed\n"
                "---\n"
                "\n"
                "# Role Profile\n",
                encoding="utf-8",
                newline="\n",
            )
            create_source(
                kb,
                source_id="source-named-blog",
                role_id="source-named-analyst",
                platform="blog",
                display_name="Source Named Analyst",
            )

            summaries = {item["role_id"]: item for item in list_role_summaries(kb)}

            self.assertEqual(summaries["source-named-analyst"]["display_name"], "Source Named Analyst")

    def test_evaluate_role_coverage_requires_two_reviewed_roles_with_statements(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            coverage = evaluate_role_coverage(kb)

            self.assertFalse(coverage["ok"])
            self.assertEqual(coverage["min_reviewed_roles"], 2)
            self.assertEqual(coverage["reviewed_roles_with_statements"], 1)
            self.assertEqual(coverage["ready_role_ids"], ["sample-investor"])
            self.assertEqual(coverage["gaps"][0]["gap"], "needs_1_more_ready_role")
            self.assertIn("voicevault roles create", coverage["remediation"][0])

    def test_evaluate_role_coverage_passes_after_second_reviewed_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            _add_reviewed_growth_role(kb)
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            coverage = evaluate_role_coverage(kb)

            self.assertTrue(coverage["ok"])
            self.assertEqual(coverage["reviewed_roles"], 2)
            self.assertEqual(coverage["reviewed_roles_with_statements"], 2)
            self.assertEqual(set(coverage["ready_role_ids"]), {"sample-investor", "growth-investor"})
            self.assertFalse(coverage["gaps"])


def _add_reviewed_growth_role(kb) -> None:
    role_dir = kb.roles_dir / "growth-investor"
    role_dir.mkdir(parents=True)
    (role_dir / "profile.md").write_text(
        "---\n"
        "role_id: growth-investor\n"
        "display_name: Growth Investor\n"
        "profile_status: reviewed\n"
        "---\n"
        "\n"
        "# Growth Investor\n",
        encoding="utf-8",
        newline="\n",
    )
    (role_dir / "statements.csv").write_text(
        "statement_id,role_id,source_type,source_url,published_at,captured_at,title,body,symbols,topics,stance,time_horizon,confidence,notes\n"
        'growth-1,growth-investor,post,https://example.com/growth,2026-05-30,2026-05-31,Growth view,"NVDA demand remains durable because AI infrastructure spend is broadening.",NVDA,ai-infrastructure,bullish,long_term,medium,\n',
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    unittest.main()
