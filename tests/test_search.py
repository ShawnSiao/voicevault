from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicevault.importers import load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb
from voicevault.search import search_statements


class SearchTests(unittest.TestCase):
    def test_search_statements_returns_ranked_statement_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            role_dir = kb.roles_dir / "macro-observer"
            role_dir.mkdir()
            (role_dir / "statements.csv").write_text(
                "statement_id,role_id,source_type,source_url,published_at,captured_at,title,body,symbols,topics,stance,time_horizon,confidence,notes\n"
                "rates-001,macro-observer,post,https://example.com/rates,2026-05-01,2026-05-02,Rates note,Treasury duration matters more than semiconductors,TLT,rates,neutral,medium_term,low,\n",
                encoding="utf-8",
            )
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            result = search_statements(kb, query="AI infrastructure NVDA", limit=3)

            self.assertEqual(result["query"], "AI infrastructure NVDA")
            self.assertGreaterEqual(result["count"], 1)
            self.assertEqual(result["results"][0]["role_id"], "sample-investor")
            self.assertGreater(result["results"][0]["score"], 0)
            self.assertIn("NVDA", result["results"][0]["symbols"])
            self.assertIn("source_url", result["results"][0])
            self.assertNotIn("rates-001", {item["statement_id"] for item in result["results"]})

    def test_search_statements_filters_by_role_symbol_and_topic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            result = search_statements(
                kb,
                query="margin",
                role_id="sample-investor",
                symbol="NVDA",
                topic="earnings",
                limit=10,
            )

            self.assertTrue(result["results"])
            self.assertTrue(all(item["role_id"] == "sample-investor" for item in result["results"]))
            self.assertTrue(all("NVDA" in item["symbols"] for item in result["results"]))
            self.assertTrue(all("earnings" in item["topics"] for item in result["results"]))

    def test_search_requires_more_than_symbol_only_hit_for_multi_term_queries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            role_dir = kb.roles_dir / "symbol-only"
            role_dir.mkdir()
            (role_dir / "statements.csv").write_text(
                "statement_id,role_id,source_type,source_url,published_at,captured_at,title,body,symbols,topics,stance,time_horizon,confidence,notes\n"
                "symbol-only-001,symbol-only,post,https://example.com/symbol,2026-05-01,2026-05-02,Portfolio basket,Only a broad basket mention without query context,NVDA,macro,unclear,long_term,low,\n",
                encoding="utf-8",
            )
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            result = search_statements(kb, query="NVDA margin", limit=10)

            self.assertNotIn("symbol-only-001", {item["statement_id"] for item in result["results"]})

    def test_search_expands_company_aliases_to_symbol_terms(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            role_dir = kb.roles_dir / "alias-source"
            role_dir.mkdir()
            (role_dir / "statements.csv").write_text(
                "statement_id,role_id,source_type,source_url,published_at,captured_at,title,body,symbols,topics,stance,time_horizon,confidence,notes\n"
                "alias-001,alias-source,post,https://example.com/alias,2026-05-01,2026-05-02,Capex note,AI infrastructure demand is still the core debate,NVDA,ai-infrastructure,unclear,long_term,medium,\n",
                encoding="utf-8",
            )
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            result = search_statements(kb, query="英伟达", limit=10)

            self.assertIn("nvda", result["expanded_terms"])
            self.assertIn("alias-001", {item["statement_id"] for item in result["results"]})

    def test_search_splits_chinese_compound_company_and_finance_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            role_dir = kb.roles_dir / "compound-query-source"
            role_dir.mkdir()
            (role_dir / "statements.csv").write_text(
                "statement_id,role_id,source_type,source_url,published_at,captured_at,title,body,symbols,topics,stance,time_horizon,confidence,notes\n"
                "compound-001,compound-query-source,post,https://example.com/margin,2026-05-01,2026-05-02,Margin note,NVDA margins could face pressure if customers and suppliers capture more economics,NVDA,margins,bearish,long_term,medium,\n",
                encoding="utf-8",
            )
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            result = search_statements(kb, query="英伟达利润率怎么看", role_id="compound-query-source", limit=10)

            self.assertIn("英伟达", result["terms"])
            self.assertIn("利润率", result["terms"])
            self.assertIn("nvda", result["expanded_terms"])
            self.assertIn("margins", result["expanded_terms"])
            self.assertIn("compound-001", {item["statement_id"] for item in result["results"]})

    def test_search_excerpt_removes_embedded_media_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            role_dir = kb.roles_dir / "media-noise-source"
            role_dir.mkdir()
            (role_dir / "statements.csv").write_text(
                "statement_id,role_id,source_type,source_url,published_at,captured_at,title,body,symbols,topics,stance,time_horizon,confidence,notes\n"
                "media-001,media-noise-source,post,https://example.com/media,2026-05-01,2026-05-02,Media note,英伟达利润率要看竞争和产能。 图片: - https://xqimg.imedao.com/ugc/images/face/emoji_76_rich.png?v=1 网页链接,NVDA,margins,mixed,long_term,medium,\n",
                encoding="utf-8",
            )
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            result = search_statements(kb, query="英伟达利润率怎么看", role_id="media-noise-source", limit=10)

            excerpt = result["results"][0]["excerpt"].lower()
            self.assertIn("英伟达利润率", excerpt)
            for noisy_fragment in ["http", "xqimg", "imedao", "emoji_76", "png", "网页链接"]:
                self.assertNotIn(noisy_fragment, excerpt)


if __name__ == "__main__":
    unittest.main()
