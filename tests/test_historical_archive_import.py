from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voicevault.app_db import AppDatabase
from voicevault.historical_archive_import import HistoricalArchiveImporter
from voicevault.person_archive import PersonRepository, PlatformAccountRepository


class HistoricalArchiveImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.database = AppDatabase(data_dir=self.root / "runtime")
        self.database.initialize()
        self.person = PersonRepository(self.database).create("示例研究者")
        self.account = PlatformAccountRepository(self.database).bind(
            self.person.person_id,
            platform="xueqiu",
            external_user_id="1000000000",
            display_name="示例研究者",
        )
        self.archive_path = self.root / "posts.json"
        self.archive_path.write_text(
            json.dumps(
                [
                    {
                        "post_id": "100",
                        "author": "示例研究者",
                        "content": "第一篇\r\n正文",
                        "content_complete": True,
                        "publish_time_text": "2024-05-19 13:51· 来自Android",
                        "collected_at": "2026-06-14T23:16:55.519Z",
                        "source_url": "https://xueqiu.com/1000000000/100",
                    },
                    {
                        "post_id": "101",
                        "author": "示例研究者",
                        "content": "重复一",
                        "content_complete": True,
                        "publish_time_text": "2024-05-19 13:52· 来自Android",
                        "collected_at": "2026-06-14T23:16:55.519Z",
                        "source_url": "https://xueqiu.com/1000000000/101",
                    },
                    {
                        "post_id": "101",
                        "author": "示例研究者",
                        "content": "重复二",
                        "content_complete": True,
                        "publish_time_text": "2024-05-19 13:52· 来自Android",
                        "collected_at": "2026-06-14T23:16:55.519Z",
                        "source_url": "https://xueqiu.com/1000000000/101",
                    },
                    {
                        "post_id": "102",
                        "author": "示例研究者",
                        "content": "时间错误",
                        "content_complete": True,
                        "publish_time_text": "昨天",
                        "collected_at": "2026-06-14T23:16:55.519Z",
                        "source_url": "https://xueqiu.com/1000000000/102",
                    },
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_preview_is_read_only_and_reports_validation_issues(self) -> None:
        importer = HistoricalArchiveImporter(self.database)

        report = importer.preview(
            self.archive_path,
            person_id=self.person.person_id,
            account_id=self.account.account_id,
        )

        self.assertEqual(report["total_records"], 4)
        self.assertEqual(report["valid_records"], 1)
        self.assertEqual(report["duplicate_records"], 2)
        self.assertEqual(report["time_parse_errors"], 1)
        self.assertEqual(report["skipped_records"], 3)
        self.assertEqual(len(report["source_fingerprint"]), 64)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 0)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM legacy_import_runs").fetchone()[0], 0
            )

    def test_import_binds_to_existing_account_is_idempotent_and_creates_no_coverage(self) -> None:
        importer = HistoricalArchiveImporter(self.database)

        first = importer.import_archive(
            self.archive_path,
            person_id=self.person.person_id,
            account_id=self.account.account_id,
        )
        second = importer.import_archive(
            self.archive_path,
            person_id=self.person.person_id,
            account_id=self.account.account_id,
        )

        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(first["imported"], {"posts": 1, "revisions": 1})
        self.assertEqual(first["skipped_records"], 3)
        self.assertEqual(second["imported"], {"posts": 0, "revisions": 0})
        self.assertEqual(second["existing"], {"posts": 1, "revisions": 1})
        with self.database.connect() as connection:
            post = connection.execute(
                "SELECT account_id, published_at, canonical_url FROM posts"
            ).fetchone()
            revision = connection.execute(
                "SELECT content_text, captured_at, first_seen_job_id FROM post_revisions"
            ).fetchone()
            self.assertEqual(post["account_id"], self.account.account_id)
            self.assertEqual(post["published_at"], "2024-05-19T05:51:00+00:00")
            self.assertEqual(post["canonical_url"], "https://xueqiu.com/1000000000/100")
            self.assertEqual(revision["content_text"], "第一篇\n正文")
            self.assertEqual(revision["captured_at"], "2026-06-14T23:16:55.519000+00:00")
            self.assertIsNone(revision["first_seen_job_id"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM coverage_intervals").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM post_observations").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM legacy_import_runs").fetchone()[0], 2)

    def test_preview_resolves_modified_month_day_and_yesterday_timestamps(self) -> None:
        archive = self.root / "relative-times.json"
        archive.write_text(
            json.dumps(
                [
                    {
                        "post_id": "200",
                        "content": "修改日期",
                        "content_complete": True,
                        "publish_time_text": "修改于2026-06-15 18:14· 来自Android",
                        "collected_at": "2026-06-16T04:00:00Z",
                        "source_url": "https://xueqiu.com/1000000000/200",
                    },
                    {
                        "post_id": "201",
                        "content": "跨年日期",
                        "content_complete": True,
                        "publish_time_text": "12-31 23:00· 来自Android",
                        "collected_at": "2026-01-02T04:00:00Z",
                        "source_url": "https://xueqiu.com/1000000000/201",
                    },
                    {
                        "post_id": "202",
                        "content": "相对日期",
                        "content_complete": True,
                        "publish_time_text": "昨天 11:31· 来自Android",
                        "collected_at": "2026-06-16T04:00:00Z",
                        "source_url": "https://xueqiu.com/1000000000/202",
                    },
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        report = HistoricalArchiveImporter(self.database).import_archive(
            archive, person_id=self.person.person_id, account_id=self.account.account_id
        )

        self.assertEqual(report["valid_records"], 3)
        self.assertEqual(report["time_parse_errors"], 0)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT external_post_id, published_at FROM posts ORDER BY external_post_id"
            ).fetchall()
        self.assertEqual(
            [(row["external_post_id"], row["published_at"]) for row in rows],
            [
                ("200", "2026-06-15T10:14:00+00:00"),
                ("201", "2025-12-31T15:00:00+00:00"),
                ("202", "2026-06-15T03:31:00+00:00"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
