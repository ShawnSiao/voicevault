from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Mapping

from .app_db import AppDatabase
from .coverage import serialize_utc


class PostArchiveError(Exception):
    """Base class for stable post-archive failures."""


class ArchiveIdentityConflict(PostArchiveError):
    """An existing post has different immutable identity metadata."""


class PurgedPostRejected(PostArchiveError):
    """A purged post tombstone rejected new archive data."""


class ObservationTargetNotFound(PostArchiveError):
    """A status-only observation did not identify an existing post."""


class EvidenceMetadataConflict(PostArchiveError):
    """Evidence identity was reused with inconsistent metadata."""


class ArchiveIntegrityError(PostArchiveError):
    """Stored archive state violates repository invariants."""


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_key: str
    evidence_id: str
    sha256: str
    media_type: str
    byte_size: int
    relative_path: str


@dataclass(frozen=True)
class ArchiveRecord:
    external_post_id: str
    published_at: datetime | None
    captured_at: datetime
    canonical_url: str | None
    content_text: str | None
    observation_status: str
    evidence_keys: tuple[str, ...]


@dataclass(frozen=True)
class ArchiveImportSummary:
    post_count: int
    revision_count: int
    duplicate_count: int
    observation_count: int


@dataclass(frozen=True)
class _PreparedRecord:
    record: ArchiveRecord
    published_at: str | None
    captured_at: str
    normalized_content: str | None
    content_hash: str | None


_OBSERVATION_STATUSES = frozenset({"available", "deleted", "unavailable"})
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def normalize_post_text(text: str) -> str:
    """Return the canonical voicevault-text-v1 representation of post text."""
    if not isinstance(text, str):
        raise PostArchiveError("Post content must be text.")
    normalized = unicodedata.normalize("NFC", text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise PostArchiveError("Post content must not be empty after normalization.")
    return normalized


def post_content_sha256(text: str) -> str:
    """Hash normalized post text as lowercase UTF-8 SHA-256."""
    normalized = normalize_post_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class ArchiveRepository:
    """Connection-aware writer for posts, revisions, observations, and evidence."""

    def __init__(self, database: AppDatabase) -> None:
        self.database = database

    def import_records(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        job_id: str,
        records: tuple[ArchiveRecord, ...],
        evidence_by_key: Mapping[str, EvidenceRecord],
        accepted_at: datetime,
    ) -> ArchiveImportSummary:
        """Import validated records without owning the caller's transaction."""
        accepted_timestamp = _serialize_datetime(accepted_at, "Accepted time")
        _require_text(account_id, "Account ID")
        _require_text(job_id, "Job ID")
        prepared = tuple(_prepare_record(record, evidence_by_key) for record in records)
        validated_evidence = _validate_evidence_mapping(evidence_by_key)
        self._require_job_account(connection, account_id=account_id, job_id=job_id)

        evidence_ids = {
            key: self._resolve_evidence(connection, evidence, accepted_timestamp)
            for key, evidence in validated_evidence
        }
        revision_count = 0
        duplicate_count = 0
        observation_count = 0

        for prepared_record in prepared:
            post_id = self._resolve_post(
                connection,
                account_id=account_id,
                prepared=prepared_record,
                accepted_at=accepted_timestamp,
            )
            observation_id, observation_created = self._resolve_observation(
                connection,
                post_id=post_id,
                status=prepared_record.record.observation_status,
                observed_at=prepared_record.captured_at,
                job_id=job_id,
            )
            if observation_created:
                observation_count += 1

            linked_evidence_ids = tuple(
                dict.fromkeys(evidence_ids[key] for key in prepared_record.record.evidence_keys)
            )
            if prepared_record.record.observation_status == "available":
                revision_id, revision_created = self._resolve_revision(
                    connection,
                    post_id=post_id,
                    prepared=prepared_record,
                    job_id=job_id,
                )
                if revision_created:
                    revision_count += 1
                else:
                    duplicate_count += 1
                for evidence_id in linked_evidence_ids:
                    self._link_evidence(
                        connection,
                        revision_id=revision_id,
                        observation_id=None,
                        evidence_id=evidence_id,
                        job_id=job_id,
                        relation_kind="content",
                        created_at=accepted_timestamp,
                    )
            else:
                for evidence_id in linked_evidence_ids:
                    self._link_evidence(
                        connection,
                        revision_id=None,
                        observation_id=observation_id,
                        evidence_id=evidence_id,
                        job_id=job_id,
                        relation_kind="status",
                        created_at=accepted_timestamp,
                    )

        return ArchiveImportSummary(
            post_count=len({item.record.external_post_id for item in prepared}),
            revision_count=revision_count,
            duplicate_count=duplicate_count,
            observation_count=observation_count,
        )

    @staticmethod
    def _require_job_account(
        connection: sqlite3.Connection, *, account_id: str, job_id: str
    ) -> None:
        job = connection.execute(
            "SELECT account_id FROM collection_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if job is None:
            raise ArchiveIntegrityError(f"Collection job does not exist: {job_id}")
        if job[0] != account_id:
            raise ArchiveIntegrityError("Collection job does not belong to the archive account.")

    @staticmethod
    def _resolve_post(
        connection: sqlite3.Connection,
        *,
        account_id: str,
        prepared: _PreparedRecord,
        accepted_at: str,
    ) -> str:
        record = prepared.record
        row = connection.execute(
            """
            SELECT post_id, published_at, canonical_url
            FROM posts
            WHERE account_id = ? AND external_post_id = ?
            """,
            (account_id, record.external_post_id),
        ).fetchone()
        if row is None:
            if record.observation_status != "available":
                raise ObservationTargetNotFound(
                    f"Status observation target does not exist: {record.external_post_id}"
                )
            post_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO posts(
                    post_id, account_id, external_post_id, published_at,
                    canonical_url, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    post_id,
                    account_id,
                    record.external_post_id,
                    prepared.published_at,
                    record.canonical_url,
                    accepted_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO content_dispositions(
                    post_id, state, reason, changed_at, purged_content_hash
                ) VALUES (?, 'active', NULL, ?, NULL)
                """,
                (post_id, accepted_at),
            )
            return post_id

        post_id = row[0]
        disposition = connection.execute(
            "SELECT state FROM content_dispositions WHERE post_id = ?", (post_id,)
        ).fetchone()
        if disposition is None:
            raise ArchiveIntegrityError("Existing post has no content disposition.")
        if disposition[0] == "purged":
            raise PurgedPostRejected(f"Post was purged: {record.external_post_id}")

        if prepared.published_at is not None and prepared.published_at != row[1]:
            raise ArchiveIdentityConflict(
                f"Published time conflicts for post: {record.external_post_id}"
            )
        if record.canonical_url is not None and record.canonical_url != row[2]:
            raise ArchiveIdentityConflict(
                f"Canonical URL conflicts for post: {record.external_post_id}"
            )
        return post_id

    @staticmethod
    def _resolve_revision(
        connection: sqlite3.Connection,
        *,
        post_id: str,
        prepared: _PreparedRecord,
        job_id: str,
    ) -> tuple[str, bool]:
        if prepared.content_hash is None or prepared.normalized_content is None:
            raise ArchiveIntegrityError("Available record is missing prepared content.")
        row = connection.execute(
            """
            SELECT revision_id, content_text
            FROM post_revisions
            WHERE post_id = ? AND content_hash = ?
            """,
            (post_id, prepared.content_hash),
        ).fetchone()
        if row is not None:
            if row[1] != prepared.normalized_content:
                raise ArchiveIntegrityError(
                    "Stored revision content does not match its normalized content hash."
                )
            return row[0], False

        revision_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO post_revisions(
                revision_id, post_id, content_hash, content_text,
                captured_at, first_seen_job_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                post_id,
                prepared.content_hash,
                prepared.normalized_content,
                prepared.captured_at,
                job_id,
            ),
        )
        return revision_id, True

    @staticmethod
    def _resolve_observation(
        connection: sqlite3.Connection,
        *,
        post_id: str,
        status: str,
        observed_at: str,
        job_id: str,
    ) -> tuple[str, bool]:
        row = connection.execute(
            """
            SELECT observation_id
            FROM post_observations
            WHERE source_job_id = ? AND post_id = ? AND status = ? AND observed_at = ?
            """,
            (job_id, post_id, status, observed_at),
        ).fetchone()
        if row is not None:
            return row[0], False
        observation_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO post_observations(
                observation_id, post_id, status, observed_at, source_job_id
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (observation_id, post_id, status, observed_at, job_id),
        )
        return observation_id, True

    @staticmethod
    def _resolve_evidence(
        connection: sqlite3.Connection,
        evidence: EvidenceRecord,
        created_at: str,
    ) -> str:
        row = connection.execute(
            """
            SELECT evidence_id, media_type, byte_size, local_path
            FROM capture_evidence
            WHERE sha256 = ?
            """,
            (evidence.sha256,),
        ).fetchone()
        if row is not None:
            if (row[1], row[2], row[3]) != (
                evidence.media_type,
                evidence.byte_size,
                evidence.relative_path,
            ):
                raise EvidenceMetadataConflict(
                    f"Evidence metadata conflicts for SHA-256: {evidence.sha256}"
                )
            return row[0]

        path_owner = connection.execute(
            "SELECT sha256 FROM capture_evidence WHERE local_path = ?",
            (evidence.relative_path,),
        ).fetchone()
        if path_owner is not None:
            raise EvidenceMetadataConflict("Evidence path is already owned by another digest.")
        id_owner = connection.execute(
            "SELECT sha256 FROM capture_evidence WHERE evidence_id = ?",
            (evidence.evidence_id,),
        ).fetchone()
        if id_owner is not None:
            raise EvidenceMetadataConflict("Evidence ID is already owned by another digest.")

        try:
            connection.execute(
                """
                INSERT INTO capture_evidence(
                    evidence_id, sha256, media_type, byte_size, local_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    evidence.sha256,
                    evidence.media_type,
                    evidence.byte_size,
                    evidence.relative_path,
                    created_at,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise EvidenceMetadataConflict("Evidence metadata could not be inserted.") from error
        return evidence.evidence_id

    @staticmethod
    def _link_evidence(
        connection: sqlite3.Connection,
        *,
        revision_id: str | None,
        observation_id: str | None,
        evidence_id: str,
        job_id: str,
        relation_kind: str,
        created_at: str,
    ) -> None:
        target_column = "revision_id" if revision_id is not None else "observation_id"
        target_id = revision_id if revision_id is not None else observation_id
        existing = connection.execute(
            f"""
            SELECT post_evidence_id
            FROM post_evidence
            WHERE job_id = ? AND {target_column} = ?
              AND evidence_id = ? AND relation_kind = ?
            """,
            (job_id, target_id, evidence_id, relation_kind),
        ).fetchone()
        if existing is not None:
            return
        connection.execute(
            """
            INSERT INTO post_evidence(
                post_evidence_id, revision_id, observation_id,
                evidence_id, job_id, relation_kind, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                revision_id,
                observation_id,
                evidence_id,
                job_id,
                relation_kind,
                created_at,
            ),
        )


def _prepare_record(
    record: ArchiveRecord, evidence_by_key: Mapping[str, EvidenceRecord]
) -> _PreparedRecord:
    if not isinstance(record, ArchiveRecord):
        raise PostArchiveError("Archive records must be ArchiveRecord values.")
    _require_text(record.external_post_id, "External post ID")
    if record.observation_status not in _OBSERVATION_STATUSES:
        raise PostArchiveError(f"Unknown observation status: {record.observation_status}")
    captured_at = _serialize_datetime(record.captured_at, "Captured time")
    published_at = (
        _serialize_datetime(record.published_at, "Published time")
        if record.published_at is not None
        else None
    )
    if not isinstance(record.evidence_keys, tuple):
        raise PostArchiveError("Evidence keys must be a tuple.")
    for key in record.evidence_keys:
        _require_text(key, "Evidence key")
        if key not in evidence_by_key:
            raise PostArchiveError(f"Archive record references unknown evidence key: {key}")

    if record.observation_status == "available":
        if published_at is None:
            raise PostArchiveError("Available posts require a published time.")
        _require_text(record.canonical_url, "Canonical URL")
        if record.content_text is None:
            raise PostArchiveError("Available posts require content text.")
        normalized_content = normalize_post_text(record.content_text)
        content_hash = hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()
    else:
        if record.content_text is not None:
            raise PostArchiveError("Status-only observations must not include content text.")
        if record.canonical_url is not None:
            _require_text(record.canonical_url, "Canonical URL")
        normalized_content = None
        content_hash = None

    return _PreparedRecord(
        record=record,
        published_at=published_at,
        captured_at=captured_at,
        normalized_content=normalized_content,
        content_hash=content_hash,
    )


def _validate_evidence_mapping(
    evidence_by_key: Mapping[str, EvidenceRecord],
) -> tuple[tuple[str, EvidenceRecord], ...]:
    if not isinstance(evidence_by_key, Mapping):
        raise PostArchiveError("Evidence metadata must be a mapping.")
    validated: list[tuple[str, EvidenceRecord]] = []
    seen_keys: set[str] = set()
    for key, evidence in evidence_by_key.items():
        _require_text(key, "Evidence key")
        if key in seen_keys:
            raise PostArchiveError(f"Duplicate evidence key: {key}")
        seen_keys.add(key)
        if not isinstance(evidence, EvidenceRecord):
            raise PostArchiveError("Evidence values must be EvidenceRecord values.")
        if evidence.evidence_key != key:
            raise PostArchiveError("Evidence mapping key does not match its record key.")
        _require_text(evidence.evidence_id, "Evidence ID")
        if not isinstance(evidence.sha256, str) or _SHA256_PATTERN.fullmatch(evidence.sha256) is None:
            raise PostArchiveError("Evidence SHA-256 must be 64 lowercase hexadecimal characters.")
        _require_text(evidence.media_type, "Evidence media type")
        if (
            not isinstance(evidence.byte_size, int)
            or isinstance(evidence.byte_size, bool)
            or evidence.byte_size < 0
        ):
            raise PostArchiveError("Evidence byte size must be a non-negative integer.")
        _validate_relative_path(evidence.relative_path)
        validated.append((key, evidence))
    return tuple(validated)


def _serialize_datetime(value: datetime, label: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PostArchiveError(f"{label} must include a timezone.")
    try:
        return serialize_utc(value)
    except (TypeError, ValueError) as error:
        raise PostArchiveError(f"{label} must be a valid timezone-aware datetime.") from error


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PostArchiveError(f"{label} is required.")
    return value


def _validate_relative_path(value: object) -> str:
    path_text = _require_text(value, "Evidence relative path")
    if "\\" in path_text or "\x00" in path_text:
        raise PostArchiveError("Evidence path must use canonical POSIX separators.")
    if path_text.startswith("/") or path_text.endswith("/"):
        raise PostArchiveError("Evidence path must be relative without a trailing separator.")
    windows_path = PureWindowsPath(path_text)
    if windows_path.root or windows_path.drive:
        raise PostArchiveError("Evidence path must be relative.")
    parts = path_text.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise PostArchiveError("Evidence path must contain canonical non-relative segments.")
    if PurePosixPath(*parts).as_posix() != path_text:
        raise PostArchiveError("Evidence path must be canonical POSIX relative form.")
    return path_text
