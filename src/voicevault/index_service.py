from __future__ import annotations

import json
import math
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .app_db import AppDatabase
from .embedding import (
    EmbeddingBatch,
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingResponseInvalid,
    EmbeddingUnavailable,
)
from .fulltext_index import FullTextDocument, FullTextIndexProvider
from .knowledge_chunks import ChunkingRule, KnowledgeChunkRepository
from .vector_index import (
    VectorIndexError,
    VectorIndexProvider,
    VectorShard,
    VectorShardNotFound,
)


_CHUNK_RULE = ChunkingRule()


@dataclass(frozen=True)
class IndexBuildResult:
    status: str
    retrieval_mode: str
    generation_id: str
    fingerprint: str | None


@dataclass(frozen=True)
class _BuildSnapshot:
    generation_id: str
    person_id: str
    created_at: str
    chunk_ids: tuple[str, ...]
    texts: tuple[str, ...]
    fulltext_documents: tuple[FullTextDocument, ...]
    old_generation_id: str | None
    old_fingerprint: str | None


@dataclass(frozen=True)
class _EmbeddingIdentity:
    model: str
    dimension: int
    provider_fingerprint: str


class IndexService:
    def __init__(
        self,
        database: AppDatabase,
        fulltext_provider: FullTextIndexProvider,
        vector_provider: VectorIndexProvider,
        embedding_provider: EmbeddingProvider | None,
        clock: Callable[[], datetime],
        batch_size: int = 64,
    ) -> None:
        if not isinstance(database, AppDatabase):
            raise TypeError("Index database must be an AppDatabase.")
        if not callable(clock):
            raise TypeError("Index clock must be callable.")
        if (
            not isinstance(batch_size, int)
            or isinstance(batch_size, bool)
            or batch_size < 1
        ):
            raise ValueError("Embedding batch size must be positive.")
        self.database = database
        self.fulltext_provider = fulltext_provider
        self.vector_provider = vector_provider
        self.embedding_provider = embedding_provider
        self.clock = clock
        self.batch_size = batch_size

    def rebuild_person(self, person_id: str) -> IndexBuildResult:
        person = _canonical_uuid(person_id, "Person ID")
        generation_id = str(uuid.uuid4())
        try:
            snapshot = self._begin_generation(generation_id, person)
        except (sqlite3.Error, ValueError, TypeError):
            return IndexBuildResult("failed", "none", generation_id, None)

        try:
            self.fulltext_provider.build(
                generation_id, snapshot.fulltext_documents
            )
        except Exception:
            self._mark_failed(generation_id, "fulltext_failed")
            return IndexBuildResult("failed", "none", generation_id, None)

        if not snapshot.chunk_ids:
            return self._activate_or_fail(
                snapshot,
                status="ready",
                retrieval_mode="fulltext_only",
                identity=None,
                error_code=None,
            )
        if self.embedding_provider is None:
            return self._activate_or_fail(
                snapshot,
                status="degraded",
                retrieval_mode="fulltext_only",
                identity=None,
                error_code="embedding_unavailable",
            )

        try:
            embeddings = self._prepare_embeddings(snapshot)
        except EmbeddingError:
            return self._activate_or_fail(
                snapshot,
                status="degraded",
                retrieval_mode="fulltext_only",
                identity=None,
                error_code="embedding_unavailable",
            )
        except VectorIndexError:
            self._mark_failed(generation_id, "vector_failed")
            return IndexBuildResult("failed", "none", generation_id, None)
        except Exception:
            self._mark_failed(generation_id, "embedding_failed")
            return IndexBuildResult("failed", "none", generation_id, None)

        identity = _EmbeddingIdentity(
            embeddings.model,
            embeddings.dimension,
            embeddings.provider_fingerprint,
        )
        try:
            self.vector_provider.build_person_shard(
                generation_id,
                person,
                snapshot.chunk_ids,
                embeddings,
            )
        except Exception:
            self._mark_failed(generation_id, "vector_failed")
            return IndexBuildResult("failed", "none", generation_id, None)
        try:
            return self._activate(
                snapshot,
                status="ready",
                retrieval_mode="hybrid",
                identity=identity,
                error_code=None,
            )
        except Exception:
            self._mark_failed(generation_id, "database_failed")
            return IndexBuildResult("failed", "none", generation_id, None)

    def _begin_generation(self, generation_id: str, person_id: str) -> _BuildSnapshot:
        created_at = _serialize_time(self.clock())
        repository = KnowledgeChunkRepository(self.database)
        with self.database.transaction(immediate=True) as connection:
            if connection.execute(
                "SELECT 1 FROM persons WHERE person_id = ?", (person_id,)
            ).fetchone() is None:
                raise ValueError("Person does not exist.")
            old = connection.execute(
                """
                SELECT g.generation_id, g.embedding_fingerprint
                FROM person_index_heads h
                JOIN index_generations g ON g.generation_id = h.generation_id
                WHERE h.person_id = ?
                  AND g.status = 'ready'
                  AND g.retrieval_mode = 'hybrid'
                """,
                (person_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO index_generations(
                    generation_id, person_id, chunk_rule_version,
                    status, retrieval_mode, created_at
                ) VALUES (?, ?, ?, 'building', 'none', ?)
                """,
                (generation_id, person_id, _CHUNK_RULE.version, created_at),
            )
            chunks = repository.materialize_active_revisions(
                connection, person_id, _CHUNK_RULE
            )
            connection.executemany(
                """
                INSERT INTO index_generation_chunks(generation_id, chunk_id)
                VALUES (?, ?)
                """,
                ((generation_id, chunk.chunk_id) for chunk in chunks),
            )
            rows = connection.execute(
                """
                SELECT c.chunk_id, c.content_text, a.platform, p.published_at
                FROM index_generation_chunks gc
                JOIN knowledge_chunks c ON c.chunk_id = gc.chunk_id
                JOIN post_revisions r ON r.revision_id = c.revision_id
                JOIN posts p ON p.post_id = r.post_id
                JOIN platform_accounts a ON a.account_id = p.account_id
                WHERE gc.generation_id = ?
                ORDER BY c.chunk_id
                """,
                (generation_id,),
            ).fetchall()

        chunk_ids = tuple(row["chunk_id"] for row in rows)
        texts = tuple(row["content_text"] for row in rows)
        documents = tuple(
            FullTextDocument(
                chunk_id=row["chunk_id"],
                person_id=person_id,
                platform=row["platform"],
                published_at=_parse_optional_time(row["published_at"]),
                text=row["content_text"],
            )
            for row in rows
        )
        return _BuildSnapshot(
            generation_id=generation_id,
            person_id=person_id,
            created_at=created_at,
            chunk_ids=chunk_ids,
            texts=texts,
            fulltext_documents=documents,
            old_generation_id=None if old is None else old["generation_id"],
            old_fingerprint=None if old is None else old["embedding_fingerprint"],
        )

    def _prepare_embeddings(self, snapshot: _BuildSnapshot) -> EmbeddingBatch:
        provider = self.embedding_provider
        if provider is None:
            raise AssertionError("Embedding provider is required.")
        reuse_shard = self._load_reuse_shard(snapshot)
        old_positions = (
            {} if reuse_shard is None else {
                chunk_id: index for index, chunk_id in enumerate(reuse_shard.chunk_ids)
            }
        )
        current_positions = {
            chunk_id: index for index, chunk_id in enumerate(snapshot.chunk_ids)
        }
        new_indices = tuple(
            index
            for index, chunk_id in enumerate(snapshot.chunk_ids)
            if chunk_id not in old_positions
        )
        embedded: dict[int, tuple[float, ...]] = {}
        identity: _EmbeddingIdentity | None = None

        if reuse_shard is None:
            target_indices = tuple(range(len(snapshot.chunk_ids)))
            identity = self._embed_indices(snapshot, target_indices, embedded, None)
        else:
            known_fingerprint = _provider_fingerprint_for_dimension(
                provider, reuse_shard.dimension
            )
            if known_fingerprint == reuse_shard.provider_fingerprint:
                identity = _EmbeddingIdentity(
                    reuse_shard.model,
                    reuse_shard.dimension,
                    reuse_shard.provider_fingerprint,
                )
                if new_indices:
                    identity = self._embed_indices(
                        snapshot, new_indices, embedded, identity
                    )
            else:
                identity = self._embed_indices(
                    snapshot,
                    tuple(range(len(snapshot.chunk_ids))),
                    embedded,
                    None,
                )
            if identity.provider_fingerprint == reuse_shard.provider_fingerprint:
                shared = tuple(
                    chunk_id
                    for chunk_id in snapshot.chunk_ids
                    if chunk_id in old_positions
                )
                old_vectors = self.vector_provider.read_person_vectors(
                    reuse_shard.generation_id,
                    snapshot.person_id,
                    shared,
                )
                for chunk_id, vector in zip(shared, old_vectors, strict=True):
                    embedded[current_positions[chunk_id]] = vector
            else:
                remaining = tuple(
                    index
                    for index in range(len(snapshot.chunk_ids))
                    if index not in embedded
                )
                identity = self._embed_indices(
                    snapshot, remaining, embedded, identity
                )

        if identity is None or len(embedded) != len(snapshot.chunk_ids):
            raise ValueError("Embedding build is incomplete.")
        vectors = tuple(embedded[index] for index in range(len(snapshot.chunk_ids)))
        return EmbeddingBatch(
            model=identity.model,
            dimension=identity.dimension,
            provider_fingerprint=identity.provider_fingerprint,
            vectors=vectors,
        )

    def _load_reuse_shard(self, snapshot: _BuildSnapshot) -> VectorShard | None:
        if snapshot.old_generation_id is None or snapshot.old_fingerprint is None:
            return None
        try:
            shard = self.vector_provider.load_person_shard(
                snapshot.old_generation_id, snapshot.person_id
            )
        except VectorShardNotFound:
            return None
        if shard.provider_fingerprint != snapshot.old_fingerprint:
            raise ValueError("Stored embedding fingerprint is inconsistent.")
        return shard

    def _embed_indices(
        self,
        snapshot: _BuildSnapshot,
        indices: tuple[int, ...],
        embedded: dict[int, tuple[float, ...]],
        expected: _EmbeddingIdentity | None,
    ) -> _EmbeddingIdentity:
        provider = self.embedding_provider
        if provider is None or not indices:
            if expected is None:
                raise ValueError("No embedding identity is available.")
            return expected
        identity = expected
        for offset in range(0, len(indices), self.batch_size):
            batch_indices = indices[offset : offset + self.batch_size]
            response = provider.embed(
                tuple(snapshot.texts[index] for index in batch_indices)
            )
            if not isinstance(response, EmbeddingBatch):
                raise EmbeddingResponseInvalid(
                    "Embedding provider returned an invalid response."
                )
            if response.provider_fingerprint != _provider_fingerprint_for_dimension(
                provider, response.dimension
            ):
                raise EmbeddingResponseInvalid(
                    "Embedding provider returned an invalid response."
                )
            current = _EmbeddingIdentity(
                response.model,
                response.dimension,
                response.provider_fingerprint,
            )
            if identity is None:
                identity = current
            elif current != identity:
                raise ValueError("Embedding provider identity changed during build.")
            if len(response.vectors) != len(batch_indices):
                raise ValueError("Embedding provider returned an invalid count.")
            for index, vector in zip(batch_indices, response.vectors, strict=True):
                embedded[index] = vector
        if identity is None:
            raise ValueError("No embedding identity is available.")
        return identity

    def _activate(
        self,
        snapshot: _BuildSnapshot,
        *,
        status: str,
        retrieval_mode: str,
        identity: _EmbeddingIdentity | None,
        error_code: str | None,
    ) -> IndexBuildResult:
        completed_at = _serialize_time(self.clock())
        with self.database.transaction(immediate=True) as connection:
            current = connection.execute(
                """
                SELECT g.generation_id, g.created_at
                FROM person_index_heads h
                JOIN index_generations g ON g.generation_id = h.generation_id
                WHERE h.person_id = ?
                """,
                (snapshot.person_id,),
            ).fetchone()
            if current is not None and (
                current["created_at"], current["generation_id"]
            ) > (snapshot.created_at, snapshot.generation_id):
                connection.execute(
                    """
                    UPDATE index_generations
                    SET embedding_provider = ?, embedding_model = ?,
                        embedding_dimension = ?, embedding_fingerprint = ?,
                        status = 'stale', retrieval_mode = ?, error_json = ?,
                        completed_at = ?
                    WHERE generation_id = ? AND status = 'building'
                    """,
                    (
                        None if identity is None else type(self.embedding_provider).__name__,
                        None if identity is None else identity.model,
                        None if identity is None else identity.dimension,
                        None if identity is None else identity.provider_fingerprint,
                        retrieval_mode,
                        _error_json(error_code),
                        completed_at,
                        snapshot.generation_id,
                    ),
                )
                return IndexBuildResult(
                    "stale",
                    retrieval_mode,
                    snapshot.generation_id,
                    None if identity is None else identity.provider_fingerprint,
                )

            if current is not None and current["generation_id"] != snapshot.generation_id:
                connection.execute(
                    "UPDATE index_generations SET status = 'stale' WHERE generation_id = ?",
                    (current["generation_id"],),
                )
            connection.execute(
                """
                UPDATE index_generations
                SET embedding_provider = ?, embedding_model = ?,
                    embedding_dimension = ?, embedding_fingerprint = ?,
                    status = ?, retrieval_mode = ?, error_json = ?, completed_at = ?
                WHERE generation_id = ? AND status = 'building'
                """,
                (
                    None if identity is None else type(self.embedding_provider).__name__,
                    None if identity is None else identity.model,
                    None if identity is None else identity.dimension,
                    None if identity is None else identity.provider_fingerprint,
                    status,
                    retrieval_mode,
                    _error_json(error_code),
                    completed_at,
                    snapshot.generation_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO person_index_heads(person_id, generation_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(person_id) DO UPDATE SET
                    generation_id = excluded.generation_id,
                    updated_at = excluded.updated_at
                """,
                (snapshot.person_id, snapshot.generation_id, completed_at),
            )
        return IndexBuildResult(
            status,
            retrieval_mode,
            snapshot.generation_id,
            None if identity is None else identity.provider_fingerprint,
        )

    def _activate_or_fail(
        self,
        snapshot: _BuildSnapshot,
        *,
        status: str,
        retrieval_mode: str,
        identity: _EmbeddingIdentity | None,
        error_code: str | None,
    ) -> IndexBuildResult:
        try:
            return self._activate(
                snapshot,
                status=status,
                retrieval_mode=retrieval_mode,
                identity=identity,
                error_code=error_code,
            )
        except Exception:
            self._mark_failed(snapshot.generation_id, "database_failed")
            return IndexBuildResult("failed", "none", snapshot.generation_id, None)

    def _mark_failed(self, generation_id: str, code: str) -> None:
        try:
            completed_at = _serialize_time(self.clock())
            with self.database.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    UPDATE index_generations
                    SET status = 'failed', retrieval_mode = 'none',
                        error_json = ?, completed_at = ?
                    WHERE generation_id = ? AND status = 'building'
                    """,
                    (_error_json(code), completed_at, generation_id),
                )
        except Exception:
            return


def _canonical_uuid(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical UUID.")
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"{label} must be a canonical UUID.") from None
    if value != canonical:
        raise ValueError(f"{label} must be a canonical UUID.")
    return canonical


def _serialize_time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Index clock must return a timezone-aware datetime.")
    normalized = value.astimezone(timezone.utc)
    if not math.isfinite(normalized.timestamp()):
        raise ValueError("Index clock returned an invalid datetime.")
    return normalized.isoformat()


def _parse_optional_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ValueError("Stored published time is invalid.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Stored published time is invalid.")
    return parsed.astimezone(timezone.utc)


def _error_json(code: str | None) -> str | None:
    if code is None:
        return None
    return json.dumps({"code": code}, sort_keys=True, separators=(",", ":"))


def _provider_fingerprint_for_dimension(
    provider: EmbeddingProvider,
    dimension: int,
) -> str:
    method = getattr(provider, "fingerprint_for_dimension", None)
    if not callable(method):
        raise EmbeddingUnavailable("Embedding provider identity is unavailable.")
    try:
        fingerprint = method(dimension)
    except EmbeddingError:
        raise
    except Exception:
        raise EmbeddingUnavailable("Embedding provider identity is unavailable.") from None
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        raise EmbeddingUnavailable("Embedding provider identity is unavailable.")
    return fingerprint
