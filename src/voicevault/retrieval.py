from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Mapping


_RUN_STATUSES = {"pending", "running", "succeeded", "failed", "interrupted"}
_RUN_MODES = {"none", "hybrid", "mixed", "fulltext_only"}
_PERSON_MODES = {"none", "hybrid", "fulltext_only"}
_GENERATION_STATUSES = {
    "missing",
    "pending",
    "building",
    "ready",
    "degraded",
    "stale",
    "failed",
}
_OBSERVATION_STATUSES = {"available", "deleted", "unavailable"}
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|cookie|credential|local[_-]?path|password|secret|token)",
    re.IGNORECASE,
)
_ABSOLUTE_PATH = re.compile(r"(?:^[A-Za-z]:[\\/]|^/)")


class RetrievalError(Exception):
    """Base class for stable retrieval-run contract failures."""


class RetrievalRunNotFound(RetrievalError):
    """The requested retrieval run does not exist."""


class RetrievalStateError(RetrievalError):
    """The requested retrieval-run state transition is invalid."""


@dataclass(frozen=True)
class RetrievalRequest:
    query: str
    person_ids: tuple[str, ...]
    platforms: tuple[str, ...] = ()
    published_from: datetime | None = None
    published_to: datetime | None = None
    revision_scope: str = "current"
    limit: int = 20
    min_hits_per_person: int = 1
    max_chunks_per_post: int = 2

    def __post_init__(self) -> None:
        if not isinstance(self.query, str) or not self.query.strip():
            raise ValueError("Retrieval query is required.")
        if not isinstance(self.person_ids, tuple):
            raise TypeError("Retrieval person IDs must be a tuple.")
        people = _deduplicate_strings(self.person_ids, "Retrieval person IDs")
        if not 1 <= len(people) <= 10:
            raise ValueError("Retrieval requests require between one and ten people.")
        if not isinstance(self.platforms, tuple):
            raise TypeError("Retrieval platforms must be a tuple.")
        platforms = _deduplicate_strings(
            self.platforms, "Retrieval platforms", allow_empty=True
        )
        published_from = _optional_utc(self.published_from, "Published from")
        published_to = _optional_utc(self.published_to, "Published to")
        if (
            published_from is not None
            and published_to is not None
            and published_from >= published_to
        ):
            raise ValueError("Published time range must be half-open and increasing.")
        if self.revision_scope not in {"current", "all"}:
            raise ValueError("Revision scope must be current or all.")
        _bounded_int(self.limit, "Retrieval limit", minimum=1, maximum=50)
        _bounded_int(
            self.min_hits_per_person,
            "Minimum hits per person",
            minimum=0,
        )
        _bounded_int(
            self.max_chunks_per_post,
            "Maximum chunks per post",
            minimum=1,
            maximum=10,
        )
        if self.limit < len(people) * self.min_hits_per_person:
            raise ValueError("Retrieval limit cannot satisfy the per-person quota.")
        object.__setattr__(self, "query", self.query.strip())
        object.__setattr__(self, "person_ids", people)
        object.__setattr__(self, "platforms", platforms)
        object.__setattr__(self, "published_from", published_from)
        object.__setattr__(self, "published_to", published_to)


@dataclass(frozen=True)
class RetrievalPersonSnapshot:
    person_id: str
    ordinal: int
    generation_id: str | None
    generation_status: str
    retrieval_mode: str

    def __post_init__(self) -> None:
        _required_string(self.person_id, "Person ID")
        _bounded_int(self.ordinal, "Person ordinal", minimum=0)
        if self.generation_id is not None:
            _required_string(self.generation_id, "Generation ID")
        if self.generation_status not in _GENERATION_STATUSES:
            raise ValueError("Generation status is invalid.")
        if self.retrieval_mode not in _PERSON_MODES:
            raise ValueError("Person retrieval mode is invalid.")
        if self.generation_id is None:
            if self.generation_status != "missing" or self.retrieval_mode != "none":
                raise ValueError("Missing generation snapshot is inconsistent.")
        elif self.generation_status == "missing":
            raise ValueError("Present generation snapshot is inconsistent.")


@dataclass(frozen=True)
class EvidenceHit:
    evidence_id: str
    ordinal: int
    person_id: str
    account_id: str
    platform: str
    post_id: str
    revision_id: str
    chunk_id: str
    generation_id: str
    canonical_url: str | None
    published_at: datetime | None
    captured_at: datetime
    observation_status: str | None
    observed_at: datetime | None
    char_start: int
    char_end: int
    fulltext_rank: int | None
    vector_rank: int | None
    fused_rank: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.evidence_id, "Evidence ID"),
            (self.person_id, "Person ID"),
            (self.account_id, "Account ID"),
            (self.platform, "Platform"),
            (self.post_id, "Post ID"),
            (self.revision_id, "Revision ID"),
            (self.chunk_id, "Chunk ID"),
            (self.generation_id, "Generation ID"),
        ):
            _required_string(value, label)
        if self.canonical_url is not None:
            _required_string(self.canonical_url, "Canonical URL")
        _bounded_int(self.ordinal, "Evidence ordinal", minimum=0)
        _bounded_int(self.char_start, "Evidence char_start", minimum=0)
        _bounded_int(self.char_end, "Evidence char_end", minimum=1)
        if self.char_end <= self.char_start:
            raise ValueError("Evidence offsets are invalid.")
        _bounded_int(self.fused_rank, "Fused rank", minimum=1)
        for rank, label in (
            (self.fulltext_rank, "Full-text rank"),
            (self.vector_rank, "Vector rank"),
        ):
            if rank is not None:
                _bounded_int(rank, label, minimum=1)
        if self.fulltext_rank is None and self.vector_rank is None:
            raise ValueError("Evidence requires at least one retrieval channel rank.")
        if self.observation_status is not None and self.observation_status not in _OBSERVATION_STATUSES:
            raise ValueError("Observation status is invalid.")
        if (self.observation_status is None) != (self.observed_at is None):
            raise ValueError("Observation status and time must be supplied together.")
        object.__setattr__(self, "published_at", _optional_utc(self.published_at, "Published at"))
        object.__setattr__(self, "captured_at", _required_utc(self.captured_at, "Captured at"))
        object.__setattr__(self, "observed_at", _optional_utc(self.observed_at, "Observed at"))


@dataclass(frozen=True)
class EvidenceSet:
    run_id: str
    request: RetrievalRequest
    status: str
    retrieval_mode: str
    degradation: Mapping[str, Any] | None
    error: Mapping[str, Any] | None
    persons: tuple[RetrievalPersonSnapshot, ...]
    hits: tuple[EvidenceHit, ...]
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    missing_person_ids: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        _required_string(self.run_id, "Retrieval run ID")
        if not isinstance(self.request, RetrievalRequest):
            raise TypeError("Evidence set request is invalid.")
        if self.status not in _RUN_STATUSES or self.retrieval_mode not in _RUN_MODES:
            raise ValueError("Evidence set state is invalid.")
        if not isinstance(self.persons, tuple) or not all(
            isinstance(item, RetrievalPersonSnapshot) for item in self.persons
        ):
            raise TypeError("Evidence set people are invalid.")
        if not isinstance(self.hits, tuple) or not all(
            isinstance(item, EvidenceHit) for item in self.hits
        ):
            raise TypeError("Evidence set hits are invalid.")
        object.__setattr__(self, "degradation", _freeze_json(self.degradation))
        object.__setattr__(self, "error", _freeze_json(self.error))
        object.__setattr__(self, "created_at", _required_utc(self.created_at, "Created at"))
        object.__setattr__(self, "started_at", _optional_utc(self.started_at, "Started at"))
        object.__setattr__(self, "completed_at", _optional_utc(self.completed_at, "Completed at"))
        object.__setattr__(
            self,
            "missing_person_ids",
            tuple(item.person_id for item in self.persons if item.generation_id is None),
        )


class RetrievalRepository:
    """Persist retrieval resources using only a caller-owned SQLite connection."""

    def create_run(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        request: RetrievalRequest,
        *,
        created_at: datetime,
    ) -> EvidenceSet:
        _required_string(run_id, "Retrieval run ID")
        if not isinstance(request, RetrievalRequest):
            raise TypeError("Retrieval request is invalid.")
        created = _serialize_utc(created_at, "Created at")
        snapshots: list[RetrievalPersonSnapshot] = []
        for ordinal, person_id in enumerate(request.person_ids):
            row = connection.execute(
                """
                SELECT p.person_id, g.generation_id, g.status, g.retrieval_mode
                FROM persons p
                LEFT JOIN person_index_heads h ON h.person_id = p.person_id
                LEFT JOIN index_generations g ON g.generation_id = h.generation_id
                WHERE p.person_id = ?
                """,
                (person_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Retrieval person does not exist.")
            snapshots.append(
                RetrievalPersonSnapshot(
                    person_id=person_id,
                    ordinal=ordinal,
                    generation_id=row["generation_id"],
                    generation_status="missing" if row["generation_id"] is None else row["status"],
                    retrieval_mode=(
                        row["retrieval_mode"]
                        if row["generation_id"] is not None
                        and row["status"] in {"ready", "degraded"}
                        else "none"
                    ),
                )
            )
        connection.execute(
            """
            INSERT INTO retrieval_runs(
                run_id, request_json, status, retrieval_mode, created_at
            ) VALUES (?, ?, 'pending', 'none', ?)
            """,
            (run_id, _request_json(request), created),
        )
        connection.executemany(
            """
            INSERT INTO retrieval_run_persons(
                run_id, person_id, ordinal, generation_id,
                generation_status, retrieval_mode
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    run_id,
                    item.person_id,
                    item.ordinal,
                    item.generation_id,
                    item.generation_status,
                    item.retrieval_mode,
                )
                for item in snapshots
            ),
        )
        return self.get_run(connection, run_id)

    def get_run(self, connection: sqlite3.Connection, run_id: str) -> EvidenceSet:
        _required_string(run_id, "Retrieval run ID")
        row = connection.execute(
            "SELECT * FROM retrieval_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise RetrievalRunNotFound("Retrieval run was not found.")
        persons = tuple(
            RetrievalPersonSnapshot(
                person_id=item["person_id"],
                ordinal=item["ordinal"],
                generation_id=item["generation_id"],
                generation_status=item["generation_status"],
                retrieval_mode=item["retrieval_mode"],
            )
            for item in connection.execute(
                "SELECT * FROM retrieval_run_persons WHERE run_id = ? ORDER BY ordinal",
                (run_id,),
            )
        )
        hits = tuple(
            _evidence_from_row(item)
            for item in connection.execute(
                "SELECT * FROM retrieval_evidence WHERE run_id = ? ORDER BY ordinal",
                (run_id,),
            )
        )
        return EvidenceSet(
            run_id=run_id,
            request=_request_from_json(row["request_json"]),
            status=row["status"],
            retrieval_mode=row["retrieval_mode"],
            degradation=_json_from_storage(row["degradation_json"]),
            error=_json_from_storage(row["error_json"]),
            persons=persons,
            hits=hits,
            created_at=_parse_utc(row["created_at"], "Created at"),
            started_at=_parse_optional_utc(row["started_at"], "Started at"),
            completed_at=_parse_optional_utc(row["completed_at"], "Completed at"),
        )

    def mark_running(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        started_at: datetime,
    ) -> EvidenceSet:
        started = _serialize_utc(started_at, "Started at")
        cursor = connection.execute(
            """
            UPDATE retrieval_runs SET status = 'running', started_at = ?
            WHERE run_id = ? AND status = 'pending'
            """,
            (started, run_id),
        )
        if cursor.rowcount != 1:
            self._raise_transition(connection, run_id)
        return self.get_run(connection, run_id)

    def complete(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        retrieval_mode: str,
        degradation: Mapping[str, Any],
        hits: tuple[EvidenceHit, ...],
        completed_at: datetime,
        person_modes: Mapping[str, str] | None = None,
    ) -> EvidenceSet:
        if retrieval_mode not in {"hybrid", "mixed", "fulltext_only"}:
            raise ValueError("Completed retrieval mode is invalid.")
        if not isinstance(hits, tuple) or not all(isinstance(hit, EvidenceHit) for hit in hits):
            raise TypeError("Completed evidence hits must be a tuple.")
        if tuple(hit.ordinal for hit in hits) != tuple(range(len(hits))):
            raise ValueError("Evidence ordinals must be contiguous and ordered.")
        completed = _serialize_utc(completed_at, "Completed at")
        degradation_json = _canonical_json(degradation, "Retrieval degradation")
        self._require_status(connection, run_id, {"running"})
        if person_modes is not None:
            snapshots = tuple(
                connection.execute(
                    "SELECT person_id FROM retrieval_run_persons WHERE run_id = ? ORDER BY ordinal",
                    (run_id,),
                )
            )
            expected_people = {row["person_id"] for row in snapshots}
            if set(person_modes) != expected_people or any(
                mode not in _PERSON_MODES for mode in person_modes.values()
            ):
                raise ValueError("Completed person retrieval modes are invalid.")
            for person_id, mode in person_modes.items():
                connection.execute(
                    """
                    UPDATE retrieval_run_persons SET retrieval_mode = ?
                    WHERE run_id = ? AND person_id = ?
                    """,
                    (mode, run_id, person_id),
                )
        connection.executemany(
            """
            INSERT INTO retrieval_evidence(
                evidence_id, run_id, ordinal, person_id, account_id, platform,
                post_id, revision_id, chunk_id, generation_id, canonical_url,
                published_at, captured_at, observation_status, observed_at,
                char_start, char_end, fulltext_rank, vector_rank, fused_rank, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    hit.evidence_id,
                    run_id,
                    hit.ordinal,
                    hit.person_id,
                    hit.account_id,
                    hit.platform,
                    hit.post_id,
                    hit.revision_id,
                    hit.chunk_id,
                    hit.generation_id,
                    hit.canonical_url,
                    _serialize_optional_utc(hit.published_at, "Published at"),
                    _serialize_utc(hit.captured_at, "Captured at"),
                    hit.observation_status,
                    _serialize_optional_utc(hit.observed_at, "Observed at"),
                    hit.char_start,
                    hit.char_end,
                    hit.fulltext_rank,
                    hit.vector_rank,
                    hit.fused_rank,
                    completed,
                )
                for hit in hits
            ),
        )
        cursor = connection.execute(
            """
            UPDATE retrieval_runs
            SET status = 'succeeded', retrieval_mode = ?, degradation_json = ?,
                error_json = NULL, completed_at = ?
            WHERE run_id = ? AND status = 'running'
            """,
            (retrieval_mode, degradation_json, completed, run_id),
        )
        if cursor.rowcount != 1:
            raise RetrievalStateError("Retrieval run state changed during completion.")
        return self.get_run(connection, run_id)

    def fail(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        error: Mapping[str, Any],
        completed_at: datetime,
    ) -> EvidenceSet:
        return self._terminal_error(
            connection, run_id, "failed", error, completed_at
        )

    def interrupt(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        error: Mapping[str, Any],
        completed_at: datetime,
    ) -> EvidenceSet:
        return self._terminal_error(
            connection, run_id, "interrupted", error, completed_at
        )

    def _terminal_error(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        status: str,
        error: Mapping[str, Any],
        completed_at: datetime,
    ) -> EvidenceSet:
        error_json = _canonical_json(error, "Retrieval error")
        completed = _serialize_utc(completed_at, "Completed at")
        cursor = connection.execute(
            """
            UPDATE retrieval_runs
            SET status = ?, retrieval_mode = 'none', error_json = ?, completed_at = ?
            WHERE run_id = ? AND status IN ('pending', 'running')
            """,
            (status, error_json, completed, run_id),
        )
        if cursor.rowcount != 1:
            self._raise_transition(connection, run_id)
        return self.get_run(connection, run_id)

    @staticmethod
    def _require_status(
        connection: sqlite3.Connection, run_id: str, allowed: set[str]
    ) -> None:
        row = connection.execute(
            "SELECT status FROM retrieval_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise RetrievalRunNotFound("Retrieval run was not found.")
        if row["status"] not in allowed:
            raise RetrievalStateError("Retrieval run state transition is invalid.")

    @staticmethod
    def _raise_transition(connection: sqlite3.Connection, run_id: str) -> None:
        if connection.execute(
            "SELECT 1 FROM retrieval_runs WHERE run_id = ?", (run_id,)
        ).fetchone() is None:
            raise RetrievalRunNotFound("Retrieval run was not found.")
        raise RetrievalStateError("Retrieval run state transition is invalid.")


def _evidence_from_row(row: sqlite3.Row) -> EvidenceHit:
    return EvidenceHit(
        evidence_id=row["evidence_id"],
        ordinal=row["ordinal"],
        person_id=row["person_id"],
        account_id=row["account_id"],
        platform=row["platform"],
        post_id=row["post_id"],
        revision_id=row["revision_id"],
        chunk_id=row["chunk_id"],
        generation_id=row["generation_id"],
        canonical_url=row["canonical_url"],
        published_at=_parse_optional_utc(row["published_at"], "Published at"),
        captured_at=_parse_utc(row["captured_at"], "Captured at"),
        observation_status=row["observation_status"],
        observed_at=_parse_optional_utc(row["observed_at"], "Observed at"),
        char_start=row["char_start"],
        char_end=row["char_end"],
        fulltext_rank=row["fulltext_rank"],
        vector_rank=row["vector_rank"],
        fused_rank=row["fused_rank"],
    )


def _request_json(request: RetrievalRequest) -> str:
    return json.dumps(
        {
            "limit": request.limit,
            "max_chunks_per_post": request.max_chunks_per_post,
            "min_hits_per_person": request.min_hits_per_person,
            "person_ids": list(request.person_ids),
            "platforms": list(request.platforms),
            "published_from": _serialize_optional_utc(request.published_from, "Published from"),
            "published_to": _serialize_optional_utc(request.published_to, "Published to"),
            "query": request.query,
            "revision_scope": request.revision_scope,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _request_from_json(value: str) -> RetrievalRequest:
    try:
        payload = json.loads(value)
        return RetrievalRequest(
            query=payload["query"],
            person_ids=tuple(payload["person_ids"]),
            platforms=tuple(payload["platforms"]),
            published_from=_parse_optional_utc(payload["published_from"], "Published from"),
            published_to=_parse_optional_utc(payload["published_to"], "Published to"),
            revision_scope=payload["revision_scope"],
            limit=payload["limit"],
            min_hits_per_person=payload["min_hits_per_person"],
            max_chunks_per_post=payload["max_chunks_per_post"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise RetrievalError("Stored retrieval request is invalid.") from None


def _canonical_json(value: Mapping[str, Any], label: str) -> str:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    frozen = _freeze_json(value)
    try:
        return json.dumps(
            _thaw_json(frozen),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ValueError(f"{label} must contain canonical JSON values.") from None


def _freeze_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and _ABSOLUTE_PATH.search(value):
            raise ValueError("Structured retrieval state contains unsafe details.")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Structured retrieval state must contain finite numbers.")
        return value
    if isinstance(value, Mapping):
        prepared: dict[str, Any] = {}
        for key in sorted(value):
            if not isinstance(key, str) or not key or _SENSITIVE_KEY.search(key):
                raise ValueError("Structured retrieval state contains unsafe keys.")
            prepared[key] = _freeze_json(value[key])
        return MappingProxyType(prepared)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise ValueError("Structured retrieval state must contain JSON values.")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _json_from_storage(value: str | None) -> Mapping[str, Any] | None:
    if value is None:
        return None
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise RetrievalError("Stored retrieval state is invalid.") from None
    if not isinstance(payload, dict):
        raise RetrievalError("Stored retrieval state is invalid.")
    return _freeze_json(payload)


def _deduplicate_strings(
    values: tuple[str, ...], label: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    if not values and not allow_empty:
        raise ValueError(f"{label} must not be empty.")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        _required_string(value, label)
        normalized = value.strip()
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required.")
    return value.strip()


def _bounded_int(
    value: Any, label: str, *, minimum: int, maximum: int | None = None
) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{label} is invalid.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} is invalid.")
    return value


def _required_utc(value: datetime, label: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"{label} must be an aware UTC datetime.")
    return value.astimezone(timezone.utc)


def _optional_utc(value: datetime | None, label: str) -> datetime | None:
    return None if value is None else _required_utc(value, label)


def _serialize_utc(value: datetime, label: str) -> str:
    return _required_utc(value, label).isoformat()


def _serialize_optional_utc(value: datetime | None, label: str) -> str | None:
    return None if value is None else _serialize_utc(value, label)


def _parse_utc(value: str, label: str) -> datetime:
    if not isinstance(value, str):
        raise RetrievalError(f"Stored {label.lower()} is invalid.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RetrievalError(f"Stored {label.lower()} is invalid.") from None
    try:
        return _required_utc(parsed, label)
    except ValueError:
        raise RetrievalError(f"Stored {label.lower()} is invalid.") from None


def _parse_optional_utc(value: str | None, label: str) -> datetime | None:
    return None if value is None else _parse_utc(value, label)
