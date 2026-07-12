from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from voicevault.app_db import AppDatabase
from voicevault.embedding import (
    EmbeddingBatch,
    FakeEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
)
from voicevault.fulltext_index import LocalFullTextIndexProvider
from voicevault.index_service import IndexService
from voicevault.vector_index import LocalVectorIndexProvider, VectorIndexInvalid


UTC = timezone.utc
PERSON_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PERSON_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
GENERATION_A = "11111111-1111-4111-8111-111111111111"
GENERATION_B = "22222222-2222-4222-8222-222222222222"
GENERATION_C = "33333333-3333-4333-8333-333333333333"
GENERATION_D = "44444444-4444-4444-8444-444444444444"
GENERATION_E = "55555555-5555-4555-8555-555555555555"


def instant(day: int) -> datetime:
    return datetime(2026, 7, day, tzinfo=UTC)


class FailingFullText:
    def build(self, generation_id, documents):
        raise RuntimeError("C:\\secret\\fulltext")


class FailingVector(LocalVectorIndexProvider):
    def build_person_shard(self, generation_id, person_id, chunk_ids, embeddings):
        raise VectorIndexInvalid("Vector shard is invalid.")


class CallbackFullText:
    def __init__(self, delegate: LocalFullTextIndexProvider) -> None:
        self.delegate = delegate
        self.callback = None
        self._called = False

    def build(self, generation_id, documents):
        if self.callback is not None and not self._called:
            self._called = True
            self.callback()
        return self.delegate.build(generation_id, documents)


class InspectingVector(LocalVectorIndexProvider):
    def __init__(self, data_dir, database, expected_head):
        super().__init__(data_dir)
        self.database = database
        self.expected_head = expected_head
        self.observed_head = None

    def build_person_shard(self, generation_id, person_id, chunk_ids, embeddings):
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT generation_id FROM person_index_heads WHERE person_id = ?",
                (person_id,),
            ).fetchone()
            self.observed_head = None if row is None else row[0]
        return super().build_person_shard(
            generation_id, person_id, chunk_ids, embeddings
        )


class JSONResponse:
    def __init__(self, payload) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.payload


class DeterministicOpenAIOpener:
    def __init__(self, dimension: int = 4) -> None:
        self.dimension = dimension
        self.calls: list[tuple[str, ...]] = []

    def open(self, request, timeout):
        payload = json.loads(request.data)
        texts = tuple(payload["input"])
        self.calls.append(texts)
        vectors = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vectors.append([float(digest[index] + 1) for index in range(self.dimension)])
        return JSONResponse(
            {
                "model": payload["model"],
                "data": [
                    {"index": index, "embedding": vector}
                    for index, vector in enumerate(vectors)
                ],
            }
        )


class LegacyEmbeddingProvider:
    def embed(self, texts):
        return EmbeddingBatch(
            model="legacy",
            dimension=2,
            provider_fingerprint="a" * 64,
            vectors=tuple((1.0, 0.0) for _ in texts),
        )


class IndexServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_dir = Path(self.temp_dir.name)
        self.database = AppDatabase(data_dir=self.data_dir)
        self.database.initialize()
        with self.database.transaction() as connection:
            for person_id, name in ((PERSON_A, "Alice"), (PERSON_B, "Bob")):
                connection.execute(
                    "INSERT INTO persons VALUES (?, ?, ?, ?)",
                    (person_id, name, instant(1).isoformat(), instant(1).isoformat()),
                )
                connection.execute(
                    """
                    INSERT INTO platform_accounts(
                        account_id, person_id, platform, external_user_id,
                        archive_basis_confirmed_at, created_at, updated_at
                    ) VALUES (?, ?, 'xueqiu', ?, ?, ?, ?)
                    """,
                    (
                        f"account-{person_id[0]}", person_id, f"user-{person_id[0]}",
                        instant(1).isoformat(), instant(1).isoformat(), instant(1).isoformat(),
                    ),
                )
        self.fulltext = LocalFullTextIndexProvider(self.data_dir)
        self.vector = LocalVectorIndexProvider(self.data_dir)

    def add_post(self, suffix: str, text: str, *, person_id: str = PERSON_A, day: int = 2) -> None:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO posts VALUES (?, ?, ?, ?, NULL, ?)",
                (
                    f"post-{suffix}", f"account-{person_id[0]}", f"external-{suffix}",
                    instant(day).isoformat(), instant(day).isoformat(),
                ),
            )
            connection.execute(
                "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, NULL)",
                (f"revision-{suffix}", f"post-{suffix}", digest, text, instant(day).isoformat()),
            )
            connection.execute(
                "INSERT INTO content_dispositions VALUES (?, 'active', NULL, ?, NULL)",
                (f"post-{suffix}", instant(day).isoformat()),
            )

    def add_revision(self, post_suffix: str, revision_suffix: str, text: str, *, day: int) -> None:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, NULL)",
                (
                    f"revision-{revision_suffix}",
                    f"post-{post_suffix}",
                    digest,
                    text,
                    instant(day).isoformat(),
                ),
            )

    def service(self, embedding, *, fulltext=None, vector=None, batch_size=64) -> IndexService:
        return IndexService(
            self.database,
            self.fulltext if fulltext is None else fulltext,
            self.vector if vector is None else vector,
            embedding,
            clock=lambda: instant(10),
            batch_size=batch_size,
        )

    def generation_row(self, generation_id: str):
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT * FROM index_generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()

    def head(self, person_id: str = PERSON_A) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT generation_id FROM person_index_heads WHERE person_id = ?",
                (person_id,),
            ).fetchone()
            return None if row is None else row[0]

    def test_real_fulltext_vector_build_is_ready_hybrid_and_switches_head(self) -> None:
        self.add_post("alpha", "alpha durable evidence")
        embedding = FakeEmbeddingProvider(dimension=4)

        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_A)):
            result = self.service(embedding).rebuild_person(PERSON_A)

        self.assertEqual((result.status, result.retrieval_mode), ("ready", "hybrid"))
        self.assertEqual(result.generation_id, GENERATION_A)
        self.assertEqual(self.head(), GENERATION_A)
        row = self.generation_row(GENERATION_A)
        self.assertEqual((row["status"], row["retrieval_mode"]), ("ready", "hybrid"))
        self.assertEqual(row["embedding_fingerprint"], result.fingerprint)
        query = embedding.embed(("alpha durable evidence",)).vectors[0]
        hits = self.vector.search_person(GENERATION_A, PERSON_A, query, 5)
        self.assertEqual(len(hits), 1)
        self.assertTrue(
            self.fulltext.search(GENERATION_A, "durable", filters=self._filters(), limit=5)
        )

    @staticmethod
    def _filters():
        from voicevault.fulltext_index import FullTextSearchFilters

        return FullTextSearchFilters()

    def test_missing_or_failing_embedding_degrades_but_zero_chunks_are_ready(self) -> None:
        self.add_post("alpha", "alpha text")
        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_A)):
            missing = self.service(None).rebuild_person(PERSON_A)
        self.assertEqual((missing.status, missing.retrieval_mode), ("degraded", "fulltext_only"))
        self.assertEqual(json.loads(self.generation_row(GENERATION_A)["error_json"])["code"], "embedding_unavailable")

        failing_provider = FakeEmbeddingProvider(fail=True)
        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_B)):
            failing = self.service(failing_provider).rebuild_person(PERSON_A)
        self.assertEqual((failing.status, failing.retrieval_mode), ("degraded", "fulltext_only"))
        self.assertEqual(self.head(), GENERATION_B)
        self.assertNotIn(str(self.data_dir), self.generation_row(GENERATION_B)["error_json"])

        unused = FakeEmbeddingProvider()
        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_C)):
            empty = self.service(unused).rebuild_person(PERSON_B)
        self.assertEqual((empty.status, empty.retrieval_mode), ("ready", "fulltext_only"))
        self.assertEqual(unused.batches, ())

    def test_fulltext_or_vector_failure_marks_failed_and_preserves_old_head(self) -> None:
        self.add_post("alpha", "alpha text")
        embedding = FakeEmbeddingProvider(dimension=4)
        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_A)):
            self.service(embedding).rebuild_person(PERSON_A)

        for generation_id, fulltext, vector, code in (
            (GENERATION_B, FailingFullText(), self.vector, "fulltext_failed"),
            (GENERATION_C, self.fulltext, FailingVector(self.data_dir), "vector_failed"),
        ):
            with self.subTest(code=code), patch(
                "voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(generation_id)
            ):
                result = self.service(embedding, fulltext=fulltext, vector=vector).rebuild_person(PERSON_A)
            self.assertEqual(result.status, "failed")
            self.assertEqual(self.head(), GENERATION_A)
            row = self.generation_row(generation_id)
            self.assertEqual(json.loads(row["error_json"])["code"], code)
            self.assertNotIn(str(self.data_dir), row["error_json"])

    def test_head_switch_occurs_only_after_vector_publish(self) -> None:
        self.add_post("alpha", "alpha text")
        first = FakeEmbeddingProvider(dimension=4)
        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_A)):
            self.service(first).rebuild_person(PERSON_A)
        self.add_post("beta", "beta text", day=3)
        inspecting = InspectingVector(self.data_dir, self.database, GENERATION_A)

        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_B)):
            result = self.service(FakeEmbeddingProvider(dimension=4), vector=inspecting).rebuild_person(PERSON_A)

        self.assertEqual(inspecting.observed_head, GENERATION_A)
        self.assertEqual(result.status, "ready")
        self.assertEqual(self.head(), GENERATION_B)
        self.assertEqual(self.generation_row(GENERATION_A)["status"], "stale")

    def test_same_fingerprint_reuses_unchanged_vectors_and_embeds_only_new_chunks(self) -> None:
        self.add_post("alpha", "alpha unchanged")
        embedding = FakeEmbeddingProvider(dimension=4)
        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_A)):
            self.service(embedding, batch_size=1).rebuild_person(PERSON_A)
        previous_calls = len(embedding.batches)
        self.add_post("beta", "beta new", day=3)

        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_B)):
            result = self.service(embedding, batch_size=1).rebuild_person(PERSON_A)

        self.assertEqual(result.status, "ready")
        self.assertEqual(embedding.batches[previous_calls:], (("beta new",),))
        shard = self.vector.load_person_shard(GENERATION_B, PERSON_A)
        self.assertEqual(len(shard.chunk_ids), 2)
        after_incremental = len(embedding.batches)
        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_C)):
            unchanged = self.service(embedding, batch_size=1).rebuild_person(PERSON_A)
        self.assertEqual(unchanged.status, "ready")
        self.assertEqual(len(embedding.batches), after_incremental)

    def test_new_revision_keeps_history_and_embeds_only_new_revision_chunks(self) -> None:
        self.add_post("history", "old revision", day=2)
        embedding = FakeEmbeddingProvider(dimension=4)
        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_A)):
            self.service(embedding, batch_size=1).rebuild_person(PERSON_A)
        previous_calls = len(embedding.batches)
        self.add_revision("history", "history-new", "new revision", day=3)

        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_B)):
            result = self.service(embedding, batch_size=1).rebuild_person(PERSON_A)

        with self.database.connect() as connection:
            revisions = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT c.revision_id
                    FROM index_generation_chunks gc
                    JOIN knowledge_chunks c ON c.chunk_id = gc.chunk_id
                    WHERE gc.generation_id = ?
                    """,
                    (GENERATION_B,),
                )
            }
        self.assertEqual(result.status, "ready")
        self.assertEqual(revisions, {"revision-history", "revision-history-new"})
        self.assertEqual(embedding.batches[previous_calls:], (("new revision",),))
        self.assertEqual(
            len(self.vector.load_person_shard(GENERATION_B, PERSON_A).chunk_ids), 2
        )

    def test_suppressed_and_purged_posts_exclude_all_revisions_from_generation(self) -> None:
        self.add_post("hidden", "hidden old", day=2)
        self.add_revision("hidden", "hidden-new", "hidden new", day=3)
        self.add_post("gone", "gone old", day=2)
        self.add_revision("gone", "gone-new", "gone new", day=3)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE content_dispositions SET state = 'suppressed', reason = 'hidden' WHERE post_id = 'post-hidden'"
            )
            connection.execute(
                """
                UPDATE content_dispositions
                SET state = 'purged', reason = 'gone', purged_content_hash = ?
                WHERE post_id = 'post-gone'
                """,
                ("a" * 64,),
            )
        embedding = FakeEmbeddingProvider(dimension=4)

        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_A)):
            result = self.service(embedding).rebuild_person(PERSON_A)

        with self.database.connect() as connection:
            frozen_count = connection.execute(
                "SELECT COUNT(*) FROM index_generation_chunks WHERE generation_id = ?",
                (GENERATION_A,),
            ).fetchone()[0]
        self.assertEqual((result.status, result.retrieval_mode), ("ready", "fulltext_only"))
        self.assertEqual(frozen_count, 0)
        self.assertEqual(embedding.batches, ())

    def test_model_fingerprint_change_reembeds_every_chunk(self) -> None:
        self.add_post("alpha", "alpha text")
        self.add_post("beta", "beta text", day=3)
        first = FakeEmbeddingProvider(model="model-v1", dimension=4)
        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_A)):
            self.service(first, batch_size=1).rebuild_person(PERSON_A)
        changed = FakeEmbeddingProvider(model="model-v2", dimension=4)

        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_B)):
            result = self.service(changed, batch_size=1).rebuild_person(PERSON_A)

        self.assertEqual(result.status, "ready")
        self.assertEqual(changed.batches, (("alpha text",), ("beta text",)))
        self.assertNotEqual(result.fingerprint, self.generation_row(GENERATION_A)["embedding_fingerprint"])

    def test_fake_dimension_change_reembeds_all_unchanged_chunks(self) -> None:
        self.add_post("alpha", "alpha text")
        self.add_post("beta", "beta text", day=3)
        first = FakeEmbeddingProvider(model="same-model", dimension=4)
        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_A)):
            first_result = self.service(first, batch_size=1).rebuild_person(PERSON_A)
        changed = FakeEmbeddingProvider(model="same-model", dimension=5)

        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_B)):
            changed_result = self.service(changed, batch_size=1).rebuild_person(PERSON_A)

        self.assertEqual(changed_result.status, "ready")
        self.assertEqual(changed.batches, (("alpha text",), ("beta text",)))
        self.assertNotEqual(changed_result.fingerprint, first_result.fingerprint)
        shard = self.vector.load_person_shard(GENERATION_B, PERSON_A)
        self.assertEqual(shard.dimension, 5)
        self.assertEqual(shard.provider_fingerprint, changed_result.fingerprint)

    def test_openai_identity_reuses_unchanged_and_model_or_base_change_reembeds_all(self) -> None:
        self.add_post("alpha", "alpha text")
        self.add_post("beta", "beta text", day=3)

        def compatible(base_url: str, model: str):
            opener = DeterministicOpenAIOpener()
            provider = OpenAICompatibleEmbeddingProvider.from_environment(
                {
                    "VOICEVAULT_EMBEDDING_BASE_URL": base_url,
                    "VOICEVAULT_EMBEDDING_MODEL": model,
                    "VOICEVAULT_EMBEDDING_API_KEY": "must-not-enter-fingerprint",
                },
                opener=opener,
            )
            return provider, opener

        first, first_opener = compatible("https://embedding-a.example/v1", "model-v1")
        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_A)):
            first_result = self.service(first, batch_size=1).rebuild_person(PERSON_A)
        self.assertEqual(first_result.status, "ready")
        self.assertEqual(first_opener.calls, [("alpha text",), ("beta text",)])

        unchanged, unchanged_opener = compatible(
            "https://embedding-a.example/v1", "model-v1"
        )
        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_B)):
            unchanged_result = self.service(unchanged, batch_size=1).rebuild_person(PERSON_A)
        self.assertEqual(unchanged_result.status, "ready")
        self.assertEqual(unchanged_opener.calls, [])
        self.assertEqual(unchanged_result.fingerprint, first_result.fingerprint)

        changed_model, model_opener = compatible(
            "https://embedding-a.example/v1", "model-v2"
        )
        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_C)):
            model_result = self.service(changed_model, batch_size=1).rebuild_person(PERSON_A)
        self.assertEqual(model_opener.calls, [("alpha text",), ("beta text",)])
        self.assertNotEqual(model_result.fingerprint, unchanged_result.fingerprint)

        changed_base, base_opener = compatible(
            "https://embedding-b.example/v1", "model-v2"
        )
        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_D)):
            base_result = self.service(changed_base, batch_size=1).rebuild_person(PERSON_A)
        self.assertEqual(base_opener.calls, [("alpha text",), ("beta text",)])
        self.assertNotEqual(base_result.fingerprint, model_result.fingerprint)

    def test_provider_without_identity_contract_degrades_explicitly(self) -> None:
        self.add_post("alpha", "alpha text")

        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_E)):
            result = self.service(LegacyEmbeddingProvider()).rebuild_person(PERSON_A)

        self.assertEqual((result.status, result.retrieval_mode), ("degraded", "fulltext_only"))
        self.assertEqual(
            json.loads(self.generation_row(GENERATION_E)["error_json"])["code"],
            "embedding_unavailable",
        )

    def test_concurrent_newer_generation_wins_and_older_becomes_stale(self) -> None:
        self.add_post("alpha", "alpha text")
        callback_fulltext = CallbackFullText(self.fulltext)
        embedding = FakeEmbeddingProvider(dimension=4)
        services = self.service(embedding, fulltext=callback_fulltext)
        nested_result = None

        def run_newer():
            nonlocal nested_result
            nested_result = services.rebuild_person(PERSON_A)

        callback_fulltext.callback = run_newer
        generation_ids = iter((uuid.UUID(GENERATION_A), uuid.UUID(GENERATION_B)))
        real_uuid4 = uuid.uuid4

        def generation_then_staging_uuid():
            return next(generation_ids, real_uuid4())

        with patch(
            "voicevault.index_service.uuid.uuid4",
            side_effect=generation_then_staging_uuid,
        ):
            older = services.rebuild_person(PERSON_A)

        self.assertIsNotNone(nested_result)
        self.assertEqual(nested_result.generation_id, GENERATION_B)
        self.assertEqual(older.status, "stale")
        self.assertEqual(self.head(), GENERATION_B)
        self.assertEqual(self.generation_row(GENERATION_A)["status"], "stale")

    def test_fresh_derived_directory_rebuilds_from_database_without_deleting_old_generation(self) -> None:
        self.add_post("alpha", "alpha text")
        embedding = FakeEmbeddingProvider(dimension=4)
        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_A)):
            self.service(embedding).rebuild_person(PERSON_A)
        old_files = {
            path.relative_to(self.data_dir).as_posix(): path.read_bytes()
            for path in (self.data_dir / "indexes" / "generations" / GENERATION_A).rglob("*")
            if path.is_file()
        }
        fresh_dir = self.data_dir / "fresh-derived"
        fresh_service = IndexService(
            self.database,
            LocalFullTextIndexProvider(fresh_dir),
            LocalVectorIndexProvider(fresh_dir),
            embedding,
            clock=lambda: instant(11),
        )

        with patch("voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_B)):
            result = fresh_service.rebuild_person(PERSON_A)

        self.assertEqual((result.status, result.retrieval_mode), ("ready", "hybrid"))
        self.assertEqual(self.head(), GENERATION_B)
        self.assertTrue(
            fresh_dir.joinpath("indexes", "generations", GENERATION_B, "persons", f"{PERSON_A}.json").is_file()
        )
        self.assertEqual(
            {
                path.relative_to(self.data_dir).as_posix(): path.read_bytes()
                for path in (self.data_dir / "indexes" / "generations" / GENERATION_A).rglob("*")
                if path.is_file()
            },
            old_files,
        )


if __name__ == "__main__":
    unittest.main()
