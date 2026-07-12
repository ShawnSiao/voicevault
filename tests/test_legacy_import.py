from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from voicevault.app_db import AppDatabase
from voicevault.legacy_import import LegacyImporter


class LegacyImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.legacy = self.root / "legacy"
        role = self.legacy / "content" / "roles" / "researcher"
        role.mkdir(parents=True)
        (role / "statements.csv").write_text(
            "statement_id,role_id,source_type,source_url,published_at,captured_at,title,body,symbols,topics,stance,time_horizon,confidence,notes,source_platform,source_user_id,source_author\n"
            "s1,researcher,post,https://xueqiu.com/123/1,2026-07-01T00:00:00Z,2026-07-02T00:00:00Z,One,alpha view,,,unclear,unknown,low,,xueqiu,123,Alice\n"
            "s2,researcher,post,https://example.test/alice/2,2026-07-01T00:00:00Z,2026-07-02T00:00:00Z,Two,beta view,,,unclear,unknown,low,,example,alice,Alice\n"
            "s3,researcher,post,https://example.test/unknown,2026-07-01T00:00:00Z,2026-07-02T00:00:00Z,Unknown,skip me,,,unclear,unknown,low,,,,Alice\n",
            encoding="utf-8",
        )
        self.database = AppDatabase(data_dir=self.root / "runtime")
        self.database.initialize()

    def test_import_is_source_read_only_idempotent_and_never_creates_coverage(self) -> None:
        before = self._source_hashes()
        importer = LegacyImporter(self.database)
        first = importer.import_kb(self.legacy)
        counts = self._business_counts()
        second = importer.import_kb(self.legacy)

        self.assertEqual(first["imported"], {"persons": 1, "accounts": 2, "posts": 2, "revisions": 2})
        self.assertEqual(len(first["skipped"]), 1)
        self.assertEqual(second["imported"], {"persons": 0, "accounts": 0, "posts": 0, "revisions": 0})
        self.assertEqual(second["existing"]["posts"], 2)
        self.assertEqual(self._business_counts(), counts)
        self.assertEqual(self._source_hashes(), before)
        self.assertEqual(counts["coverage_intervals"], 0)
        with self.database.connect() as connection:
            mapping = connection.execute(
                "SELECT source_key, target_id FROM legacy_import_mappings WHERE source_kind = 'role'"
            ).fetchone()
        self.assertEqual(mapping["source_key"], "researcher")
        self.assertNotEqual(mapping["target_id"], "researcher")

    def _source_hashes(self) -> dict[str, str]:
        return {
            path.relative_to(self.legacy).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.legacy.rglob("*") if path.is_file()
        }

    def _business_counts(self) -> dict[str, int]:
        with self.database.connect() as connection:
            return {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("persons", "platform_accounts", "posts", "post_revisions", "coverage_intervals")
            }


if __name__ == "__main__":
    unittest.main()
