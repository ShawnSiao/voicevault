from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from voicevault.app_db import AppDatabase
from voicevault.knowledge_chunks import (
    ChunkingRule,
    KnowledgeChunkRepository,
    chunk_revision,
)


NOW = "2026-07-11T00:00:00Z"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class KnowledgeChunkingTests(unittest.TestCase):
    def test_default_rule_and_short_revision_are_frozen_deterministic_single_chunk(self) -> None:
        rule = ChunkingRule()
        first = chunk_revision("revision-1", "短帖🙂", rule)
        second = chunk_revision("revision-1", "短帖🙂", rule)

        self.assertEqual((rule.version, rule.max_chars, rule.overlap_chars), ("paragraph-window-v1", 1200, 160))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertEqual((first[0].char_start, first[0].char_end, first[0].text), (0, 3, "短帖🙂"))
        with self.assertRaises(FrozenInstanceError):
            first[0].ordinal = 7  # type: ignore[misc]

    def test_paragraph_boundaries_are_preferred_and_offsets_slice_original_text(self) -> None:
        text = "aaaa\n\nbbbb\n\ncccc"
        chunks = chunk_revision(
            "revision-paragraphs", text, ChunkingRule(max_chars=12, overlap_chars=3)
        )

        self.assertEqual(chunks[0].char_end, 12)
        self.assertTrue(chunks[0].text.endswith("\n\n"))
        self.assertEqual(chunks[0].char_end - chunks[1].char_start, 3)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.text), 12)
            self.assertEqual(chunk.text, text[chunk.char_start : chunk.char_end])

    def test_long_paragraph_uses_fixed_unicode_windows_with_bounded_overlap(self) -> None:
        text = "甲🙂乙丙丁戊己庚辛壬癸子丑寅卯辰巳午"
        chunks = chunk_revision(
            "revision-window", text, ChunkingRule(max_chars=8, overlap_chars=2)
        )

        self.assertGreater(len(chunks), 1)
        self.assertEqual([chunk.ordinal for chunk in chunks], list(range(len(chunks))))
        for index, chunk in enumerate(chunks):
            self.assertEqual(chunk.text, text[chunk.char_start : chunk.char_end])
            self.assertLessEqual(len(chunk.text), 8)
            if index:
                overlap = chunks[index - 1].char_end - chunk.char_start
                self.assertGreaterEqual(overlap, 0)
                self.assertLessEqual(overlap, 2)

    def test_rule_and_revision_inputs_reject_nonprogressing_or_empty_values(self) -> None:
        for kwargs in (
            {"version": ""},
            {"max_chars": 0},
            {"max_chars": 10, "overlap_chars": 10},
            {"overlap_chars": -1},
        ):
            with self.assertRaises(ValueError):
                ChunkingRule(**kwargs)
        with self.assertRaises(ValueError):
            chunk_revision("", "body", ChunkingRule())
        with self.assertRaises(ValueError):
            chunk_revision("revision", "", ChunkingRule())


class KnowledgeChunkRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = AppDatabase(data_dir=Path(self.temp_dir.name))
        self.database.initialize()
        self.repository = KnowledgeChunkRepository(self.database)
        with self.database.transaction() as connection:
            connection.execute("INSERT INTO persons VALUES ('person-1', 'Alice', ?, ?)", (NOW, NOW))
            connection.execute(
                """
                INSERT INTO platform_accounts(
                    account_id, person_id, platform, external_user_id,
                    archive_basis_confirmed_at, created_at, updated_at
                ) VALUES ('account-1', 'person-1', 'xueqiu', '12345', ?, ?, ?)
                """,
                (NOW, NOW, NOW),
            )

    def _post(self, post_id: str, state: str = "active") -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO posts VALUES (?, 'account-1', ?, ?, ?, ?)",
                (post_id, f"external-{post_id}", NOW, f"https://xueqiu.com/12345/{post_id}", NOW),
            )
            reason = None if state == "active" else f"{state} by user"
            purged_hash = digest("purged") if state == "purged" else None
            connection.execute(
                "INSERT INTO content_dispositions VALUES (?, ?, ?, ?, ?)",
                (post_id, state, reason, NOW, purged_hash),
            )

    def _revision(self, post_id: str, revision_id: str, text: str, captured_at: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, NULL)",
                (revision_id, post_id, digest(text), text, captured_at),
            )

    def test_materialize_selects_current_revision_by_captured_at_then_revision_id(self) -> None:
        self._post("post-1")
        self._revision("post-1", "revision-a", "older", "2026-07-10T00:00:00Z")
        self._revision("post-1", "revision-b", "tie loser", NOW)
        self._revision("post-1", "revision-c", "tie winner", NOW)

        with self.database.transaction() as connection:
            first = self.repository.materialize_current(connection, "person-1", ChunkingRule())
            second = self.repository.materialize_current(connection, "person-1", ChunkingRule())
            stored_count = connection.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]

        self.assertEqual({chunk.revision_id for chunk in first}, {"revision-c"})
        self.assertEqual(first, second)
        self.assertEqual(stored_count, len(first))

    def test_new_revision_replaces_old_revision_in_current_materialization(self) -> None:
        self._post("post-1")
        self._revision("post-1", "revision-1", "old body", "2026-07-10T00:00:00Z")
        with self.database.transaction() as connection:
            old = self.repository.materialize_current(connection, "person-1", ChunkingRule())
        self._revision("post-1", "revision-2", "new body", NOW)

        with self.database.transaction() as connection:
            current = self.repository.materialize_current(connection, "person-1", ChunkingRule())
            stored_revisions = {
                row[0] for row in connection.execute("SELECT revision_id FROM knowledge_chunks")
            }

        self.assertEqual({chunk.revision_id for chunk in old}, {"revision-1"})
        self.assertEqual({chunk.revision_id for chunk in current}, {"revision-2"})
        self.assertEqual(stored_revisions, {"revision-1", "revision-2"})

    def test_all_active_revisions_are_stable_history_while_current_remains_latest(self) -> None:
        self._post("post-b")
        self._post("post-a")
        self._revision("post-a", "revision-a-old", "a old", "2026-07-09T00:00:00Z")
        self._revision("post-a", "revision-a-current", "a current", NOW)
        self._revision("post-b", "revision-b-current", "b current", NOW)

        with self.database.transaction() as connection:
            history = self.repository.materialize_active_revisions(
                connection, "person-1", ChunkingRule()
            )
            repeated = self.repository.materialize_active_revisions(
                connection, "person-1", ChunkingRule()
            )
            current = self.repository.materialize_current(
                connection, "person-1", ChunkingRule()
            )

        self.assertEqual(
            [chunk.revision_id for chunk in history],
            ["revision-a-old", "revision-a-current", "revision-b-current"],
        )
        self.assertEqual(repeated, history)
        self.assertEqual(
            {chunk.revision_id for chunk in current},
            {"revision-a-current", "revision-b-current"},
        )

    def test_suppressed_and_purged_posts_are_not_materialized(self) -> None:
        for post_id, state in (("active-post", "active"), ("hidden-post", "suppressed"), ("gone-post", "purged")):
            self._post(post_id, state)
            self._revision(post_id, f"revision-{post_id}-old", f"old {post_id}", "2026-07-10T00:00:00Z")
            self._revision(post_id, f"revision-{post_id}", f"body {post_id}", NOW)

        with self.database.transaction() as connection:
            chunks = self.repository.materialize_current(connection, "person-1", ChunkingRule())
            history = self.repository.materialize_active_revisions(
                connection, "person-1", ChunkingRule()
            )

        self.assertEqual({chunk.revision_id for chunk in chunks}, {"revision-active-post"})
        self.assertEqual(
            {chunk.revision_id for chunk in history},
            {"revision-active-post-old", "revision-active-post"},
        )

    def test_repository_uses_supplied_connection_and_outer_rollback_removes_chunks(self) -> None:
        self._post("post-1")
        self._revision("post-1", "revision-1", "rollback body", NOW)

        with self.assertRaisesRegex(RuntimeError, "outer abort"):
            with self.database.transaction(immediate=True) as connection:
                with patch.object(self.database, "connect", side_effect=AssertionError("must not open")):
                    chunks = self.repository.materialize_current(
                        connection, "person-1", ChunkingRule()
                    )
                self.assertTrue(chunks)
                raise RuntimeError("outer abort")

        with self.database.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
        self.assertEqual(count, 0)
