from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Mapping


_PROVIDERS = {"codex_task", "openai_compatible"}
_STATUSES = {
    "pending_codex",
    "pending_provider",
    "running",
    "succeeded",
    "citation_invalid",
    "failed",
    "interrupted",
}
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|cookie|credential|local[_-]?path|password|secret|token)",
    re.IGNORECASE,
)
_ABSOLUTE_PATH = re.compile(
    r"(?:^[A-Za-z]:[\\/]|(?<![A-Za-z])[A-Za-z]:[\\/]|/(?:Users|home|tmp|var|etc)/)"
)


class QuestionError(Exception):
    """Base class for stable question-run contract failures."""


class QuestionRunNotFound(QuestionError):
    """The requested question run does not exist."""


class QuestionRunStateError(QuestionError):
    """The question run or its retrieval source is not ready."""


@dataclass(frozen=True)
class QuestionPerson:
    person_id: str
    ordinal: int
    display_name: str
    has_evidence: bool

    def __post_init__(self) -> None:
        _required_string(self.person_id, "Person ID")
        _required_string(self.display_name, "Display name")
        _nonnegative_int(self.ordinal, "Person ordinal")
        if not isinstance(self.has_evidence, bool):
            raise TypeError("Person evidence flag must be boolean.")


@dataclass(frozen=True)
class QuestionEvidence:
    evidence_id: str
    ordinal: int
    person_id: str
    account_id: str
    platform: str
    post_id: str
    revision_id: str
    chunk_id: str
    excerpt: str
    char_start: int
    char_end: int
    canonical_url: str | None
    published_at: datetime | None
    captured_at: datetime
    observation_status: str | None
    observed_at: datetime | None
    disposition_state: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.evidence_id, "Evidence ID"),
            (self.person_id, "Person ID"),
            (self.account_id, "Account ID"),
            (self.platform, "Platform"),
            (self.post_id, "Post ID"),
            (self.revision_id, "Revision ID"),
            (self.chunk_id, "Chunk ID"),
            (self.excerpt, "Evidence excerpt"),
        ):
            _required_string(value, label)
        if self.evidence_id != f"E{self.ordinal + 1}":
            raise ValueError("Evidence ID and ordinal are inconsistent.")
        _nonnegative_int(self.char_start, "Evidence start")
        if not isinstance(self.char_end, int) or self.char_end <= self.char_start:
            raise ValueError("Evidence offsets are invalid.")
        if self.canonical_url is not None:
            _required_string(self.canonical_url, "Canonical URL")
        if self.observation_status not in {None, "available", "deleted", "unavailable"}:
            raise ValueError("Observation status is invalid.")
        if (self.observation_status is None) != (self.observed_at is None):
            raise ValueError("Observation status and time must be supplied together.")
        if self.disposition_state not in {"active", "suppressed", "purged"}:
            raise ValueError("Disposition state is invalid.")
        object.__setattr__(self, "published_at", _optional_utc(self.published_at, "Published at"))
        object.__setattr__(self, "captured_at", _required_utc(self.captured_at, "Captured at"))
        object.__setattr__(self, "observed_at", _optional_utc(self.observed_at, "Observed at"))


@dataclass(frozen=True)
class EvidenceBundle:
    retrieval_run_id: str
    question: str
    filters: Mapping[str, Any]
    persons: tuple[QuestionPerson, ...]
    evidence: tuple[QuestionEvidence, ...]
    canonical_json: str = field(init=False)
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _required_string(self.retrieval_run_id, "Retrieval run ID")
        _required_string(self.question, "Question")
        if not isinstance(self.persons, tuple) or not all(
            isinstance(item, QuestionPerson) for item in self.persons
        ):
            raise TypeError("Evidence bundle people are invalid.")
        if not isinstance(self.evidence, tuple) or not all(
            isinstance(item, QuestionEvidence) for item in self.evidence
        ):
            raise TypeError("Evidence bundle evidence is invalid.")
        if tuple(item.ordinal for item in self.persons) != tuple(range(len(self.persons))):
            raise ValueError("Evidence bundle person order is invalid.")
        if tuple(item.ordinal for item in self.evidence) != tuple(range(len(self.evidence))):
            raise ValueError("Evidence bundle evidence order is invalid.")
        frozen_filters = _freeze_json(self.filters)
        object.__setattr__(self, "filters", frozen_filters)
        canonical = _bundle_json(self)
        object.__setattr__(self, "canonical_json", canonical)
        object.__setattr__(self, "sha256", hashlib.sha256(canonical.encode("utf-8")).hexdigest())


@dataclass(frozen=True)
class QuestionRun:
    run_id: str
    provider: str
    status: str
    bundle: EvidenceBundle
    candidate: Mapping[str, Any] | None
    result: Mapping[str, Any] | None
    error: Mapping[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    def __post_init__(self) -> None:
        _required_string(self.run_id, "Question run ID")
        if self.provider not in _PROVIDERS or self.status not in _STATUSES:
            raise ValueError("Question run state is invalid.")
        if not isinstance(self.bundle, EvidenceBundle):
            raise TypeError("Question evidence bundle is invalid.")
        object.__setattr__(self, "candidate", _freeze_json(self.candidate))
        object.__setattr__(self, "result", _freeze_json(self.result))
        object.__setattr__(self, "error", _freeze_json(self.error))
        object.__setattr__(self, "created_at", _required_utc(self.created_at, "Created at"))
        object.__setattr__(self, "started_at", _optional_utc(self.started_at, "Started at"))
        object.__setattr__(self, "completed_at", _optional_utc(self.completed_at, "Completed at"))

    @property
    def persons(self) -> tuple[QuestionPerson, ...]:
        return self.bundle.persons


class QuestionRepository:
    """Persist question runs using only a caller-owned SQLite connection."""

    def create(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        retrieval_run_id: str,
        *,
        provider: str,
        created_at: datetime,
    ) -> QuestionRun:
        _required_string(run_id, "Question run ID")
        _required_string(retrieval_run_id, "Retrieval run ID")
        if provider not in _PROVIDERS:
            raise ValueError("Question provider is invalid.")
        source = connection.execute(
            "SELECT request_json, status FROM retrieval_runs WHERE run_id = ?",
            (retrieval_run_id,),
        ).fetchone()
        if source is None or source["status"] != "succeeded":
            raise QuestionRunStateError("Retrieval run is not ready for answering.")
        request = _stored_object(source["request_json"], "retrieval request")
        try:
            question = _required_string(request["query"], "Question")
            filters = {
                "platforms": request["platforms"],
                "published_from": request["published_from"],
                "published_to": request["published_to"],
                "revision_scope": request["revision_scope"],
            }
        except (KeyError, TypeError, ValueError):
            raise QuestionError("Stored retrieval request is invalid.") from None
        person_rows = connection.execute(
            """
            SELECT rp.person_id, rp.ordinal, p.display_name,
                   EXISTS(
                       SELECT 1 FROM retrieval_evidence re
                       WHERE re.run_id = rp.run_id AND re.person_id = rp.person_id
                   ) AS has_evidence
            FROM retrieval_run_persons rp
            JOIN persons p ON p.person_id = rp.person_id
            WHERE rp.run_id = ? ORDER BY rp.ordinal
            """,
            (retrieval_run_id,),
        ).fetchall()
        evidence_rows = connection.execute(
            """
            SELECT re.*, revision.content_text, disposition.state AS disposition_state
            FROM retrieval_evidence re
            JOIN post_revisions revision ON revision.revision_id = re.revision_id
            JOIN content_dispositions disposition ON disposition.post_id = re.post_id
            WHERE re.run_id = ? ORDER BY re.ordinal
            """,
            (retrieval_run_id,),
        ).fetchall()
        persons = tuple(
            QuestionPerson(
                person_id=row["person_id"],
                ordinal=row["ordinal"],
                display_name=row["display_name"],
                has_evidence=bool(row["has_evidence"]),
            )
            for row in person_rows
        )
        evidence = tuple(
            QuestionEvidence(
                evidence_id=f"E{index + 1}",
                ordinal=index,
                person_id=row["person_id"],
                account_id=row["account_id"],
                platform=row["platform"],
                post_id=row["post_id"],
                revision_id=row["revision_id"],
                chunk_id=row["chunk_id"],
                excerpt=row["content_text"][row["char_start"] : row["char_end"]],
                char_start=row["char_start"],
                char_end=row["char_end"],
                canonical_url=row["canonical_url"],
                published_at=_parse_optional_utc(row["published_at"], "Published at"),
                captured_at=_parse_utc(row["captured_at"], "Captured at"),
                observation_status=row["observation_status"],
                observed_at=_parse_optional_utc(row["observed_at"], "Observed at"),
                disposition_state=row["disposition_state"],
            )
            for index, row in enumerate(evidence_rows)
        )
        bundle = EvidenceBundle(
            retrieval_run_id=retrieval_run_id,
            question=question,
            filters=filters,
            persons=persons,
            evidence=evidence,
        )
        created = _serialize_utc(created_at, "Created at")
        status = "pending_codex" if provider == "codex_task" else "pending_provider"
        connection.execute(
            """
            INSERT INTO question_runs(
                run_id, retrieval_run_id, provider, status,
                evidence_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, retrieval_run_id, provider, status, bundle.sha256, created),
        )
        connection.executemany(
            """
            INSERT INTO question_run_persons(
                run_id, person_id, ordinal, display_name, has_evidence
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                (run_id, item.person_id, item.ordinal, item.display_name, int(item.has_evidence))
                for item in persons
            ),
        )
        connection.executemany(
            """
            INSERT INTO question_evidence(
                run_id, evidence_id, ordinal, retrieval_run_id,
                retrieval_evidence_id, person_id, account_id, platform, post_id,
                revision_id, chunk_id, excerpt, char_start, char_end,
                canonical_url, published_at, captured_at, observation_status,
                observed_at, disposition_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    run_id,
                    item.evidence_id,
                    item.ordinal,
                    retrieval_run_id,
                    evidence_rows[item.ordinal]["evidence_id"],
                    item.person_id,
                    item.account_id,
                    item.platform,
                    item.post_id,
                    item.revision_id,
                    item.chunk_id,
                    item.excerpt,
                    item.char_start,
                    item.char_end,
                    item.canonical_url,
                    _serialize_optional_utc(item.published_at, "Published at"),
                    _serialize_utc(item.captured_at, "Captured at"),
                    item.observation_status,
                    _serialize_optional_utc(item.observed_at, "Observed at"),
                    item.disposition_state,
                )
                for item in evidence
            ),
        )
        return self.get(connection, run_id)

    def get(self, connection: sqlite3.Connection, run_id: str) -> QuestionRun:
        _required_string(run_id, "Question run ID")
        row = connection.execute(
            "SELECT * FROM question_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise QuestionRunNotFound("Question run was not found.")
        source = connection.execute(
            "SELECT request_json FROM retrieval_runs WHERE run_id = ?",
            (row["retrieval_run_id"],),
        ).fetchone()
        if source is None:
            raise QuestionError("Stored question source is invalid.")
        request = _stored_object(source["request_json"], "retrieval request")
        persons = tuple(
            QuestionPerson(
                person_id=item["person_id"],
                ordinal=item["ordinal"],
                display_name=item["display_name"],
                has_evidence=bool(item["has_evidence"]),
            )
            for item in connection.execute(
                "SELECT * FROM question_run_persons WHERE run_id = ? ORDER BY ordinal",
                (run_id,),
            )
        )
        evidence = tuple(
            _evidence_from_row(item)
            for item in connection.execute(
                "SELECT * FROM question_evidence WHERE run_id = ? ORDER BY ordinal",
                (run_id,),
            )
        )
        bundle = EvidenceBundle(
            retrieval_run_id=row["retrieval_run_id"],
            question=request["query"],
            filters={
                "platforms": request["platforms"],
                "published_from": request["published_from"],
                "published_to": request["published_to"],
                "revision_scope": request["revision_scope"],
            },
            persons=persons,
            evidence=evidence,
        )
        if bundle.sha256 != row["evidence_sha256"]:
            raise QuestionError("Stored evidence bundle hash is invalid.")
        return QuestionRun(
            run_id=run_id,
            provider=row["provider"],
            status=row["status"],
            bundle=bundle,
            candidate=_stored_optional_object(row["candidate_json"], "candidate answer"),
            result=_stored_optional_object(row["result_json"], "answer result"),
            error=_stored_optional_object(row["error_json"], "question error"),
            created_at=_parse_utc(row["created_at"], "Created at"),
            started_at=_parse_optional_utc(row["started_at"], "Started at"),
            completed_at=_parse_optional_utc(row["completed_at"], "Completed at"),
        )

    def mark_running(
        self, connection: sqlite3.Connection, run_id: str, *, started_at: datetime
    ) -> QuestionRun:
        cursor = connection.execute(
            """
            UPDATE question_runs SET status = 'running', started_at = ?
            WHERE run_id = ? AND status = 'pending_provider'
            """,
            (_serialize_utc(started_at, "Started at"), run_id),
        )
        if cursor.rowcount != 1:
            self._raise_transition(connection, run_id)
        return self.get(connection, run_id)

    def succeed(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        candidate: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        completed_at: datetime,
    ) -> QuestionRun:
        return self._complete(
            connection,
            run_id,
            "succeeded",
            candidate=candidate,
            result=result,
            error=None,
            completed_at=completed_at,
            allowed={"pending_codex", "running"},
        )

    def invalidate(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        candidate: Mapping[str, Any],
        error: Mapping[str, Any],
        *,
        completed_at: datetime,
    ) -> QuestionRun:
        return self._complete(
            connection,
            run_id,
            "citation_invalid",
            candidate=candidate,
            result=None,
            error=error,
            completed_at=completed_at,
            allowed={"pending_codex", "running"},
        )

    def fail(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        error: Mapping[str, Any],
        *,
        completed_at: datetime,
    ) -> QuestionRun:
        return self._complete(
            connection,
            run_id,
            "failed",
            candidate=None,
            result=None,
            error=error,
            completed_at=completed_at,
            allowed={"pending_provider", "running"},
        )

    def interrupt(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        error: Mapping[str, Any],
        *,
        completed_at: datetime,
    ) -> QuestionRun:
        return self._complete(
            connection,
            run_id,
            "interrupted",
            candidate=None,
            result=None,
            error=error,
            completed_at=completed_at,
            allowed={"running"},
        )

    def _complete(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        status: str,
        *,
        candidate: Mapping[str, Any] | None,
        result: Mapping[str, Any] | None,
        error: Mapping[str, Any] | None,
        completed_at: datetime,
        allowed: set[str],
    ) -> QuestionRun:
        placeholders = ",".join("?" for _ in allowed)
        cursor = connection.execute(
            f"""
            UPDATE question_runs
            SET status = ?, candidate_json = ?, result_json = ?, error_json = ?, completed_at = ?
            WHERE run_id = ? AND status IN ({placeholders})
            """,
            (
                status,
                _canonical_optional_object(candidate, "Candidate answer"),
                _canonical_optional_object(result, "Answer result"),
                _canonical_optional_object(error, "Question error"),
                _serialize_utc(completed_at, "Completed at"),
                run_id,
                *sorted(allowed),
            ),
        )
        if cursor.rowcount != 1:
            self._raise_transition(connection, run_id)
        return self.get(connection, run_id)

    @staticmethod
    def _raise_transition(connection: sqlite3.Connection, run_id: str) -> None:
        if connection.execute(
            "SELECT 1 FROM question_runs WHERE run_id = ?", (run_id,)
        ).fetchone() is None:
            raise QuestionRunNotFound("Question run was not found.")
        raise QuestionRunStateError("Question run state transition is invalid.")


def _evidence_from_row(row: sqlite3.Row) -> QuestionEvidence:
    return QuestionEvidence(
        evidence_id=row["evidence_id"],
        ordinal=row["ordinal"],
        person_id=row["person_id"],
        account_id=row["account_id"],
        platform=row["platform"],
        post_id=row["post_id"],
        revision_id=row["revision_id"],
        chunk_id=row["chunk_id"],
        excerpt=row["excerpt"],
        char_start=row["char_start"],
        char_end=row["char_end"],
        canonical_url=row["canonical_url"],
        published_at=_parse_optional_utc(row["published_at"], "Published at"),
        captured_at=_parse_utc(row["captured_at"], "Captured at"),
        observation_status=row["observation_status"],
        observed_at=_parse_optional_utc(row["observed_at"], "Observed at"),
        disposition_state=row["disposition_state"],
    )


def evidence_bundle_json(bundle: EvidenceBundle) -> dict[str, Any]:
    return json.loads(bundle.canonical_json)


def _bundle_json(bundle: EvidenceBundle) -> str:
    value = {
        "schema_version": 1,
        "retrieval_run_id": bundle.retrieval_run_id,
        "question": bundle.question,
        "filters": _thaw_json(bundle.filters),
        "persons": [
            {
                "person_id": person.person_id,
                "ordinal": person.ordinal,
                "display_name": person.display_name,
                "has_evidence": person.has_evidence,
            }
            for person in bundle.persons
        ],
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "ordinal": item.ordinal,
                "person_id": item.person_id,
                "account_id": item.account_id,
                "platform": item.platform,
                "post_id": item.post_id,
                "revision_id": item.revision_id,
                "chunk_id": item.chunk_id,
                "excerpt": item.excerpt,
                "char_start": item.char_start,
                "char_end": item.char_end,
                "canonical_url": item.canonical_url,
                "published_at": _serialize_optional_utc(item.published_at, "Published at"),
                "captured_at": _serialize_utc(item.captured_at, "Captured at"),
                "observation_status": item.observation_status,
                "observed_at": _serialize_optional_utc(item.observed_at, "Observed at"),
                "disposition_state": item.disposition_state,
            }
            for item in bundle.evidence
        ],
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _canonical_optional_object(value: Mapping[str, Any] | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    return json.dumps(
        _thaw_json(_freeze_json(value)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _stored_object(value: str, label: str) -> Mapping[str, Any]:
    result = _stored_optional_object(value, label)
    if result is None:
        raise QuestionError(f"Stored {label} is invalid.")
    return result


def _stored_optional_object(value: str | None, label: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise QuestionError(f"Stored {label} is invalid.") from None
    if not isinstance(payload, dict):
        raise QuestionError(f"Stored {label} is invalid.")
    return _freeze_json(payload)


def _freeze_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and _ABSOLUTE_PATH.search(value):
            raise ValueError("Structured question state contains unsafe details.")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Structured question state must contain finite numbers.")
        return value
    if isinstance(value, Mapping):
        prepared: dict[str, Any] = {}
        for key in sorted(value):
            if not isinstance(key, str) or not key or _SENSITIVE_KEY.search(key):
                raise ValueError("Structured question state contains unsafe keys.")
            prepared[key] = _freeze_json(value[key])
        return MappingProxyType(prepared)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise ValueError("Structured question state must contain JSON values.")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required.")
    return value.strip()


def _nonnegative_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} is invalid.")
    return value


def _required_utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
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
        raise QuestionError(f"Stored {label.lower()} is invalid.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _required_utc(parsed, label)
    except ValueError:
        raise QuestionError(f"Stored {label.lower()} is invalid.") from None


def _parse_optional_utc(value: str | None, label: str) -> datetime | None:
    return None if value is None else _parse_utc(value, label)
