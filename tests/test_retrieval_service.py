from __future__ import annotations

import hashlib
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from voicevault.app_db import AppDatabase
from voicevault.embedding import FakeEmbeddingProvider
from voicevault.fulltext_index import FullTextHit, LocalFullTextIndexProvider
from voicevault.index_service import IndexService
from voicevault.retrieval import RetrievalRepository, RetrievalRequest
from voicevault.retrieval_service import IndexStale, RetrievalService
from voicevault.vector_index import LocalVectorIndexProvider, VectorHit


UTC = timezone.utc
PERSON_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PERSON_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PERSON_C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
GEN_A = "11111111-1111-4111-8111-111111111111"
GEN_B = "22222222-2222-4222-8222-222222222222"
GEN_C = "33333333-3333-4333-8333-333333333333"


def instant(day: int) -> datetime:
    return datetime(2026, 7, day, tzinfo=UTC)


class StaticFullText:
    def __init__(self, order=()) -> None:
        self.order = tuple(order)
        self.allowed_calls = []
        self.limits = []

    def search(self, generation_id, query, filters, limit):
        allowed = tuple(filters.allowed_chunk_ids or ())
        self.allowed_calls.append((generation_id, allowed))
        self.limits.append(limit)
        ordered = [chunk for chunk in self.order if chunk in allowed]
        return tuple(
            FullTextHit(chunk, rank, ("trigram",), (("trigram", rank),))
            for rank, chunk in enumerate(ordered[:limit], 1)
        )


class StaticVector:
    def __init__(self, order=(), similarities=()) -> None:
        self.order = tuple(order)
        self.similarities = tuple(similarities) or tuple(1.0 for _ in self.order)
        self.allowed_calls = []

    def search_person(self, generation_id, person_id, query_vector, limit, allowed_chunk_ids=None):
        allowed = tuple(allowed_chunk_ids or ())
        self.allowed_calls.append((generation_id, person_id, allowed))
        pairs = [(chunk, self.similarities[index]) for index, chunk in enumerate(self.order) if chunk in allowed]
        return tuple(VectorHit(chunk, rank, similarity) for rank, (chunk, similarity) in enumerate(pairs[:limit], 1))


class RetrievalServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_dir = Path(self.temp_dir.name)
        self.database = AppDatabase(data_dir=self.data_dir)
        self.database.initialize()
        with self.database.transaction() as connection:
            for person, name in ((PERSON_A, "Alice"), (PERSON_B, "Bob"), (PERSON_C, "Carol")):
                connection.execute("INSERT INTO persons VALUES (?, ?, ?, ?)", (person, name, instant(1).isoformat(), instant(1).isoformat()))
                connection.execute(
                    """
                    INSERT INTO platform_accounts(
                        account_id, person_id, platform, external_user_id,
                        archive_basis_confirmed_at, created_at, updated_at
                    ) VALUES (?, ?, 'xueqiu', ?, ?, ?, ?)
                    """,
                    (f"account-{person[0]}", person, f"user-{person[0]}", instant(1).isoformat(), instant(1).isoformat(), instant(1).isoformat()),
                )
            connection.execute(
                """
                INSERT INTO platform_accounts(
                    account_id, person_id, platform, external_user_id,
                    archive_basis_confirmed_at, created_at, updated_at
                ) VALUES ('account-aw', ?, 'wechat', 'user-aw', ?, ?, ?)
                """,
                (PERSON_A, instant(1).isoformat(), instant(1).isoformat(), instant(1).isoformat()),
            )
            connection.execute(
                """
                INSERT INTO collection_jobs(
                    job_id, account_id, mode, status, requested_start_at,
                    requested_end_at, created_at, updated_at
                ) VALUES ('job-a', 'account-a', 'normal', 'succeeded', ?, ?, ?, ?)
                """,
                (instant(1).isoformat(), instant(9).isoformat(), instant(1).isoformat(), instant(9).isoformat()),
            )
        self.fulltext = LocalFullTextIndexProvider(self.data_dir)
        self.vector = LocalVectorIndexProvider(self.data_dir)
        self.embedding = FakeEmbeddingProvider(dimension=4)

    def add_post(self, person, suffix, text, *, day=2, state="active", platform="xueqiu"):
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.database.transaction() as connection:
            account_id = "account-aw" if person == PERSON_A and platform == "wechat" else f"account-{person[0]}"
            connection.execute("INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?)", (f"post-{suffix}", account_id, f"external-{suffix}", instant(day).isoformat(), f"https://example.test/{suffix}", instant(day).isoformat()))
            connection.execute("INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, NULL)", (f"revision-{suffix}", f"post-{suffix}", digest, text, instant(day).isoformat()))
            reason = None if state == "active" else state
            connection.execute("INSERT INTO content_dispositions VALUES (?, ?, ?, ?, NULL)", (f"post-{suffix}", state, reason, instant(day).isoformat()))

    def add_revision(self, post_suffix, suffix, text, day):
        with self.database.transaction() as connection:
            connection.execute("INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, NULL)", (f"revision-{suffix}", f"post-{post_suffix}", hashlib.sha256(text.encode()).hexdigest(), text, instant(day).isoformat()))

    def observe(self, post_suffix, suffix, status, day):
        with self.database.transaction() as connection:
            connection.execute("INSERT INTO post_observations VALUES (?, ?, ?, ?, 'job-a')", (f"observation-{suffix}", f"post-{post_suffix}", status, instant(day).isoformat()))

    def build(self, person, generation, embedding=None):
        service = IndexService(self.database, self.fulltext, self.vector, embedding, clock=lambda: instant(8))
        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(generation)):
            return service.rebuild_person(person)

    def retrieval(self, embedding=None, fulltext=None, vector=None, candidate_pool=100):
        return RetrievalService(
            self.database,
            RetrievalRepository(),
            fulltext or self.fulltext,
            vector or self.vector,
            embedding,
            clock=lambda: instant(9),
            candidate_pool=candidate_pool,
        )

    def chunks(self, generation):
        with self.database.connect() as connection:
            return {
                row["content_text"]: (row["chunk_id"], row["post_id"])
                for row in connection.execute(
                    """
                    SELECT c.chunk_id, c.content_text, p.post_id
                    FROM index_generation_chunks gc
                    JOIN knowledge_chunks c ON c.chunk_id = gc.chunk_id
                    JOIN post_revisions r ON r.revision_id = c.revision_id
                    JOIN posts p ON p.post_id = r.post_id
                    WHERE gc.generation_id = ?
                    """,
                    (generation,),
                )
            }

    def test_real_integration_current_all_and_latest_observation(self) -> None:
        self.add_post(PERSON_A, "history", "common old", day=2)
        self.add_revision("history", "history-new", "common current", 3)
        self.observe("history", "old", "available", 3)
        self.observe("history", "new", "deleted", 4)
        self.add_post(PERSON_A, "hidden", "common hidden", state="suppressed")
        self.add_post(PERSON_A, "wechat", "common wechat", platform="wechat")
        self.add_post(PERSON_A, "late", "common late", day=5)
        self.build(PERSON_A, GEN_A, self.embedding)
        service = self.retrieval(self.embedding)

        filters = {
            "platforms": ("xueqiu",),
            "published_from": instant(1),
            "published_to": instant(5),
        }
        current_run = service.create_run(RetrievalRequest("common", (PERSON_A,), revision_scope="current", **filters))
        current = service.execute(current_run.run_id)
        all_run = service.create_run(RetrievalRequest("common", (PERSON_A,), revision_scope="all", **filters))
        all_result = service.execute(all_run.run_id)

        self.assertEqual([hit.revision_id for hit in current.hits], ["revision-history-new"])
        self.assertEqual({hit.revision_id for hit in all_result.hits}, {"revision-history", "revision-history-new"})
        self.assertTrue(all(hit.observation_status == "deleted" for hit in all_result.hits))
        self.assertTrue(all(hit.post_id != "post-hidden" for hit in all_result.hits))
        self.assertTrue(all(hit.post_id not in {"post-wechat", "post-late"} for hit in all_result.hits))

    def test_microsecond_boundaries_choose_exact_revision_and_observation(self) -> None:
        boundary = instant(2)
        earlier = boundary + timedelta(microseconds=400)
        later = boundary + timedelta(microseconds=500)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?)",
                ("post-boundary", "account-a", "external-boundary", earlier.isoformat(), None, earlier.isoformat()),
            )
            connection.execute(
                "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, NULL)",
                ("revision-boundary", "post-boundary", hashlib.sha256(b"common boundary").hexdigest(), "common boundary", earlier.isoformat()),
            )
            connection.execute(
                "INSERT INTO content_dispositions VALUES (?, 'active', NULL, ?, NULL)",
                ("post-boundary", earlier.isoformat()),
            )
            connection.execute(
                "INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?)",
                ("post-precision", "account-a", "external-precision", instant(3).isoformat(), None, instant(3).isoformat()),
            )
            for revision_id, text, captured_at in (
                ("revision-precision-z-old", "common old", earlier),
                ("revision-precision-a-new", "common current", later),
            ):
                connection.execute(
                    "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, NULL)",
                    (revision_id, "post-precision", hashlib.sha256(text.encode()).hexdigest(), text, captured_at.isoformat()),
                )
            connection.execute(
                "INSERT INTO content_dispositions VALUES (?, 'active', NULL, ?, NULL)",
                ("post-precision", instant(1).isoformat()),
            )
            for observation_id, status, observed_at in (
                ("observation-precision-z-old", "available", earlier),
                ("observation-precision-a-new", "deleted", later),
            ):
                connection.execute(
                    "INSERT INTO post_observations VALUES (?, ?, ?, ?, 'job-a')",
                    (observation_id, "post-precision", status, observed_at.isoformat()),
                )
        self.build(PERSON_A, GEN_A, None)
        service = self.retrieval(None)

        result = service.execute(
            service.create_run(
                RetrievalRequest(
                    "common",
                    (PERSON_A,),
                    published_from=later,
                    revision_scope="current",
                )
            ).run_id
        )

        self.assertEqual([hit.post_id for hit in result.hits], ["post-precision"])
        self.assertEqual(result.hits[0].revision_id, "revision-precision-a-new")
        self.assertEqual(result.hits[0].observation_status, "deleted")

    def test_post_cap_expands_ranked_channels_until_another_post_is_seen(self) -> None:
        self.add_post(PERSON_A, "long", "x" * 108_000)
        self.add_post(PERSON_A, "other", "common other")
        self.build(PERSON_A, GEN_A, None)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.chunk_id, p.post_id
                FROM index_generation_chunks gc
                JOIN knowledge_chunks c ON c.chunk_id = gc.chunk_id
                JOIN post_revisions r ON r.revision_id = c.revision_id
                JOIN posts p ON p.post_id = r.post_id
                WHERE gc.generation_id = ?
                ORDER BY p.post_id, c.ordinal
                """,
                (GEN_A,),
            ).fetchall()
        long_chunks = tuple(row["chunk_id"] for row in rows if row["post_id"] == "post-long")
        other_chunk = next(row["chunk_id"] for row in rows if row["post_id"] == "post-other")
        self.assertGreaterEqual(len(long_chunks), 101)
        fulltext = StaticFullText(long_chunks[:101] + (other_chunk,) + long_chunks[101:])
        service = self.retrieval(None, fulltext=fulltext, candidate_pool=100)

        result = service.execute(
            service.create_run(
                RetrievalRequest(
                    "common",
                    (PERSON_A,),
                    limit=2,
                    max_chunks_per_post=1,
                )
            ).run_id
        )

        self.assertEqual({hit.post_id for hit in result.hits}, {"post-long", "post-other"})
        self.assertEqual(fulltext.limits[0], 100)
        self.assertGreater(fulltext.limits[-1], 100)

    def test_frozen_head_is_used_after_current_head_switches(self) -> None:
        self.add_post(PERSON_A, "alpha", "alpha evidence")
        self.build(PERSON_A, GEN_A, self.embedding)
        service = self.retrieval(self.embedding)
        pending = service.create_run(RetrievalRequest("alpha", (PERSON_A,)))
        self.add_post(PERSON_A, "new", "alpha new", day=3)
        self.build(PERSON_A, GEN_C, self.embedding)

        result = service.execute(pending.run_id)

        self.assertTrue(result.hits)
        self.assertEqual({hit.generation_id for hit in result.hits}, {GEN_A})

    def test_same_allowed_set_reaches_both_channels_and_group_embedding_runs_once(self) -> None:
        self.add_post(PERSON_A, "alpha", "common alpha")
        self.add_post(PERSON_B, "beta", "common beta")
        self.build(PERSON_A, GEN_A, self.embedding)
        self.build(PERSON_B, GEN_B, self.embedding)
        chunks = self.chunks(GEN_A) | self.chunks(GEN_B)
        order = tuple(value[0] for value in chunks.values())
        fulltext = StaticFullText(order)
        vector = StaticVector(order)
        before = len(self.embedding.batches)
        service = self.retrieval(self.embedding, fulltext=fulltext, vector=vector)

        result = service.execute(service.create_run(RetrievalRequest("common", (PERSON_A, PERSON_B))).run_id)

        self.assertEqual(len(self.embedding.batches) - before, 1)
        self.assertEqual({call[0]: set(call[1]) for call in fulltext.allowed_calls}, {call[0]: set(call[2]) for call in vector.allowed_calls})
        self.assertEqual(result.retrieval_mode, "hybrid")

    def test_rank_only_rrf_quota_post_cap_tie_and_similarity_independence(self) -> None:
        self.add_post(PERSON_A, "a1", "common a1")
        self.add_post(PERSON_A, "a2", "common a2")
        self.add_post(PERSON_B, "b1", "common b1")
        self.build(PERSON_A, GEN_A, self.embedding)
        self.build(PERSON_B, GEN_B, self.embedding)
        values = self.chunks(GEN_A) | self.chunks(GEN_B)
        a1, a2, b1 = (values[key][0] for key in ("common a1", "common a2", "common b1"))
        order = (a1, a2, b1)
        request = RetrievalRequest("common", (PERSON_A, PERSON_B), limit=3, min_hits_per_person=1, max_chunks_per_post=1)

        first_service = self.retrieval(self.embedding, fulltext=StaticFullText(order), vector=StaticVector(order, (1000.0, -5.0, 0.0)))
        first = first_service.execute(first_service.create_run(request).run_id)
        second_service = self.retrieval(self.embedding, fulltext=StaticFullText(order), vector=StaticVector(order, (-1000.0, 50.0, 9.0)))
        second = second_service.execute(second_service.create_run(request).run_id)

        self.assertEqual([hit.chunk_id for hit in first.hits], [hit.chunk_id for hit in second.hits])
        self.assertEqual([hit.person_id for hit in first.hits[:2]], [PERSON_A, PERSON_B])
        self.assertEqual([hit.fused_rank for hit in first.hits], list(range(1, len(first.hits) + 1)))

    def test_per_person_degrade_produces_mixed_and_missing_contract_persists(self) -> None:
        self.add_post(PERSON_A, "alpha", "common alpha")
        self.add_post(PERSON_B, "beta", "common beta")
        self.build(PERSON_A, GEN_A, self.embedding)
        self.build(PERSON_B, GEN_B, None)
        service = self.retrieval(self.embedding)

        result = service.execute(service.create_run(RetrievalRequest("common", (PERSON_A, PERSON_B, PERSON_C))).run_id)
        with self.database.connect() as connection:
            restarted = RetrievalRepository().get_run(connection, result.run_id)

        self.assertEqual(result.retrieval_mode, "mixed")
        self.assertEqual(result.missing_person_ids, (PERSON_C,))
        self.assertEqual(restarted, result)
        self.assertEqual([person.retrieval_mode for person in result.persons], ["hybrid", "fulltext_only", "none"])

    def test_all_missing_is_rejected_before_a_run_is_created(self) -> None:
        service = self.retrieval(self.embedding)

        with self.assertRaises(IndexStale):
            service.create_run(RetrievalRequest("anything", (PERSON_C,)))
        with self.database.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM retrieval_runs").fetchone()[0]
        self.assertEqual(count, 0)

    def test_restart_reconciles_only_preexisting_incomplete_runs_once(self) -> None:
        self.add_post(PERSON_A, "alpha", "alpha evidence")
        self.build(PERSON_A, GEN_A, None)
        service = self.retrieval(None)
        pending = service.create_run(RetrievalRequest("alpha", (PERSON_A,)))
        running = service.create_run(RetrievalRequest("alpha", (PERSON_A,)))
        with self.database.transaction(immediate=True) as connection:
            RetrievalRepository().mark_running(connection, running.run_id, started_at=instant(8))

        self.assertEqual(service.reconcile_incomplete(), 2)
        current = service.create_run(RetrievalRequest("alpha", (PERSON_A,)))

        with self.database.connect() as connection:
            interrupted = [
                RetrievalRepository().get_run(connection, run_id)
                for run_id in (pending.run_id, running.run_id)
            ]
            still_pending = RetrievalRepository().get_run(connection, current.run_id)
        self.assertEqual([item.status for item in interrupted], ["interrupted", "interrupted"])
        self.assertTrue(all(item.error["code"] == "service_restarted" for item in interrupted))
        self.assertEqual(still_pending.status, "pending")

    def test_restart_reconciliation_preserves_terminal_run(self) -> None:
        self.add_post(PERSON_A, "alpha", "alpha evidence")
        self.build(PERSON_A, GEN_A, None)
        service = self.retrieval(None)
        terminal = service.execute(
            service.create_run(RetrievalRequest("alpha", (PERSON_A,))).run_id
        )

        self.assertEqual(service.reconcile_incomplete(), 0)
        self.assertEqual(service.get_run(terminal.run_id), terminal)


if __name__ == "__main__":
    unittest.main()
