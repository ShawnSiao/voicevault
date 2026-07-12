from __future__ import annotations

import math
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .app_db import AppDatabase
from .embedding import EmbeddingBatch, EmbeddingError, EmbeddingProvider
from .fulltext_index import FullTextIndexProvider, FullTextSearchFilters
from .retrieval import (
    EvidenceHit,
    EvidenceSet,
    RetrievalError,
    RetrievalPersonSnapshot,
    RetrievalRepository,
    RetrievalRequest,
)
from .vector_index import VectorIndexProvider


class IndexStale(RetrievalError):
    """No requested person has a frozen usable index generation."""


class RetrievalPersonNotFound(RetrievalError):
    """A requested person does not exist."""


class RetrievalExecutionError(RetrievalError):
    """A retrieval run failed outside a supported per-person degradation."""


@dataclass(frozen=True)
class _Candidate:
    chunk_id: str
    person_id: str
    account_id: str
    platform: str
    post_id: str
    revision_id: str
    generation_id: str
    canonical_url: str | None
    published_at: datetime | None
    captured_at: datetime
    observation_status: str | None
    observed_at: datetime | None
    char_start: int
    char_end: int


@dataclass(frozen=True)
class _Ranked:
    candidate: _Candidate
    fulltext_rank: int | None
    vector_rank: int | None
    score: float
    best_rank: int


class RetrievalService:
    def __init__(
        self,
        database: AppDatabase,
        repository: RetrievalRepository,
        fulltext_provider: FullTextIndexProvider,
        vector_provider: VectorIndexProvider,
        embedding_provider: EmbeddingProvider | None,
        clock: Callable[[], datetime],
        candidate_pool: int = 100,
    ) -> None:
        if not isinstance(database, AppDatabase):
            raise TypeError("Retrieval database must be an AppDatabase.")
        if not isinstance(repository, RetrievalRepository):
            raise TypeError("Retrieval repository is invalid.")
        if not callable(clock):
            raise TypeError("Retrieval clock must be callable.")
        if (
            not isinstance(candidate_pool, int)
            or isinstance(candidate_pool, bool)
            or not 1 <= candidate_pool <= 100
        ):
            raise ValueError("Candidate pool must be between 1 and 100.")
        self.database = database
        self.repository = repository
        self.fulltext_provider = fulltext_provider
        self.vector_provider = vector_provider
        self.embedding_provider = embedding_provider
        self.clock = clock
        self.candidate_pool = candidate_pool

    def create_run(self, request: RetrievalRequest) -> EvidenceSet:
        if not isinstance(request, RetrievalRequest):
            raise TypeError("Retrieval request is invalid.")
        run_id = str(uuid.uuid4())
        with self.database.transaction(immediate=True) as connection:
            self._preflight(connection, request)
            self.repository.create_run(
                connection, run_id, request, created_at=self._now()
            )
            return self.repository.get_run(connection, run_id)

    def preflight(self, request: RetrievalRequest) -> None:
        if not isinstance(request, RetrievalRequest):
            raise TypeError("Retrieval request is invalid.")
        with self.database.connect() as connection:
            self._preflight(connection, request)

    def get_run(self, run_id: str) -> EvidenceSet:
        with self.database.connect() as connection:
            return self.repository.get_run(connection, run_id)

    def fail_incomplete(self, run_id: str, code: str) -> EvidenceSet:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("Retrieval failure code is required.")
        with self.database.transaction(immediate=True) as connection:
            return self.repository.fail(
                connection,
                run_id,
                error={"code": code.strip()},
                completed_at=self._now(),
            )

    def reconcile_incomplete(self) -> int:
        with self.database.transaction(immediate=True) as connection:
            run_ids = tuple(
                row["run_id"]
                for row in connection.execute(
                    """
                    SELECT run_id FROM retrieval_runs
                    WHERE status IN ('pending', 'running')
                    ORDER BY created_at, run_id
                    """
                )
            )
            completed_at = self._now()
            for run_id in run_ids:
                self.repository.interrupt(
                    connection,
                    run_id,
                    error={"code": "service_restarted"},
                    completed_at=completed_at,
                )
            return len(run_ids)

    def execute(self, run_id: str) -> EvidenceSet:
        with self.database.transaction(immediate=True) as connection:
            running = self.repository.mark_running(
                connection, run_id, started_at=self._now()
            )
        if all(
            person.generation_id is None
            or person.generation_status not in {"ready", "degraded"}
            for person in running.persons
        ):
            with self.database.transaction(immediate=True) as connection:
                self.repository.fail(
                    connection,
                    run_id,
                    error={"code": "index_stale"},
                    completed_at=self._now(),
                )
            raise IndexStale("No requested person has a usable index.")

        try:
            return self._execute_running(running)
        except IndexStale:
            raise
        except Exception:
            with self.database.transaction(immediate=True) as connection:
                failed = self.repository.fail(
                    connection,
                    run_id,
                    error={"code": "retrieval_failed"},
                    completed_at=self._now(),
                )
            raise RetrievalExecutionError("Retrieval execution failed.") from None

    @staticmethod
    def _preflight(
        connection: sqlite3.Connection,
        request: RetrievalRequest,
    ) -> None:
        usable = False
        for person_id in request.person_ids:
            row = connection.execute(
                """
                SELECT p.person_id, g.status
                FROM persons p
                LEFT JOIN person_index_heads h ON h.person_id = p.person_id
                LEFT JOIN index_generations g ON g.generation_id = h.generation_id
                WHERE p.person_id = ?
                """,
                (person_id,),
            ).fetchone()
            if row is None:
                raise RetrievalPersonNotFound("Requested person was not found.")
            if row["status"] in {"ready", "degraded"}:
                usable = True
        if not usable:
            raise IndexStale("No requested person has a usable index.")

    def _execute_running(self, running: EvidenceSet) -> EvidenceSet:
        request = running.request
        candidates: dict[str, dict[str, _Candidate]] = {}
        fulltext_hits: dict[str, tuple[object, ...]] = {}
        vector_hits: dict[str, tuple[object, ...]] = {}
        channel_limits: dict[str, int] = {}
        query_vectors: dict[str, tuple[float, ...]] = {}
        person_modes = {
            person.person_id: ("none" if person.generation_id is None else person.retrieval_mode)
            for person in running.persons
        }
        degradation: dict[str, object] = {
            "missing_person_ids": list(running.missing_person_ids),
            "persons": {},
        }

        with self.database.connect() as connection:
            connection.create_function(
                "voicevault_utc_microseconds",
                1,
                _utc_microseconds,
                deterministic=True,
            )
            generation_identity: dict[str, tuple[str, int]] = {}
            for person in running.persons:
                if person.generation_id is None:
                    continue
                if person.generation_status not in {"ready", "degraded"}:
                    person_modes[person.person_id] = "none"
                    degradation["persons"][person.person_id] = {"code": "index_stale"}  # type: ignore[index]
                    continue
                allowed = self._allowed_candidates(connection, person, request)
                candidates[person.person_id] = {
                    item.chunk_id: item for item in allowed
                }
                allowed_ids = tuple(item.chunk_id for item in allowed)
                channel_limit = min(
                    max(self.candidate_pool, request.limit),
                    max(1, len(allowed_ids)),
                )
                channel_limits[person.person_id] = channel_limit
                fulltext_hits[person.person_id] = self.fulltext_provider.search(
                    person.generation_id,
                    request.query,
                    FullTextSearchFilters(
                        person_ids=(person.person_id,),
                        allowed_chunk_ids=allowed_ids,
                    ),
                    channel_limit,
                )
                if person.retrieval_mode == "hybrid" and person.generation_status == "ready":
                    row = connection.execute(
                        """
                        SELECT embedding_fingerprint, embedding_dimension
                        FROM index_generations WHERE generation_id = ?
                        """,
                        (person.generation_id,),
                    ).fetchone()
                    if (
                        row is not None
                        and isinstance(row["embedding_fingerprint"], str)
                        and isinstance(row["embedding_dimension"], int)
                    ):
                        generation_identity[person.person_id] = (
                            row["embedding_fingerprint"],
                            row["embedding_dimension"],
                        )

        groups: dict[tuple[str, int], list[RetrievalPersonSnapshot]] = {}
        for person in running.persons:
            if person.person_id not in generation_identity:
                if person.generation_id is not None and person_modes[person.person_id] == "hybrid":
                    self._degrade(person.person_id, person_modes, degradation, "provider_unavailable")
                continue
            fingerprint, dimension = generation_identity[person.person_id]
            provider = self.embedding_provider
            try:
                if provider is None or provider.fingerprint_for_dimension(dimension) != fingerprint:
                    raise ValueError
            except Exception:
                self._degrade(person.person_id, person_modes, degradation, "provider_unavailable")
                continue
            groups.setdefault((fingerprint, dimension), []).append(person)

        for identity, people in groups.items():
            try:
                response = self.embedding_provider.embed((request.query,))  # type: ignore[union-attr]
                if (
                    not isinstance(response, EmbeddingBatch)
                    or len(response.vectors) != 1
                    or (response.provider_fingerprint, response.dimension) != identity
                ):
                    raise ValueError
            except (EmbeddingError, ValueError, TypeError, AttributeError):
                for person in people:
                    self._degrade(person.person_id, person_modes, degradation, "provider_unavailable")
                continue
            query_vector = response.vectors[0]
            for person in people:
                allowed_ids = tuple(candidates.get(person.person_id, {}))
                query_vectors[person.person_id] = query_vector
                try:
                    vector_hits[person.person_id] = self.vector_provider.search_person(
                        person.generation_id,
                        person.person_id,
                        query_vector,
                        channel_limits[person.person_id],
                        allowed_chunk_ids=allowed_ids,
                    )
                except Exception:
                    self._degrade(person.person_id, person_modes, degradation, "vector_unavailable")

        for person in running.persons:
            metadata = candidates.get(person.person_id, {})
            if person.generation_id is None or not metadata:
                continue
            allowed_ids = tuple(metadata)
            channel_limit = channel_limits[person.person_id]
            while channel_limit < len(allowed_ids):
                active_hits = [fulltext_hits.get(person.person_id, ())]
                if person_modes[person.person_id] == "hybrid" and person.person_id in query_vectors:
                    active_hits.append(vector_hits.get(person.person_id, ()))
                candidate_ids = {
                    hit.chunk_id
                    for hits in active_hits
                    for hit in hits
                    if hit.chunk_id in metadata
                }
                post_counts: dict[str, int] = {}
                for chunk_id in candidate_ids:
                    post_id = metadata[chunk_id].post_id
                    post_counts[post_id] = post_counts.get(post_id, 0) + 1
                capacity = sum(
                    min(count, request.max_chunks_per_post)
                    for count in post_counts.values()
                )
                if capacity >= request.limit or not any(
                    len(hits) >= channel_limit for hits in active_hits
                ):
                    break
                channel_limit = min(len(allowed_ids), channel_limit * 2)
                channel_limits[person.person_id] = channel_limit
                fulltext_hits[person.person_id] = self.fulltext_provider.search(
                    person.generation_id,
                    request.query,
                    FullTextSearchFilters(
                        person_ids=(person.person_id,),
                        allowed_chunk_ids=allowed_ids,
                    ),
                    channel_limit,
                )
                if person_modes[person.person_id] == "hybrid" and person.person_id in query_vectors:
                    try:
                        vector_hits[person.person_id] = self.vector_provider.search_person(
                            person.generation_id,
                            person.person_id,
                            query_vectors[person.person_id],
                            channel_limit,
                            allowed_chunk_ids=allowed_ids,
                        )
                    except Exception:
                        vector_hits.pop(person.person_id, None)
                        self._degrade(
                            person.person_id,
                            person_modes,
                            degradation,
                            "vector_unavailable",
                        )

        ranked: dict[str, tuple[_Ranked, ...]] = {}
        for person in running.persons:
            metadata = candidates.get(person.person_id, {})
            ft = {
                hit.chunk_id: hit.rank
                for hit in fulltext_hits.get(person.person_id, ())
                if hit.chunk_id in metadata
            }
            vector = {
                hit.chunk_id: hit.rank
                for hit in vector_hits.get(person.person_id, ())
                if hit.chunk_id in metadata
            }
            combined: list[_Ranked] = []
            for chunk_id in set(ft) | set(vector):
                ranks = tuple(rank for rank in (ft.get(chunk_id), vector.get(chunk_id)) if rank is not None)
                combined.append(
                    _Ranked(
                        candidate=metadata[chunk_id],
                        fulltext_rank=ft.get(chunk_id),
                        vector_rank=vector.get(chunk_id),
                        score=sum(1.0 / (60 + rank) for rank in ranks),
                        best_rank=min(ranks),
                    )
                )
            combined.sort(key=lambda item: (-item.score, item.best_rank, item.candidate.chunk_id))
            ranked[person.person_id] = tuple(combined)

        selected = self._select(running.persons, ranked, request)
        hits = tuple(
            EvidenceHit(
                evidence_id=str(uuid.uuid4()),
                ordinal=index,
                person_id=item.candidate.person_id,
                account_id=item.candidate.account_id,
                platform=item.candidate.platform,
                post_id=item.candidate.post_id,
                revision_id=item.candidate.revision_id,
                chunk_id=item.candidate.chunk_id,
                generation_id=item.candidate.generation_id,
                canonical_url=item.candidate.canonical_url,
                published_at=item.candidate.published_at,
                captured_at=item.candidate.captured_at,
                observation_status=item.candidate.observation_status,
                observed_at=item.candidate.observed_at,
                char_start=item.candidate.char_start,
                char_end=item.candidate.char_end,
                fulltext_rank=item.fulltext_rank,
                vector_rank=item.vector_rank,
                fused_rank=index + 1,
            )
            for index, item in enumerate(selected)
        )
        overall = self._overall_mode(tuple(person_modes.values()))
        if overall == "none":
            with self.database.transaction(immediate=True) as connection:
                self.repository.fail(
                    connection,
                    running.run_id,
                    error={"code": "index_stale"},
                    completed_at=self._now(),
                )
            raise IndexStale("No requested person has a usable index.")
        with self.database.transaction(immediate=True) as connection:
            return self.repository.complete(
                connection,
                running.run_id,
                retrieval_mode=overall,
                degradation=degradation,
                hits=hits,
                completed_at=self._now(),
                person_modes=person_modes,
            )

    def _allowed_candidates(
        self,
        connection: sqlite3.Connection,
        person: RetrievalPersonSnapshot,
        request: RetrievalRequest,
    ) -> tuple[_Candidate, ...]:
        clauses = ["gc.generation_id = ?", "a.person_id = ?", "d.state = 'active'"]
        parameters: list[object] = [person.generation_id, person.person_id]
        if request.platforms:
            clauses.append(f"a.platform IN ({','.join('?' for _ in request.platforms)})")
            parameters.extend(request.platforms)
        if request.published_from is not None:
            clauses.append("voicevault_utc_microseconds(p.published_at) >= ?")
            parameters.append(_utc_microseconds(_serialize_time(request.published_from)))
        if request.published_to is not None:
            clauses.append("voicevault_utc_microseconds(p.published_at) < ?")
            parameters.append(_utc_microseconds(_serialize_time(request.published_to)))
        if request.revision_scope == "current":
            clauses.append(
                """
                NOT EXISTS (
                    SELECT 1
                    FROM index_generation_chunks newer_gc
                    JOIN knowledge_chunks newer_c ON newer_c.chunk_id = newer_gc.chunk_id
                    JOIN post_revisions newer_r ON newer_r.revision_id = newer_c.revision_id
                    WHERE newer_gc.generation_id = gc.generation_id
                      AND newer_r.post_id = r.post_id
                      AND (
                           voicevault_utc_microseconds(newer_r.captured_at)
                               > voicevault_utc_microseconds(r.captured_at)
                           OR (
                               voicevault_utc_microseconds(newer_r.captured_at)
                                   = voicevault_utc_microseconds(r.captured_at)
                               AND newer_r.revision_id > r.revision_id
                          )
                      )
                )
                """
            )
        rows = connection.execute(
            f"""
            SELECT c.chunk_id, a.person_id, a.account_id, a.platform,
                   p.post_id, r.revision_id, gc.generation_id, p.canonical_url,
                   p.published_at, r.captured_at, observation.status AS observation_status,
                   observation.observed_at, c.char_start, c.char_end
            FROM index_generation_chunks gc
            JOIN knowledge_chunks c ON c.chunk_id = gc.chunk_id
            JOIN post_revisions r ON r.revision_id = c.revision_id
            JOIN posts p ON p.post_id = r.post_id
            JOIN platform_accounts a ON a.account_id = p.account_id
            JOIN content_dispositions d ON d.post_id = p.post_id
            LEFT JOIN post_observations observation
              ON observation.observation_id = (
                  SELECT o.observation_id FROM post_observations o
                  WHERE o.post_id = p.post_id
                   ORDER BY voicevault_utc_microseconds(o.observed_at) DESC,
                            o.observation_id DESC LIMIT 1
              )
            WHERE {' AND '.join(clauses)}
            ORDER BY c.chunk_id
            """,
            tuple(parameters),
        ).fetchall()
        return tuple(
            _Candidate(
                chunk_id=row["chunk_id"],
                person_id=row["person_id"],
                account_id=row["account_id"],
                platform=row["platform"],
                post_id=row["post_id"],
                revision_id=row["revision_id"],
                generation_id=row["generation_id"],
                canonical_url=row["canonical_url"],
                published_at=_parse_time(row["published_at"]),
                captured_at=_parse_time(row["captured_at"]),  # type: ignore[arg-type]
                observation_status=row["observation_status"],
                observed_at=_parse_time(row["observed_at"]),
                char_start=row["char_start"],
                char_end=row["char_end"],
            )
            for row in rows
        )

    @staticmethod
    def _select(
        people: tuple[RetrievalPersonSnapshot, ...],
        ranked: dict[str, tuple[_Ranked, ...]],
        request: RetrievalRequest,
    ) -> tuple[_Ranked, ...]:
        selected: list[_Ranked] = []
        selected_ids: set[str] = set()
        post_counts: dict[str, int] = {}

        def add(item: _Ranked) -> bool:
            if (
                item.candidate.chunk_id in selected_ids
                or post_counts.get(item.candidate.post_id, 0) >= request.max_chunks_per_post
                or len(selected) >= request.limit
            ):
                return False
            selected.append(item)
            selected_ids.add(item.candidate.chunk_id)
            post_counts[item.candidate.post_id] = post_counts.get(item.candidate.post_id, 0) + 1
            return True

        for person in people:
            if request.min_hits_per_person == 0:
                continue
            count = 0
            for item in ranked.get(person.person_id, ()):
                if add(item):
                    count += 1
                if count >= request.min_hits_per_person:
                    break
        ordinal = {person.person_id: person.ordinal for person in people}
        remaining = [item for values in ranked.values() for item in values if item.candidate.chunk_id not in selected_ids]
        remaining.sort(
            key=lambda item: (
                -item.score,
                item.best_rank,
                ordinal[item.candidate.person_id],
                item.candidate.chunk_id,
            )
        )
        for item in remaining:
            add(item)
            if len(selected) >= request.limit:
                break
        return tuple(selected)

    @staticmethod
    def _degrade(
        person_id: str,
        modes: dict[str, str],
        degradation: dict[str, object],
        code: str,
    ) -> None:
        modes[person_id] = "fulltext_only"
        degradation["persons"][person_id] = {"code": code}  # type: ignore[index]

    @staticmethod
    def _overall_mode(modes: tuple[str, ...]) -> str:
        active = tuple(mode for mode in modes if mode != "none")
        if not active:
            return "none"
        if "hybrid" in active and "fulltext_only" in active:
            return "mixed"
        return "hybrid" if all(mode == "hybrid" for mode in active) else "fulltext_only"

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Retrieval clock must return an aware datetime.")
        return value.astimezone(timezone.utc)


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Stored retrieval time is invalid.")
    return parsed.astimezone(timezone.utc)


def _serialize_time(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


def _utc_microseconds(value: str | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Stored retrieval time is invalid.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Stored retrieval time is invalid.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Stored retrieval time is invalid.")
    delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    result = (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    if not -(2**63) <= result < 2**63:
        raise ValueError("Stored retrieval time is invalid.")
    return result
