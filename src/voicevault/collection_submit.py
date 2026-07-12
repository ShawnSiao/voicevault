from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .app_db import AppDatabase
from .collection_jobs import CollectionJobNotFound
from .collection_results import (
    CollectionResultLoader,
    CollectionResultValidator,
    CollectionTargetSnapshot,
    ResultSegmentTarget,
    ValidatedCheckpoint,
    ValidatedCollectionResult,
)
from .coverage import UtcInterval, insert_validated_complete, parse_utc, serialize_utc
from .evidence_store import EvidenceStore, StoredEvidence
from .post_archive import ArchiveRepository, EvidenceRecord


_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class CollectionSubmissionError(Exception):
    """Base class for stable collection-submission failures."""


class CollectionSubmitLeaseRejected(CollectionSubmissionError):
    """The envelope no longer owns the active collection lease."""


class CollectionSubmitLeaseExpired(CollectionSubmissionError):
    """The submitted lease expired and the job was converged."""


class CollectionSubmitConflict(CollectionSubmissionError):
    """A handoff already accepted a different submission."""


class CollectionCancelPending(CollectionSubmissionError):
    """A complete result cannot override a pending cancellation."""


@dataclass(frozen=True)
class CollectionSubmissionResult:
    submission_id: str
    manifest_sha256: str
    replayed: bool
    post_count: int
    revision_count: int
    duplicate_count: int
    observation_count: int
    evidence_count: int
    coverage_written: int
    job_status: str

    @property
    def receipt(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "submission_id": self.submission_id,
                "manifest_sha256": self.manifest_sha256,
                "post_count": self.post_count,
                "revision_count": self.revision_count,
                "duplicate_count": self.duplicate_count,
                "observation_count": self.observation_count,
                "evidence_count": self.evidence_count,
                "coverage_written": self.coverage_written,
            }
        )


class CollectionSubmissionService:
    """Validate staged collector output and atomically import accepted business state."""

    def __init__(
        self,
        database: AppDatabase,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def submit(
        self,
        job_id: str,
        *,
        collector_id: str,
        handoff_version: int,
        manifest_sha256: str,
    ) -> CollectionSubmissionResult:
        normalized_collector = _require_collector(collector_id)
        if (
            not isinstance(handoff_version, int)
            or isinstance(handoff_version, bool)
            or handoff_version < 1
        ):
            raise ValueError("Handoff version must be a positive integer.")
        if (
            not isinstance(manifest_sha256, str)
            or _SHA256_PATTERN.fullmatch(manifest_sha256) is None
        ):
            raise ValueError("Manifest SHA-256 must be 64 lowercase hexadecimal characters.")

        with self.database.transaction(immediate=True) as connection:
            replay = self._find_replay(
                connection,
                job_id=job_id,
                collector_id=normalized_collector,
                handoff_version=handoff_version,
                manifest_sha256=manifest_sha256,
            )
        if replay is not None:
            return replay

        now = _as_utc(self.clock())
        snapshot = self._read_snapshot(
            job_id, now=now, submitted_collector=normalized_collector
        )
        staged = CollectionResultLoader(self.database.path.parent).load(
            job_id, expected_manifest_sha256=manifest_sha256
        )
        validated = CollectionResultValidator().validate(staged, snapshot=snapshot)
        stored_evidence = EvidenceStore(self.database.path.parent).preserve(
            validated.artifacts
        )

        lease_expired = False
        result: CollectionSubmissionResult | None = None
        with self.database.transaction(immediate=True) as connection:
            replay = self._find_replay(
                connection,
                job_id=job_id,
                collector_id=normalized_collector,
                handoff_version=handoff_version,
                manifest_sha256=manifest_sha256,
                submission_id=validated.submission_id,
            )
            if replay is not None:
                result = replay
            else:
                job = connection.execute(
                    """
                    SELECT status, account_id, mode, handoff_version, collector_id,
                           lease_expires_at, cancel_requested_at
                    FROM collection_jobs WHERE job_id = ?
                    """,
                    (job_id,),
                ).fetchone()
                if job is None:
                    raise CollectionJobNotFound(job_id)
                handoff = connection.execute(
                    """
                    SELECT collector_id, claimed_at, revoked_at
                    FROM collection_handoffs
                    WHERE job_id = ? AND version = ?
                    """,
                    (job_id, handoff_version),
                ).fetchone()
                if not _lease_identity_matches(
                    job,
                    handoff,
                    collector_id=normalized_collector,
                    handoff_version=handoff_version,
                ):
                    raise CollectionSubmitLeaseRejected(
                        "The collection submission lease is no longer valid."
                    )

                now_text = serialize_utc(now)
                if job["lease_expires_at"] <= now_text:
                    terminal_status = (
                        "cancelled" if job["cancel_requested_at"] is not None else "interrupted"
                    )
                    connection.execute(
                        """
                        UPDATE collection_jobs
                        SET status = ?, collector_id = NULL, lease_expires_at = NULL,
                            updated_at = ?
                        WHERE job_id = ?
                          AND collector_id = ?
                          AND handoff_version = ?
                          AND status IN ('claimed', 'running')
                          AND lease_expires_at <= ?
                        """,
                        (
                            terminal_status,
                            now_text,
                            job_id,
                            normalized_collector,
                            handoff_version,
                            now_text,
                        ),
                    )
                    lease_expired = True
                else:
                    if (
                        validated.outcome_kind == "complete"
                        and job["cancel_requested_at"] is not None
                    ):
                        raise CollectionCancelPending(
                            "Collection cancellation is pending; complete results are rejected."
                        )
                    result = self._import_validated(
                        connection,
                        job_id=job_id,
                        collector_id=normalized_collector,
                        handoff_version=handoff_version,
                        account_id=job["account_id"],
                        mode=job["mode"],
                        cancel_requested=job["cancel_requested_at"] is not None,
                        validated=validated,
                        stored_evidence=stored_evidence,
                        accepted_at=now,
                    )

        if lease_expired:
            raise CollectionSubmitLeaseExpired(
                "The collection submission lease expired and the job was reconciled."
            )
        if result is None:
            raise RuntimeError("Collection submission did not produce a result.")
        return result

    @staticmethod
    def _find_replay(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        collector_id: str,
        handoff_version: int,
        manifest_sha256: str,
        submission_id: str | None = None,
    ) -> CollectionSubmissionResult | None:
        replay = connection.execute(
            """
            SELECT s.submission_id, s.collector_id, s.manifest_sha256,
                   s.receipt_json, j.status AS job_status
            FROM collection_submissions s
            JOIN collection_jobs j ON j.job_id = s.job_id
            WHERE s.job_id = ? AND s.handoff_version = ?
            """,
            (job_id, handoff_version),
        ).fetchone()
        if replay is None:
            return None
        if (
            replay["collector_id"] != collector_id
            or replay["manifest_sha256"] != manifest_sha256
            or (
                submission_id is not None
                and replay["submission_id"] != submission_id
            )
        ):
            raise CollectionSubmitConflict(
                "This collection handoff already accepted a different submission."
            )
        return _result_from_receipt(
            replay["receipt_json"], replayed=True, job_status=replay["job_status"]
        )

    def _read_snapshot(
        self, job_id: str, *, now: datetime, submitted_collector: str
    ) -> CollectionTargetSnapshot:
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            job = connection.execute(
                """
                SELECT j.*, a.person_id, a.platform, a.external_user_id,
                       h.collector_id AS handoff_collector_id
                FROM collection_jobs j
                JOIN platform_accounts a ON a.account_id = j.account_id
                LEFT JOIN collection_handoffs h
                  ON h.job_id = j.job_id AND h.version = j.handoff_version
                WHERE j.job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if job is None:
                raise CollectionJobNotFound(job_id)
            segments = connection.execute(
                """
                SELECT segment_id, ordinal, start_at, end_at
                FROM collection_segments WHERE job_id = ? ORDER BY ordinal
                """,
                (job_id,),
            ).fetchall()
            known_ids = connection.execute(
                "SELECT external_post_id FROM posts WHERE account_id = ?",
                (job["account_id"],),
            ).fetchall()

        collector = (
            job["handoff_collector_id"] or job["collector_id"] or submitted_collector
        )
        if not isinstance(collector, str) or not collector.strip():
            collector = "unclaimed"
        checkpoint = _decode_mapping(job["checkpoint_json"])
        resume_checkpoint_id = None
        if checkpoint is not None:
            for key in ("checkpoint_id", "last_checkpoint_id"):
                value = checkpoint.get(key)
                if isinstance(value, str) and value.strip():
                    resume_checkpoint_id = value
                    break
        return CollectionTargetSnapshot(
            job_id=job_id,
            handoff_version=job["handoff_version"],
            collector_id=collector,
            person_id=job["person_id"],
            account_id=job["account_id"],
            platform=job["platform"],
            external_user_id=job["external_user_id"],
            mode=job["mode"],
            requested_interval=UtcInterval(
                parse_utc(job["requested_start_at"]), parse_utc(job["requested_end_at"])
            ),
            segments=tuple(
                ResultSegmentTarget(
                    segment_id=row["segment_id"],
                    ordinal=row["ordinal"],
                    interval=UtcInterval(parse_utc(row["start_at"]), parse_utc(row["end_at"])),
                )
                for row in segments
            ),
            last_heartbeat_at=(
                parse_utc(job["last_heartbeat_at"])
                if job["last_heartbeat_at"] is not None
                else None
            ),
            stored_remote_action_count=job["remote_action_count"],
            known_external_post_ids=frozenset(row[0] for row in known_ids),
            resume_checkpoint_id=resume_checkpoint_id,
            now=now,
        )

    def _import_validated(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        collector_id: str,
        handoff_version: int,
        account_id: str,
        mode: str,
        cancel_requested: bool,
        validated: ValidatedCollectionResult,
        stored_evidence: Mapping[str, StoredEvidence],
        accepted_at: datetime,
    ) -> CollectionSubmissionResult:
        evidence_by_key = {
            key: EvidenceRecord(
                evidence_key=key,
                evidence_id=str(uuid.uuid4()),
                sha256=evidence.sha256,
                media_type=evidence.media_type,
                byte_size=evidence.byte_size,
                relative_path=evidence.relative_path,
            )
            for key, evidence in stored_evidence.items()
        }
        summary = ArchiveRepository(self.database).import_records(
            connection,
            account_id=account_id,
            job_id=job_id,
            records=validated.archive_records,
            evidence_by_key=evidence_by_key,
            accepted_at=accepted_at,
        )
        accepted_at_text = serialize_utc(accepted_at)
        self._insert_checkpoints(
            connection,
            job_id=job_id,
            checkpoints=validated.checkpoints,
            created_at=accepted_at_text,
        )
        self._link_job_evidence(
            connection,
            job_id=job_id,
            validated=validated,
            created_at=accepted_at_text,
        )

        coverage_written = 0
        if mode == "normal" and validated.outcome_kind == "complete":
            for interval in validated.coverage_intervals:
                coverage_written += int(
                    insert_validated_complete(
                        connection,
                        account_id=account_id,
                        interval=interval,
                        job_id=job_id,
                        recorded_at=accepted_at,
                    )
                )

        job_status = (
            "succeeded"
            if validated.outcome_kind == "complete"
            else ("cancelled" if cancel_requested else "partial")
        )
        evidence_count = len({item.sha256 for item in stored_evidence.values()})
        result = CollectionSubmissionResult(
            submission_id=validated.submission_id,
            manifest_sha256=validated.manifest_sha256,
            replayed=False,
            post_count=summary.post_count,
            revision_count=summary.revision_count,
            duplicate_count=summary.duplicate_count,
            observation_count=summary.observation_count,
            evidence_count=evidence_count,
            coverage_written=coverage_written,
            job_status=job_status,
        )
        receipt_json = _canonical_json(dict(result.receipt))
        connection.execute(
            """
            INSERT INTO collection_submissions(
                submission_id, job_id, handoff_version, collector_id,
                manifest_sha256, accepted_manifest_json, receipt_json,
                outcome_kind, accepted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                validated.submission_id,
                job_id,
                handoff_version,
                collector_id,
                validated.manifest_sha256,
                _canonical_json(_jsonable(validated.accepted_manifest)),
                receipt_json,
                validated.outcome_kind,
                accepted_at_text,
            ),
        )
        updated = connection.execute(
            """
            UPDATE collection_jobs
            SET status = ?, outcome = ?, remote_action_count = ?,
                submitted_at = ?, result_manifest_sha256 = ?,
                collector_id = NULL, lease_expires_at = NULL,
                error_code = NULL, error_json = NULL, updated_at = ?
            WHERE job_id = ?
              AND collector_id = ?
              AND handoff_version = ?
              AND status IN ('claimed', 'running')
              AND lease_expires_at > ?
            """,
            (
                job_status,
                validated.outcome_kind,
                validated.remote_action_count,
                accepted_at_text,
                validated.manifest_sha256,
                accepted_at_text,
                job_id,
                collector_id,
                handoff_version,
                accepted_at_text,
            ),
        )
        if updated.rowcount != 1:
            raise CollectionSubmitLeaseRejected(
                "The collection submission lease changed during import."
            )
        return result

    @staticmethod
    def _insert_checkpoints(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        checkpoints: tuple[ValidatedCheckpoint, ...],
        created_at: str,
    ) -> None:
        segment_offsets = {
            segment_id: int(row["next_sequence"])
            for segment_id in {checkpoint.segment_id for checkpoint in checkpoints}
            for row in connection.execute(
                """
                SELECT COALESCE(MAX(sequence) + 1, 0) AS next_sequence
                FROM collection_checkpoints
                WHERE job_id = ? AND segment_id = ?
                """,
                (job_id, segment_id),
            )
        }
        for checkpoint in checkpoints:
            canonical = _checkpoint_json(checkpoint)
            connection.execute(
                """
                INSERT INTO collection_checkpoints(
                    checkpoint_id, job_id, segment_id, sequence, observed_at,
                    action_type, triggered_remote_load, remote_action_ordinal,
                    visible_post_ids_json, earliest_non_pinned_at,
                    latest_non_pinned_at, anchor_post_id, start_kind,
                    completion_reason, boundary_post_id, canonical_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.checkpoint_id,
                    job_id,
                    checkpoint.segment_id,
                    segment_offsets[checkpoint.segment_id] + checkpoint.sequence,
                    serialize_utc(checkpoint.observed_at),
                    checkpoint.action_type,
                    int(checkpoint.triggered_remote_load),
                    checkpoint.remote_action_ordinal,
                    _canonical_json(list(checkpoint.visible_post_ids)),
                    _optional_time(checkpoint.earliest_non_pinned_at),
                    _optional_time(checkpoint.latest_non_pinned_at),
                    checkpoint.anchor_post_id,
                    checkpoint.start_kind,
                    checkpoint.completion_reason,
                    checkpoint.boundary_post_id,
                    _canonical_json(canonical),
                    created_at,
                ),
            )

    @staticmethod
    def _link_job_evidence(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        validated: ValidatedCollectionResult,
        created_at: str,
    ) -> None:
        for artifact in validated.artifacts:
            evidence = connection.execute(
                "SELECT evidence_id FROM capture_evidence WHERE sha256 = ?",
                (artifact.sha256,),
            ).fetchone()
            if evidence is None:
                raise sqlite3.IntegrityError("Validated evidence metadata is unavailable.")
            connection.execute(
                """
                INSERT INTO collection_job_evidence(
                    job_evidence_id, job_id, segment_id, evidence_id,
                    evidence_role, checkpoint_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    job_id,
                    artifact.segment_id,
                    evidence["evidence_id"],
                    artifact.purpose,
                    artifact.evidence_key,
                    created_at,
                ),
            )


def _lease_identity_matches(
    job: sqlite3.Row,
    handoff: sqlite3.Row | None,
    *,
    collector_id: str,
    handoff_version: int,
) -> bool:
    return bool(
        job["status"] in {"claimed", "running"}
        and job["handoff_version"] == handoff_version
        and job["collector_id"] == collector_id
        and job["lease_expires_at"] is not None
        and handoff is not None
        and handoff["collector_id"] == collector_id
        and handoff["claimed_at"] is not None
        and handoff["revoked_at"] is None
    )


def _result_from_receipt(
    receipt_json: str, *, replayed: bool, job_status: str
) -> CollectionSubmissionResult:
    receipt = json.loads(receipt_json)
    if not isinstance(receipt, dict):
        raise sqlite3.DatabaseError("Stored collection submission receipt is invalid.")
    return CollectionSubmissionResult(
        submission_id=receipt["submission_id"],
        manifest_sha256=receipt["manifest_sha256"],
        replayed=replayed,
        post_count=receipt["post_count"],
        revision_count=receipt["revision_count"],
        duplicate_count=receipt["duplicate_count"],
        observation_count=receipt["observation_count"],
        evidence_count=receipt["evidence_count"],
        coverage_written=receipt["coverage_written"],
        job_status=job_status,
    )


def _checkpoint_json(checkpoint: ValidatedCheckpoint) -> dict[str, Any]:
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "segment_id": checkpoint.segment_id,
        "sequence": checkpoint.sequence,
        "observed_at": serialize_utc(checkpoint.observed_at),
        "action_type": checkpoint.action_type,
        "triggered_remote_load": checkpoint.triggered_remote_load,
        "remote_action_ordinal": checkpoint.remote_action_ordinal,
        "visible_post_ids": list(checkpoint.visible_post_ids),
        "earliest_non_pinned_at": _optional_time(checkpoint.earliest_non_pinned_at),
        "latest_non_pinned_at": _optional_time(checkpoint.latest_non_pinned_at),
        "anchor_post_id": checkpoint.anchor_post_id,
        "start_kind": checkpoint.start_kind,
        "completion_reason": checkpoint.completion_reason,
        "boundary_post_id": checkpoint.boundary_post_id,
        "reached_end": checkpoint.reached_end,
        "evidence_keys": list(checkpoint.evidence_keys),
    }


def _optional_time(value: datetime | None) -> str | None:
    return serialize_utc(value) if value is not None else None


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_mapping(value: str | None) -> Mapping[str, Any] | None:
    if value is None:
        return None
    decoded = json.loads(value)
    return decoded if isinstance(decoded, dict) else None


def _require_collector(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Collector ID is required.")
    return value.strip()


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Clock must return a timezone-aware datetime.")
    return value.astimezone(timezone.utc)
