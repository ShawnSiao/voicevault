from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from voicevault.app_db import AppDatabase
from voicevault.retrieval import (
    EvidenceHit,
    RetrievalRepository,
    RetrievalRequest,
)


UTC = timezone.utc
PERSON_A = "person-a"
PERSON_B = "person-b"
GENERATION_A = "generation-a"
GENERATION_A2 = "generation-a2"
GENERATION_B = "generation-b"


def instant(day: int) -> datetime:
    return datetime(2026, 7, day, tzinfo=UTC)


class RetrievalContractsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_dir = Path(self.temp_dir.name)
        self.database = AppDatabase(data_dir=self.data_dir)
        self.database.initialize()
        self.repository = RetrievalRepository()
        with self.database.transaction() as connection:
            for person_id, name in ((PERSON_A, "Alice"), (PERSON_B, "Bob")):
                connection.execute(
                    "INSERT INTO persons VALUES (?, ?, ?, ?)",
                    (person_id, name, instant(1).isoformat(), instant(1).isoformat()),
                )
                connection.execute(
                    "INSERT INTO platform_accounts VALUES (?, ?, 'xueqiu', ?, ?, ?, ?, ?)",
                    (
                        f"account-{person_id[-1]}",
                        person_id,
                        f"external-{person_id[-1]}",
                        name,
                        instant(1).isoformat(),
                        instant(1).isoformat(),
                        instant(1).isoformat(),
                    ),
                )
            connection.execute(
                """
                INSERT INTO collection_jobs(
                    job_id, account_id, mode, status, requested_start_at,
                    requested_end_at, created_at, updated_at
                ) VALUES ('job-a', 'account-a', 'normal', 'succeeded', ?, ?, ?, ?)
                """,
                (instant(1).isoformat(), instant(5).isoformat(), instant(1).isoformat(), instant(5).isoformat()),
            )
            for suffix, generation_id, person_id, text in (
                ("a", GENERATION_A, PERSON_A, "alpha evidence"),
                ("b", GENERATION_B, PERSON_B, "beta evidence"),
            ):
                content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                connection.execute(
                    "INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        f"post-{suffix}", f"account-{suffix}", f"external-post-{suffix}",
                        instant(2).isoformat(), f"https://example.test/{suffix}", instant(2).isoformat(),
                    ),
                )
                connection.execute(
                    "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, NULL)",
                    (f"revision-{suffix}", f"post-{suffix}", content_hash, text, instant(3).isoformat()),
                )
                connection.execute(
                    "INSERT INTO knowledge_chunks VALUES (?, ?, 'paragraph-window-v1', 0, 0, ?, ?, ?)",
                    (f"chunk-{suffix}", f"revision-{suffix}", len(text), text, content_hash),
                )
                connection.execute(
                    """
                    INSERT INTO index_generations(
                        generation_id, person_id, chunk_rule_version, status,
                        retrieval_mode, created_at, completed_at
                    ) VALUES (?, ?, 'paragraph-window-v1', 'ready', 'hybrid', ?, ?)
                    """,
                    (generation_id, person_id, instant(4).isoformat(), instant(4).isoformat()),
                )
                connection.execute(
                    "INSERT INTO index_generation_chunks VALUES (?, ?)",
                    (generation_id, f"chunk-{suffix}"),
                )
                connection.execute(
                    "INSERT INTO post_observations VALUES (?, ?, 'available', ?, 'job-a')",
                    (f"observation-{suffix}", f"post-{suffix}", instant(4).isoformat()),
                )
            connection.execute(
                """
                INSERT INTO index_generations(
                    generation_id, person_id, chunk_rule_version, status,
                    retrieval_mode, created_at, completed_at
                ) VALUES (?, ?, 'paragraph-window-v1', 'ready', 'hybrid', ?, ?)
                """,
                (GENERATION_A2, PERSON_A, instant(5).isoformat(), instant(5).isoformat()),
            )
            connection.execute(
                "INSERT INTO index_generation_chunks VALUES (?, 'chunk-a')",
                (GENERATION_A2,),
            )
            connection.execute(
                "INSERT INTO person_index_heads VALUES (?, ?, ?)",
                (PERSON_A, GENERATION_A, instant(4).isoformat()),
            )

    def request(self, person_ids: tuple[str, ...] = (PERSON_A,)) -> RetrievalRequest:
        return RetrievalRequest(query="alpha", person_ids=person_ids)

    def hit(self, **changes) -> EvidenceHit:
        values = {
            "evidence_id": "evidence-a",
            "ordinal": 0,
            "person_id": PERSON_A,
            "account_id": "account-a",
            "platform": "xueqiu",
            "post_id": "post-a",
            "revision_id": "revision-a",
            "chunk_id": "chunk-a",
            "generation_id": GENERATION_A,
            "canonical_url": "https://example.test/a",
            "published_at": instant(2),
            "captured_at": instant(3),
            "observation_status": "available",
            "observed_at": instant(4),
            "char_start": 0,
            "char_end": len("alpha evidence"),
            "fulltext_rank": 1,
            "vector_rank": 2,
            "fused_rank": 1,
        }
        values.update(changes)
        return EvidenceHit(**values)

    def test_request_normalizes_dedupes_and_rejects_invalid_bounds(self) -> None:
        request = RetrievalRequest(
            query="  alpha question  ",
            person_ids=(PERSON_A, PERSON_B, PERSON_A),
            platforms=("xueqiu", "wechat", "xueqiu"),
            published_from=instant(1),
            published_to=instant(3),
            limit=4,
            min_hits_per_person=2,
        )
        self.assertEqual(request.query, "alpha question")
        self.assertEqual(request.person_ids, (PERSON_A, PERSON_B))
        self.assertEqual(request.platforms, ("xueqiu", "wechat"))
        with self.assertRaises(FrozenInstanceError):
            request.limit = 5  # type: ignore[misc]

        invalid = (
            {"query": " ", "person_ids": (PERSON_A,)},
            {"query": "q", "person_ids": ()},
            {"query": "q", "person_ids": tuple(f"p-{index}" for index in range(11))},
            {"query": "q", "person_ids": [PERSON_A]},
            {"query": "q", "person_ids": (PERSON_A,), "platforms": ["xueqiu"]},
            {"query": "q", "person_ids": (PERSON_A,), "revision_scope": "latest"},
            {"query": "q", "person_ids": (PERSON_A,), "limit": 51},
            {"query": "q", "person_ids": (PERSON_A, PERSON_B), "limit": 1},
            {"query": "q", "person_ids": (PERSON_A,), "min_hits_per_person": -1},
            {"query": "q", "person_ids": (PERSON_A,), "max_chunks_per_post": 0},
            {"query": "q", "person_ids": (PERSON_A,), "published_from": instant(3), "published_to": instant(3)},
            {"query": "q", "person_ids": (PERSON_A,), "published_from": datetime(2026, 7, 1)},
            {
                "query": "q",
                "person_ids": (PERSON_A,),
                "published_from": datetime(2026, 7, 1, tzinfo=timezone(timedelta(hours=8))),
            },
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises((TypeError, ValueError)):
                RetrievalRequest(**values)  # type: ignore[arg-type]

    def test_create_run_freezes_ordered_heads_and_canonical_request(self) -> None:
        request = self.request((PERSON_A, PERSON_B))
        with self.database.transaction() as connection:
            created = self.repository.create_run(
                connection, "run-freeze", request, created_at=instant(6)
            )
            connection.execute(
                "UPDATE person_index_heads SET generation_id = ? WHERE person_id = ?",
                (GENERATION_A2, PERSON_A),
            )
            frozen = self.repository.get_run(connection, "run-freeze")
            raw_request = connection.execute(
                "SELECT request_json FROM retrieval_runs WHERE run_id = 'run-freeze'"
            ).fetchone()[0]

        self.assertEqual(created, frozen)
        self.assertEqual(
            [(item.person_id, item.ordinal, item.generation_id, item.generation_status, item.retrieval_mode) for item in frozen.persons],
            [
                (PERSON_A, 0, GENERATION_A, "ready", "hybrid"),
                (PERSON_B, 1, None, "missing", "none"),
            ],
        )
        self.assertEqual(raw_request, json.dumps(json.loads(raw_request), sort_keys=True, separators=(",", ":"), ensure_ascii=False))

    def test_create_run_persists_unusable_head_as_none_without_losing_generation(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE index_generations SET status = 'stale' WHERE generation_id = ?",
                (GENERATION_A,),
            )
            created = self.repository.create_run(
                connection,
                "run-stale",
                self.request(),
                created_at=instant(6),
            )
            stored_mode = connection.execute(
                """
                SELECT retrieval_mode FROM retrieval_run_persons
                WHERE run_id = 'run-stale' AND person_id = ?
                """,
                (PERSON_A,),
            ).fetchone()[0]

        self.assertEqual(
            (
                created.persons[0].generation_id,
                created.persons[0].generation_status,
                created.persons[0].retrieval_mode,
                stored_mode,
            ),
            (GENERATION_A, "stale", "none", "none"),
        )

    def test_complete_persists_stable_deeply_immutable_evidence_across_restart(self) -> None:
        with self.database.transaction() as connection:
            self.repository.create_run(connection, "run-complete", self.request(), created_at=instant(5))
            self.repository.mark_running(connection, "run-complete", started_at=instant(6))
            completed = self.repository.complete(
                connection,
                "run-complete",
                retrieval_mode="hybrid",
                degradation={"missing_person_ids": [], "trace": {"mode": "hybrid"}},
                hits=(self.hit(),),
                completed_at=instant(7),
            )

        reopened = AppDatabase(db_path=self.database.path)
        reopened.initialize()
        with reopened.connect() as connection:
            loaded = RetrievalRepository().get_run(connection, "run-complete")

        self.assertEqual(completed, loaded)
        self.assertEqual((loaded.status, loaded.retrieval_mode), ("succeeded", "hybrid"))
        self.assertEqual(loaded.hits[0].chunk_id, "chunk-a")
        self.assertFalse(hasattr(loaded.hits[0], "similarity"))
        with self.assertRaises(TypeError):
            loaded.degradation["new"] = "value"  # type: ignore[index]
        with self.assertRaises(TypeError):
            loaded.degradation["trace"]["mode"] = "changed"  # type: ignore[index]

    def test_failed_and_interrupted_runs_are_terminal_and_restart_stable(self) -> None:
        with self.database.transaction() as connection:
            for run_id in ("run-failed", "run-interrupted"):
                self.repository.create_run(connection, run_id, self.request(), created_at=instant(5))
                self.repository.mark_running(connection, run_id, started_at=instant(6))
            failed = self.repository.fail(
                connection, "run-failed", error={"code": "provider_unavailable"}, completed_at=instant(7)
            )
            interrupted = self.repository.interrupt(
                connection, "run-interrupted", error={"code": "shutdown"}, completed_at=instant(7)
            )

        self.assertEqual((failed.status, failed.retrieval_mode), ("failed", "none"))
        self.assertEqual((interrupted.status, interrupted.retrieval_mode), ("interrupted", "none"))
        with self.database.connect() as connection:
            self.assertEqual(self.repository.get_run(connection, "run-failed"), failed)
            self.assertEqual(self.repository.get_run(connection, "run-interrupted"), interrupted)

    def test_complete_persists_only_running_time_person_mode_downgrades(self) -> None:
        with self.database.transaction() as connection:
            self.repository.create_run(
                connection,
                "run-modes",
                self.request((PERSON_A, PERSON_B)),
                created_at=instant(5),
            )
            self.repository.mark_running(connection, "run-modes", started_at=instant(6))
            connection.execute(
                """
                UPDATE retrieval_run_persons SET retrieval_mode = 'fulltext_only'
                WHERE run_id = 'run-modes' AND person_id = ?
                """,
                (PERSON_A,),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    UPDATE retrieval_run_persons SET retrieval_mode = 'hybrid'
                    WHERE run_id = 'run-modes' AND person_id = ?
                    """,
                    (PERSON_A,),
                )
            completed = self.repository.complete(
                connection,
                "run-modes",
                retrieval_mode="fulltext_only",
                degradation={"missing_person_ids": [PERSON_B]},
                hits=(replace(self.hit(), vector_rank=None),),
                completed_at=instant(7),
                person_modes={PERSON_A: "fulltext_only", PERSON_B: "none"},
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    UPDATE retrieval_run_persons SET retrieval_mode = 'none'
                    WHERE run_id = 'run-modes' AND person_id = ?
                    """,
                    (PERSON_A,),
                )

        self.assertEqual(
            [person.retrieval_mode for person in completed.persons],
            ["fulltext_only", "none"],
        )
        self.assertEqual(completed.missing_person_ids, (PERSON_B,))

    def test_repository_uses_caller_transaction_and_outer_rollback(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.database.transaction() as connection:
                self.repository.create_run(connection, "run-rollback", self.request(), created_at=instant(5))
                raise RuntimeError("rollback")
        with self.database.connect() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM retrieval_runs WHERE run_id = 'run-rollback'"
                ).fetchone()
            )

    def test_schema_constraints_require_valid_modes_ordinals_and_channel_rank(self) -> None:
        with self.database.transaction() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO retrieval_runs VALUES ('bad', '{}', 'unknown', 'none', NULL, NULL, ?, NULL, NULL)",
                    (instant(1).isoformat(),),
                )
            self.repository.create_run(connection, "run-ranks", self.request(), created_at=instant(5))
            self.repository.mark_running(connection, "run-ranks", started_at=instant(6))
            with self.assertRaises(ValueError):
                self.repository.complete(
                    connection,
                    "run-ranks",
                    retrieval_mode="hybrid",
                    degradation={},
                    hits=(replace(self.hit(), fulltext_rank=None, vector_rank=None),),
                    completed_at=instant(7),
                )

    def test_lineage_trigger_rejects_insert_and_update_crossing_any_frozen_boundary(self) -> None:
        with self.database.transaction() as connection:
            self.repository.create_run(connection, "run-lineage", self.request(), created_at=instant(5))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    UPDATE retrieval_run_persons
                    SET generation_id = ?, generation_status = 'ready', retrieval_mode = 'hybrid'
                    WHERE run_id = 'run-lineage' AND person_id = ?
                    """,
                    (GENERATION_A2, PERSON_A),
                )
            self.repository.mark_running(connection, "run-lineage", started_at=instant(6))
            self.repository.complete(
                connection,
                "run-lineage",
                retrieval_mode="hybrid",
                degradation={},
                hits=(self.hit(),),
                completed_at=instant(7),
            )
            base = {
                "person_id": PERSON_A,
                "account_id": "account-a",
                "platform": "xueqiu",
                "post_id": "post-a",
                "revision_id": "revision-a",
                "chunk_id": "chunk-a",
                "generation_id": GENERATION_A,
            }
            invalid = {
                "person_id": PERSON_B,
                "account_id": "account-b",
                "platform": "wechat",
                "post_id": "post-b",
                "revision_id": "revision-b",
                "chunk_id": "chunk-b",
                "generation_id": GENERATION_B,
            }
            for column, value in invalid.items():
                values = dict(base)
                values[column] = value
                with self.subTest(operation="insert", column=column), self.assertRaises(sqlite3.IntegrityError):
                    self._insert_raw_evidence(connection, values)
                with self.subTest(operation="update", column=column), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        f"UPDATE retrieval_evidence SET {column} = ? WHERE evidence_id = 'evidence-a'",
                        (value,),
                    )
            stored = connection.execute(
                "SELECT person_id, account_id, platform, post_id, revision_id, chunk_id, generation_id FROM retrieval_evidence WHERE evidence_id = 'evidence-a'"
            ).fetchone()
        self.assertEqual(dict(stored), base)

    @staticmethod
    def _insert_raw_evidence(connection: sqlite3.Connection, lineage: dict[str, str]) -> None:
        connection.execute(
            """
            INSERT INTO retrieval_evidence(
                evidence_id, run_id, ordinal, person_id, account_id, platform,
                post_id, revision_id, chunk_id, generation_id, canonical_url,
                published_at, captured_at, observation_status, observed_at,
                char_start, char_end, fulltext_rank, vector_rank, fused_rank, created_at
            ) VALUES (?, 'run-lineage', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 1, NULL, 2, ?)
            """,
            (
                "invalid-evidence",
                lineage["person_id"], lineage["account_id"], lineage["platform"],
                lineage["post_id"], lineage["revision_id"], lineage["chunk_id"], lineage["generation_id"],
                "https://example.test/a", instant(2).isoformat(), instant(3).isoformat(),
                "available", instant(4).isoformat(), len("alpha evidence"), instant(7).isoformat(),
            ),
        )


if __name__ == "__main__":
    unittest.main()
