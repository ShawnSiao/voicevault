from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from voicevault.app_db import AppDatabase
from voicevault.questions import QuestionRepository, QuestionRunStateError


UTC = timezone.utc
NOW = datetime(2026, 7, 11, tzinfo=UTC)


class QuestionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = AppDatabase(data_dir=Path(self.temp_dir.name))
        self.database.initialize()
        self.repository = QuestionRepository()
        self._insert_retrieval_fixture("retrieval-a", status="succeeded")

    def _insert_retrieval_fixture(self, run_id: str, *, status: str) -> None:
        text = "alpha evidence with stable excerpt"
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.database.transaction() as connection:
            if connection.execute("SELECT 1 FROM persons WHERE person_id = 'person-a'").fetchone() is None:
                connection.execute(
                    "INSERT INTO persons VALUES ('person-a', 'Alice', ?, ?)",
                    (NOW.isoformat(), NOW.isoformat()),
                )
                connection.execute(
                    "INSERT INTO platform_accounts VALUES ('account-a', 'person-a', 'xueqiu', 'user-a', 'Alice', ?, ?, ?)",
                    (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
                )
                connection.execute(
                    "INSERT INTO posts VALUES ('post-a', 'account-a', 'remote-a', ?, 'https://example.test/a', ?)",
                    (NOW.isoformat(), NOW.isoformat()),
                )
                connection.execute(
                    "INSERT INTO post_revisions VALUES ('revision-a', 'post-a', ?, ?, ?, NULL)",
                    (digest, text, NOW.isoformat()),
                )
                connection.execute(
                    "INSERT INTO content_dispositions VALUES ('post-a', 'active', NULL, ?, NULL)",
                    (NOW.isoformat(),),
                )
                connection.execute(
                    "INSERT INTO knowledge_chunks VALUES ('chunk-a', 'revision-a', 'paragraph-window-v1', 0, 0, ?, ?, ?)",
                    (len(text), text, digest),
                )
                connection.execute(
                    "INSERT INTO index_generations(generation_id, person_id, chunk_rule_version, status, retrieval_mode, created_at, completed_at) VALUES ('generation-a', 'person-a', 'paragraph-window-v1', 'ready', 'hybrid', ?, ?)",
                    (NOW.isoformat(), NOW.isoformat()),
                )
                connection.execute("INSERT INTO index_generation_chunks VALUES ('generation-a', 'chunk-a')")
            request = json.dumps(
                {
                    "limit": 20,
                    "max_chunks_per_post": 2,
                    "min_hits_per_person": 1,
                    "person_ids": ["person-a"],
                    "platforms": [],
                    "published_from": None,
                    "published_to": None,
                    "query": "What is Alice's view?",
                    "revision_scope": "current",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            completed_at = NOW.isoformat() if status == "succeeded" else None
            connection.execute(
                "INSERT INTO retrieval_runs VALUES (?, ?, ?, ?, '{}', NULL, ?, ?, ?)",
                (run_id, request, status, "hybrid" if status == "succeeded" else "none", NOW.isoformat(), None, completed_at),
            )
            connection.execute(
                "INSERT INTO retrieval_run_persons VALUES (?, 'person-a', 0, 'generation-a', 'ready', 'hybrid')",
                (run_id,),
            )
            if status == "succeeded":
                connection.execute(
                    """
                    INSERT INTO retrieval_evidence VALUES (
                        ?, ?, 0, 'person-a', 'account-a', 'xueqiu', 'post-a',
                        'revision-a', 'chunk-a', 'generation-a', 'https://example.test/a',
                        ?, ?, NULL, NULL, 0, ?, 1, 1, 1, ?
                    )
                    """,
                    (f"source-{run_id}", run_id, NOW.isoformat(), NOW.isoformat(), len(text), NOW.isoformat()),
                )

    def test_create_freezes_stable_bundle_and_uses_caller_transaction(self) -> None:
        with self.database.transaction() as connection:
            first = self.repository.create(
                connection,
                "question-a",
                "retrieval-a",
                provider="codex_task",
                created_at=NOW,
            )
            second = self.repository.create(
                connection,
                "question-b",
                "retrieval-a",
                provider="codex_task",
                created_at=NOW,
            )

        self.assertEqual(first.status, "pending_codex")
        self.assertEqual(first.bundle.canonical_json, second.bundle.canonical_json)
        self.assertEqual(first.bundle.sha256, second.bundle.sha256)
        self.assertEqual(hashlib.sha256(first.bundle.canonical_json.encode()).hexdigest(), first.bundle.sha256)
        self.assertEqual([item.evidence_id for item in first.bundle.evidence], ["E1"])
        self.assertEqual(first.bundle.evidence[0].excerpt, "alpha evidence with stable excerpt")
        self.assertEqual(first.persons[0].display_name, "Alice")

        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.database.transaction() as connection:
                self.repository.create(connection, "question-rollback", "retrieval-a", provider="codex_task", created_at=NOW)
                raise RuntimeError("rollback")
        with self.database.connect() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM question_runs WHERE run_id = 'question-rollback'").fetchone())

    def test_only_succeeded_retrieval_runs_can_be_frozen(self) -> None:
        self._insert_retrieval_fixture("retrieval-pending", status="pending")
        with self.database.transaction() as connection, self.assertRaises(QuestionRunStateError):
            self.repository.create(connection, "question-pending", "retrieval-pending", provider="codex_task", created_at=NOW)

    def test_question_evidence_trigger_rejects_cross_run_and_lineage_updates(self) -> None:
        with self.database.transaction() as connection:
            self.repository.create(connection, "question-a", "retrieval-a", provider="codex_task", created_at=NOW)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE question_runs SET evidence_sha256 = ? WHERE run_id = 'question-a'",
                    ("b" * 64,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE question_evidence SET excerpt = excerpt WHERE run_id = 'question-a' AND evidence_id = 'E1'"
                )
            for column, value in (
                ("retrieval_run_id", "other-run"),
                ("person_id", "other-person"),
                ("revision_id", "other-revision"),
                ("chunk_id", "other-chunk"),
            ):
                with self.subTest(column=column), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        f"UPDATE question_evidence SET {column} = ? WHERE run_id = 'question-a' AND evidence_id = 'E1'",
                        (value,),
                    )

    def test_terminal_resource_is_restart_stable(self) -> None:
        with self.database.transaction() as connection:
            created = self.repository.create(connection, "question-a", "retrieval-a", provider="codex_task", created_at=NOW)
            terminal = self.repository.invalidate(
                connection,
                "question-a",
                {"combined_answer": "untrusted"},
                {"code": "citation_invalid"},
                completed_at=NOW,
            )
        reopened = AppDatabase(db_path=self.database.path)
        reopened.initialize()
        with reopened.connect() as connection:
            loaded = QuestionRepository().get(connection, "question-a")
        self.assertNotEqual(created, terminal)
        self.assertEqual(terminal, loaded)


if __name__ == "__main__":
    unittest.main()
