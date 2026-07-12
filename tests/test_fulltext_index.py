from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from voicevault.fulltext_index import (
    FullTextConflict,
    FullTextDocument,
    FullTextIndexInvalid,
    FullTextSearchFilters,
    FullTextUnavailable,
    LocalFullTextIndexProvider,
)


UTC = timezone.utc
GENERATION_A = "11111111-1111-4111-8111-111111111111"
GENERATION_B = "22222222-2222-4222-8222-222222222222"


def instant(day: int) -> datetime:
    return datetime(2026, 7, day, tzinfo=UTC)


def document(
    chunk_id: str,
    text: str,
    *,
    person_id: str = "person-a",
    platform: str = "xueqiu",
    published_at: datetime | None = None,
) -> FullTextDocument:
    return FullTextDocument(
        chunk_id=chunk_id,
        person_id=person_id,
        platform=platform,
        published_at=published_at or instant(5),
        text=text,
    )


class LocalFullTextIndexProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_dir = Path(self.temp_dir.name)
        self.provider = LocalFullTextIndexProvider(self.data_dir)

    def test_two_character_chinese_name_matches_bigram_channel(self) -> None:
        self.provider.build(
            GENERATION_A,
            (document("chunk-zh", "张伟长期关注新能源"), document("chunk-other", "李明关注消费")),
        )

        hits = self.provider.search(
            GENERATION_A, "张伟", FullTextSearchFilters(), limit=10
        )

        self.assertEqual([hit.chunk_id for hit in hits], ["chunk-zh"])
        self.assertEqual(hits[0].matched_channels, ("cjk_bigram",))
        self.assertEqual(hits[0].channel_ranks, (("cjk_bigram", 1),))

    def test_trigram_matches_long_chinese_english_and_numbers(self) -> None:
        self.provider.build(
            GENERATION_A,
            (
                document("chunk-cn", "人工智能正在改变研究流程"),
                document("chunk-en", "VoiceVault supports durable evidence retrieval"),
                document("chunk-num", "Revenue increased by 123456 units"),
            ),
        )

        self.assertEqual(
            self.provider.search(GENERATION_A, "人工智能", FullTextSearchFilters(), 5)[0].chunk_id,
            "chunk-cn",
        )
        self.assertEqual(
            self.provider.search(GENERATION_A, "VoiceVault", FullTextSearchFilters(), 5)[0].chunk_id,
            "chunk-en",
        )
        self.assertEqual(
            self.provider.search(GENERATION_A, "123456", FullTextSearchFilters(), 5)[0].chunk_id,
            "chunk-num",
        )

    def test_dual_channel_hit_is_deduplicated_with_traceable_channel_ranks(self) -> None:
        self.provider.build(
            GENERATION_A,
            (document("chunk-both", "张伟投资框架"), document("chunk-second", "张伟投资记录")),
        )

        hits = self.provider.search(
            GENERATION_A, "张伟投资", FullTextSearchFilters(), limit=10
        )

        self.assertEqual(len({hit.chunk_id for hit in hits}), len(hits))
        self.assertEqual(hits[0].matched_channels, ("trigram", "cjk_bigram"))
        self.assertEqual(dict(hits[0].channel_ranks), {"trigram": 1, "cjk_bigram": 1})
        self.assertEqual([hit.rank for hit in hits], list(range(1, len(hits) + 1)))

    def test_person_platform_and_half_open_published_filters_are_combined(self) -> None:
        self.provider.build(
            GENERATION_A,
            (
                document("chunk-a", "common phrase", person_id="person-a", platform="xueqiu", published_at=instant(2)),
                document("chunk-b", "common phrase", person_id="person-b", platform="wechat", published_at=instant(3)),
                document("chunk-end", "common phrase", person_id="person-a", platform="xueqiu", published_at=instant(4)),
                FullTextDocument("chunk-null", "person-a", "xueqiu", None, "common phrase"),
            ),
        )
        filters = FullTextSearchFilters(
            person_ids=("person-a",),
            platforms=("xueqiu",),
            published_from=instant(2),
            published_to=instant(4),
        )

        hits = self.provider.search(GENERATION_A, "common", filters, 10)

        self.assertEqual([hit.chunk_id for hit in hits], ["chunk-a"])

    def test_allowed_chunks_filter_before_channel_ranks_are_assigned(self) -> None:
        self.provider.build(
            GENERATION_A,
            (
                document("chunk-a", "common common common"),
                document("chunk-b", "common phrase"),
            ),
        )

        hits = self.provider.search(
            GENERATION_A,
            "common",
            FullTextSearchFilters(allowed_chunk_ids=("chunk-b",)),
            10,
        )

        self.assertEqual([(hit.chunk_id, hit.rank) for hit in hits], [("chunk-b", 1)])

    def test_generations_are_isolated_and_fresh_empty_directory_can_rebuild(self) -> None:
        relative_a = self.provider.build(GENERATION_A, (document("chunk-a", "alpha token"),))
        self.provider.build(GENERATION_B, (document("chunk-b", "beta token"),))

        self.assertEqual(self.provider.search(GENERATION_A, "alpha", FullTextSearchFilters(), 5)[0].chunk_id, "chunk-a")
        self.assertEqual(self.provider.search(GENERATION_A, "beta", FullTextSearchFilters(), 5), ())
        self.assertEqual(self.provider.search(GENERATION_B, "beta", FullTextSearchFilters(), 5)[0].chunk_id, "chunk-b")
        self.assertEqual(relative_a, f"indexes/generations/{GENERATION_A}/fulltext.sqlite")
        self.assertFalse(Path(relative_a).is_absolute())

        with tempfile.TemporaryDirectory() as fresh_dir:
            rebuilt = LocalFullTextIndexProvider(fresh_dir)
            rebuilt.build(GENERATION_A, (document("chunk-a", "alpha token"),))
            self.assertEqual(rebuilt.search(GENERATION_A, "alpha", FullTextSearchFilters(), 5)[0].chunk_id, "chunk-a")

    def test_existing_generation_is_reused_only_for_identical_documents_and_schema(self) -> None:
        documents = (document("chunk-a", "stable content"),)
        first = self.provider.build(GENERATION_A, documents)
        final = self.data_dir / Path(first)
        before = (final.stat().st_ino, final.stat().st_mtime_ns, final.read_bytes())

        second = self.provider.build(GENERATION_A, documents)

        self.assertEqual(second, first)
        self.assertEqual((final.stat().st_ino, final.stat().st_mtime_ns, final.read_bytes()), before)
        self.assertTrue(tuple(final.parent.glob("*.staging.sqlite")))
        with self.assertRaises(FullTextConflict):
            self.provider.build(GENERATION_A, (document("chunk-a", "changed content"),))
        self.assertEqual(final.read_bytes(), before[2])

    def test_fts5_unavailable_is_explicit_and_never_semantic_degraded(self) -> None:
        with patch(
            "voicevault.fulltext_index.sqlite3.connect",
            side_effect=sqlite3.OperationalError("no such module: fts5"),
        ):
            with self.assertRaises(FullTextUnavailable) as raised:
                self.provider.build(GENERATION_A, (document("chunk-a", "content"),))
        self.assertNotIn(str(self.data_dir), str(raised.exception))

    def test_corrupt_or_symlink_final_is_rejected_without_absolute_path_leak(self) -> None:
        generation_dir = self.data_dir / "indexes" / "generations" / GENERATION_A
        generation_dir.mkdir(parents=True)
        final = generation_dir / "fulltext.sqlite"
        final.write_bytes(b"not sqlite")
        with self.assertRaises(FullTextIndexInvalid) as corrupt:
            self.provider.search(GENERATION_A, "query", FullTextSearchFilters(), 5)
        self.assertNotIn(str(self.data_dir), str(corrupt.exception))

        if hasattr(Path, "symlink_to"):
            with tempfile.TemporaryDirectory() as other_dir:
                target = Path(other_dir) / "outside.sqlite"
                target.write_bytes(b"outside")
                other_generation = self.data_dir / "indexes" / "generations" / GENERATION_B
                other_generation.mkdir(parents=True)
                link = other_generation / "fulltext.sqlite"
                try:
                    link.symlink_to(target)
                except OSError:
                    return
                with self.assertRaises(FullTextIndexInvalid) as linked:
                    self.provider.search(GENERATION_B, "query", FullTextSearchFilters(), 5)
                self.assertNotIn(str(self.data_dir), str(linked.exception))

    def test_documents_queries_filters_generation_and_limit_are_strict(self) -> None:
        invalid_generations = (
            "",
            "../escape",
            "NOT-A-UUID",
            "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
        )
        for generation_id in invalid_generations:
            with self.subTest(generation_id=generation_id), self.assertRaises(ValueError):
                self.provider.build(generation_id, ())
        with self.assertRaises(ValueError):
            self.provider.build(GENERATION_A, [document("chunk", "text")])  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            FullTextSearchFilters(person_ids="person-a")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            FullTextSearchFilters(platforms=["xueqiu"])  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.provider.build(
                GENERATION_A,
                (document("chunk", "one"), document("chunk", "two")),
            )
        self.provider.build(GENERATION_A, (document("chunk", "valid content"),))
        for query, filters, limit in (
            ("", FullTextSearchFilters(), 5),
            ("valid", FullTextSearchFilters(person_ids=("",)), 5),
            ("valid", FullTextSearchFilters(published_from=instant(4), published_to=instant(2)), 5),
            ("valid", FullTextSearchFilters(), 0),
            ("valid", FullTextSearchFilters(), True),
        ):
            with self.assertRaises(ValueError):
                self.provider.search(GENERATION_A, query, filters, limit)  # type: ignore[arg-type]

    def test_build_does_not_touch_legacy_index_or_search_modules(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        legacy_paths = (repo_root / "src/voicevault/index.py", repo_root / "src/voicevault/search.py")
        before = {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in legacy_paths}

        self.provider.build(GENERATION_A, (document("chunk", "legacy untouched"),))

        self.assertEqual(
            {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in legacy_paths},
            before,
        )


if __name__ == "__main__":
    unittest.main()
