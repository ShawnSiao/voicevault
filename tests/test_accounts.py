from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voicevault.accounts import collect_account, create_account, list_accounts, read_account_status
from voicevault.kb import init_kb


RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Public Analyst</title>
    <item>
      <guid>post-1</guid>
      <title>First public post</title>
      <link>https://example.com/public/post-1</link>
      <pubDate>Tue, 09 Jun 2026 08:00:00 GMT</pubDate>
      <description><![CDATA[First public archive text.]]></description>
    </item>
    <item>
      <guid>post-2</guid>
      <title>Second public post</title>
      <link>https://example.com/public/post-2</link>
      <pubDate>Tue, 09 Jun 2026 09:00:00 GMT</pubDate>
      <description><![CDATA[Second public archive text.]]></description>
    </item>
  </channel>
</rss>
"""


class AccountArchiveTests(unittest.TestCase):
    def test_create_account_stores_config_and_source_with_auto_rss_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            feed_path = Path(temp_dir) / "feed.xml"
            feed_path.write_text(RSS_FIXTURE, encoding="utf-8")

            account = create_account(
                kb,
                account_id="rss-public-analyst",
                platform="rss",
                platform_account_id="public-analyst",
                role_id="public-analyst",
                display_name="Public Analyst",
                collection_mode="auto",
                feed_url=str(feed_path),
            )
            accounts = list_accounts(kb)

            self.assertEqual(account["collection_mode"], "rss")
            self.assertEqual(account["source_id"], "account-rss-public-analyst")
            self.assertTrue(Path(account["config_path"]).is_file())
            self.assertEqual(accounts[0]["account_id"], "rss-public-analyst")
            self.assertEqual(accounts[0]["adapter_config"]["feed_url"], str(feed_path))
            self.assertTrue((kb.sources_dir / "account-rss-public-analyst.json").is_file())

    def test_create_account_defaults_restricted_platform_without_input_to_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")

            account = create_account(
                kb,
                account_id="xueqiu-example-researcher",
                platform="xueqiu",
                platform_account_id="example-researcher",
                role_id="example-researcher",
                collection_mode="auto",
            )
            result = collect_account(kb, "xueqiu-example-researcher")

            self.assertEqual(account["collection_mode"], "blocked")
            self.assertFalse(result["ok"])
            self.assertIn("blocked", result["status"])
            self.assertEqual(result["written"], 0)
            self.assertFalse(Path(result["capture_path"]).exists())

    def test_collect_account_reads_rss_feed_writes_capture_and_skips_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            feed_path = Path(temp_dir) / "feed.xml"
            feed_path.write_text(RSS_FIXTURE, encoding="utf-8")
            create_account(
                kb,
                account_id="rss-public-analyst",
                platform="rss",
                platform_account_id="public-analyst",
                role_id="public-analyst",
                display_name="Public Analyst",
                collection_mode="rss",
                feed_url=str(feed_path),
            )

            first = collect_account(kb, "rss-public-analyst")
            second = collect_account(kb, "rss-public-analyst")
            capture_path = Path(first["capture_path"])
            records = [json.loads(line) for line in capture_path.read_text(encoding="utf-8").splitlines()]
            status = read_account_status(kb)

            self.assertTrue(first["ok"])
            self.assertEqual(first["record_count"], 2)
            self.assertEqual(first["written"], 2)
            self.assertEqual(first["duplicates_skipped"], 0)
            self.assertTrue(second["ok"])
            self.assertEqual(second["written"], 0)
            self.assertEqual(second["duplicates_skipped"], 2)
            self.assertEqual(records[0]["statement_id"], "post-1")
            self.assertEqual(records[0]["role_id"], "public-analyst")
            self.assertEqual(records[0]["platform"], "rss")
            self.assertEqual(records[0]["platform_user_id"], "public-analyst")
            self.assertEqual(records[0]["author"], "Public Analyst")
            self.assertEqual(records[0]["text"], "First public archive text.")
            self.assertEqual(status["summary"]["total"], 1)
            self.assertEqual(status["summary"]["active"], 1)
            self.assertEqual(status["accounts"][0]["cursor"]["record_count"], 2)

    def test_collect_account_reads_local_export_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            export_path = Path(temp_dir) / "export.jsonl"
            export_path.write_text(
                json.dumps(
                    {
                        "id": "local-1",
                        "title": "Local export post",
                        "text": "Local public export text.",
                        "url": "https://example.com/local/1",
                        "published_at": "2026-06-09T10:00:00Z",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            create_account(
                kb,
                account_id="local-public-analyst",
                platform="local-export",
                platform_account_id="public-analyst",
                role_id="public-analyst",
                collection_mode="local-export",
                input_path=str(export_path),
            )

            result = collect_account(kb, "local-public-analyst")
            records = [json.loads(line) for line in Path(result["capture_path"]).read_text(encoding="utf-8").splitlines()]

            self.assertTrue(result["ok"])
            self.assertEqual(result["written"], 1)
            self.assertEqual(records[0]["statement_id"], "local-1")
            self.assertEqual(records[0]["text"], "Local public export text.")

    def test_collect_account_resolves_local_export_relative_to_kb_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            export_path = kb.inbox_dir / "exports" / "relative-export.jsonl"
            export_path.parent.mkdir(parents=True, exist_ok=True)
            export_path.write_text(
                json.dumps({"id": "relative-1", "text": "Relative local export text."}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            create_account(
                kb,
                account_id="relative-local-public-analyst",
                platform="local-export",
                platform_account_id="public-analyst",
                role_id="public-analyst",
                collection_mode="local-export",
                input_path="inbox/exports/relative-export.jsonl",
            )

            result = collect_account(kb, "relative-local-public-analyst")
            records = [json.loads(line) for line in Path(result["capture_path"]).read_text(encoding="utf-8").splitlines()]

            self.assertTrue(result["ok"])
            self.assertEqual(records[0]["statement_id"], "relative-1")
            self.assertEqual(records[0]["text"], "Relative local export text.")

    def test_collect_account_reads_custom_api_with_injected_fetcher(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_account(
                kb,
                account_id="api-public-analyst",
                platform="custom-api",
                platform_account_id="public-analyst",
                role_id="public-analyst",
                collection_mode="custom-api",
                api_url="https://api.example.com/posts",
            )

            result = collect_account(
                kb,
                "api-public-analyst",
                fetcher=lambda account: {
                    "items": [
                        {
                            "id": "api-1",
                            "title": "API post",
                            "content": "Authorized API text.",
                            "link": "https://example.com/api/1",
                            "created_at": "2026-06-09T11:00:00Z",
                        }
                    ]
                },
            )
            records = [json.loads(line) for line in Path(result["capture_path"]).read_text(encoding="utf-8").splitlines()]

            self.assertTrue(result["ok"])
            self.assertEqual(result["written"], 1)
            self.assertEqual(records[0]["statement_id"], "api-1")
            self.assertEqual(records[0]["text"], "Authorized API text.")


if __name__ == "__main__":
    unittest.main()
