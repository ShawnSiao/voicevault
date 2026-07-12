from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from .app_db import AppDatabase
from .coverage import CoverageRepository, UtcInterval, parse_utc
from .person_archive import AccountNotFound, PlatformAccountRepository


TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
RECOVERABLE_STATUSES = frozenset(
    {"waiting_for_human", "rate_limited", "partial", "interrupted"}
)
COLLECTOR_SEGMENT_STATUSES = frozenset({"pending", "running"})
SEGMENT_STATUS_TRANSITIONS = {
    "pending": frozenset({"pending", "running"}),
    "running": frozenset({"running"}),
}


class CollectionDomainError(Exception):
    """Base class for stable collection-job failures."""


class CollectionAccountNotFound(CollectionDomainError):
    pass


class CollectionAccountUnconfirmed(CollectionDomainError):
    pass


class ActiveCollectionJobExists(CollectionDomainError):
    pass


class CollectionJobNotFound(CollectionDomainError):
    pass


class InvalidCollectionMode(CollectionDomainError):
    pass


class InvalidCollectionTransition(CollectionDomainError):
    pass


class InvalidSegmentProgress(CollectionDomainError):
    pass


class HandoffRejected(CollectionDomainError):
    pass


class LeaseRejected(CollectionDomainError):
    pass


@dataclass(frozen=True)
class CollectionSegment:
    segment_id: str
    ordinal: int
    interval: UtcInterval
    status: str
    checkpoint: Mapping[str, Any] | None
    progress: Mapping[str, Any] | None


@dataclass(frozen=True)
class CollectionHandoff:
    handoff_id: str
    version: int
    instance_id: str
    expires_at: datetime
    claimed_at: datetime | None
    revoked_at: datetime | None
    collector_id: str | None


@dataclass(frozen=True)
class CollectionJob:
    job_id: str
    account_id: str
    mode: str
    status: str
    requested_interval: UtcInterval
    outcome: str | None
    remote_action_count: int
    handoff_version: int
    collector_id: str | None
    lease_expires_at: datetime | None
    last_heartbeat_at: datetime | None
    cancel_requested_at: datetime | None
    checkpoint: Mapping[str, Any] | None
    error: Mapping[str, Any] | None
    created_at: datetime
    updated_at: datetime
    segments: tuple[CollectionSegment, ...]
    handoffs: tuple[CollectionHandoff, ...]


@dataclass(frozen=True)
class ClaimResult:
    job: CollectionJob
    manifest: Mapping[str, Any]
    lease_expires_at: datetime


@dataclass(frozen=True)
class HeartbeatResult:
    job: CollectionJob
    cancel_requested: bool


class CollectionService:
    def __init__(
        self,
        database: AppDatabase,
        *,
        instance_id: str,
        clock: Callable[[], datetime],
        handoff_ttl: timedelta,
        lease_ttl: timedelta,
    ) -> None:
        if not instance_id.strip():
            raise ValueError("Instance ID is required.")
        if handoff_ttl <= timedelta(0) or lease_ttl <= timedelta(0):
            raise ValueError("Handoff and lease TTLs must be positive.")
        self.database = database
        self.instance_id = instance_id
        self.clock = clock
        self.handoff_ttl = handoff_ttl
        self.lease_ttl = lease_ttl

    def create_job(self, account_id: str, requested: UtcInterval, *, mode: str) -> CollectionJob:
        if mode not in {"normal", "recheck"}:
            raise InvalidCollectionMode(mode)
        try:
            account_record = PlatformAccountRepository(self.database).get(account_id)
        except AccountNotFound:
            raise CollectionAccountNotFound(account_id) from None
        if not account_record.can_collect:
            raise CollectionAccountUnconfirmed(account_id)
        now = self._now()
        if mode == "normal":
            segments = CoverageRepository(self.database).missing(account_id, requested)
        else:
            segments = [requested]
        job_id = str(uuid.uuid4())
        status = "pending_codex" if segments else "succeeded"
        outcome = None if segments else "no_remote_action"
        try:
            with self.database.transaction() as connection:
                account = connection.execute(
                    """
                    SELECT a.account_id, a.archive_basis_confirmed_at
                    FROM platform_accounts a
                    JOIN persons p ON p.person_id = a.person_id
                    WHERE a.account_id = ?
                    """,
                    (account_id,),
                ).fetchone()
                if account is None:
                    raise CollectionAccountNotFound(account_id)
                if account["archive_basis_confirmed_at"] is None:
                    raise CollectionAccountUnconfirmed(account_id)
                connection.execute(
                    """
                    INSERT INTO collection_jobs(
                        job_id, account_id, mode, status, requested_start_at, requested_end_at,
                        outcome, remote_action_count, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        job_id,
                        account_id,
                        mode,
                        status,
                        _serialize_time(requested.start_at),
                        _serialize_time(requested.end_at),
                        outcome,
                        _serialize_time(now),
                        _serialize_time(now),
                    ),
                )
                for ordinal, interval in enumerate(segments):
                    connection.execute(
                        """
                        INSERT INTO collection_segments(
                            segment_id, job_id, ordinal, start_at, end_at, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            job_id,
                            ordinal,
                            _serialize_time(interval.start_at),
                            _serialize_time(interval.end_at),
                            _serialize_time(now),
                            _serialize_time(now),
                        ),
                    )
                if segments:
                    self._issue_handoff(connection, job_id, 1, now)
        except sqlite3.IntegrityError as error:
            if "collection_jobs.account_id" in str(error):
                raise ActiveCollectionJobExists(account_id) from None
            raise
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> CollectionJob:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM collection_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise CollectionJobNotFound(job_id)
            segment_rows = connection.execute(
                "SELECT * FROM collection_segments WHERE job_id = ? ORDER BY ordinal", (job_id,)
            ).fetchall()
            handoff_rows = connection.execute(
                "SELECT * FROM collection_handoffs WHERE job_id = ? ORDER BY version", (job_id,)
            ).fetchall()
        return _job_from_rows(row, segment_rows, handoff_rows)

    def list_jobs(
        self,
        *,
        account_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[CollectionJob]:
        if limit < 1 or limit > 100:
            raise ValueError("Collection job limit must be between 1 and 100.")
        clauses: list[str] = []
        parameters: list[Any] = []
        if account_id is not None:
            clauses.append("account_id = ?")
            parameters.append(account_id)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT job_id FROM collection_jobs{where} ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (*parameters, limit),
            ).fetchall()
        return [self.get_job(row["job_id"]) for row in rows]

    def claim(self, handoff_id: str, collector_id: str) -> ClaimResult:
        normalized_collector = collector_id.strip()
        if not normalized_collector:
            raise ValueError("Collector ID is required.")
        now = self._now()
        lease_expires_at = now + self.lease_ttl
        now_text = _serialize_time(now)
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE collection_handoffs
                SET claimed_at = ?, collector_id = ?
                WHERE handoff_id = ?
                  AND instance_id = ?
                  AND claimed_at IS NULL
                  AND revoked_at IS NULL
                  AND expires_at > ?
                  AND EXISTS (
                      SELECT 1 FROM collection_jobs j
                      WHERE j.job_id = collection_handoffs.job_id
                        AND j.status = 'pending_codex'
                        AND j.cancel_requested_at IS NULL
                  )
                """,
                (now_text, normalized_collector, handoff_id, self.instance_id, now_text),
            )
            if updated.rowcount != 1:
                raise HandoffRejected(handoff_id)
            handoff = connection.execute(
                "SELECT job_id FROM collection_handoffs WHERE handoff_id = ?", (handoff_id,)
            ).fetchone()
            job_id = handoff["job_id"]
            claimed = connection.execute(
                """
                UPDATE collection_jobs
                SET status = 'claimed', collector_id = ?, lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'pending_codex' AND cancel_requested_at IS NULL
                """,
                (normalized_collector, _serialize_time(lease_expires_at), now_text, job_id),
            )
            if claimed.rowcount != 1:
                raise HandoffRejected(handoff_id)
            account = connection.execute(
                """
                SELECT a.account_id, a.platform, a.external_user_id, j.mode
                FROM collection_jobs j
                JOIN platform_accounts a ON a.account_id = j.account_id
                WHERE j.job_id = ?
                """,
                (job_id,),
            ).fetchone()
            segments = connection.execute(
                "SELECT segment_id, ordinal, start_at, end_at FROM collection_segments WHERE job_id = ? ORDER BY ordinal",
                (job_id,),
            ).fetchall()
        job = self.get_job(job_id)
        manifest = {
            "job_id": job_id,
            "account": {
                "account_id": account["account_id"],
                "platform": account["platform"],
                "external_user_id": account["external_user_id"],
            },
            "mode": account["mode"],
            "body_capture_policy": {
                "available_post": "保存完整正文；不得把时间线卡片的折叠预览作为正文提交。",
                "expand_control": "发现「展开」或省略号预览时，点击并等待正文完整渲染后再读取。",
                "record_state": "未展开但完整可见记为 full；点击展开后记为 expanded。",
            },
            "segments": [
                {
                    "segment_id": segment["segment_id"],
                    "ordinal": segment["ordinal"],
                    "start_at": segment["start_at"],
                    "end_at": segment["end_at"],
                }
                for segment in segments
            ],
            "lease": {
                "collector_id": normalized_collector,
                "expires_at": _serialize_time(lease_expires_at),
            },
        }
        return ClaimResult(job=job, manifest=manifest, lease_expires_at=lease_expires_at)

    def heartbeat(
        self,
        job_id: str,
        collector_id: str,
        *,
        checkpoint: Mapping[str, Any] | None = None,
        segment_progress: Mapping[str, Mapping[str, Any]] | None = None,
        remote_action_count: int | None = None,
    ) -> HeartbeatResult:
        normalized_collector = collector_id.strip()
        if not normalized_collector:
            raise ValueError("Collector ID is required.")
        if remote_action_count is not None and remote_action_count < 0:
            raise ValueError("Remote action count must not be negative.")
        now = self._now()
        now_text = _serialize_time(now)
        renewed_until = _serialize_time(now + self.lease_ttl)
        checkpoint_json = _encode_mapping(checkpoint) if checkpoint is not None else None
        with self.database.transaction() as connection:
            lease = connection.execute(
                """
                SELECT 1 FROM collection_jobs
                WHERE job_id = ?
                  AND collector_id = ?
                  AND status IN ('claimed', 'running')
                  AND lease_expires_at > ?
                """,
                (job_id, normalized_collector, now_text),
            ).fetchone()
            if lease is None:
                raise LeaseRejected(job_id)
            validated_progress: list[tuple[str, Mapping[str, Any], str | None]] = []
            for segment_id, progress in (segment_progress or {}).items():
                if not isinstance(progress, Mapping):
                    raise InvalidSegmentProgress("Segment progress must be a mapping.")
                reported_status = progress.get("status") if "status" in progress else None
                if "status" in progress and (
                    not isinstance(reported_status, str)
                    or reported_status not in COLLECTOR_SEGMENT_STATUSES
                ):
                    raise InvalidSegmentProgress("Unsupported collector segment status.")
                stored = connection.execute(
                    """
                    SELECT status FROM collection_segments
                    WHERE segment_id = ? AND job_id = ?
                    """,
                    (segment_id, job_id),
                ).fetchone()
                if stored is None:
                    raise InvalidSegmentProgress("Segment does not belong to collection job.")
                if reported_status is not None and reported_status not in SEGMENT_STATUS_TRANSITIONS[
                    stored["status"]
                ]:
                    raise InvalidSegmentProgress("Collector segment status cannot move backwards.")
                validated_progress.append((segment_id, progress, reported_status))
            updated = connection.execute(
                """
                UPDATE collection_jobs
                SET status = CASE WHEN status = 'claimed' THEN 'running' ELSE status END,
                    lease_expires_at = ?,
                    last_heartbeat_at = ?,
                    checkpoint_json = COALESCE(?, checkpoint_json),
                    remote_action_count = MAX(remote_action_count, COALESCE(?, remote_action_count)),
                    updated_at = ?
                WHERE job_id = ?
                  AND collector_id = ?
                  AND status IN ('claimed', 'running')
                  AND lease_expires_at > ?
                """,
                (
                    renewed_until,
                    now_text,
                    checkpoint_json,
                    remote_action_count,
                    now_text,
                    job_id,
                    normalized_collector,
                    now_text,
                ),
            )
            if updated.rowcount != 1:
                raise LeaseRejected(job_id)
            for segment_id, progress, segment_status in validated_progress:
                progress_json = _encode_mapping(progress)
                segment_checkpoint = progress.get("checkpoint")
                segment_checkpoint_json = (
                    _encode_mapping(segment_checkpoint) if segment_checkpoint is not None else None
                )
                changed = connection.execute(
                    """
                    UPDATE collection_segments
                    SET status = COALESCE(?, status), progress_json = ?,
                        checkpoint_json = COALESCE(?, checkpoint_json), updated_at = ?
                    WHERE segment_id = ? AND job_id = ?
                    """,
                    (
                        segment_status,
                        progress_json,
                        segment_checkpoint_json,
                        now_text,
                        segment_id,
                        job_id,
                    ),
                )
                if changed.rowcount != 1:
                    raise LeaseRejected(job_id)
        job = self.get_job(job_id)
        return HeartbeatResult(job=job, cancel_requested=job.cancel_requested_at is not None)

    def request_cancel(self, job_id: str) -> CollectionJob:
        now = self._now()
        now_text = _serialize_time(now)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE collection_jobs SET updated_at = updated_at WHERE job_id = ?",
                (job_id,),
            )
            row = connection.execute(
                "SELECT status, lease_expires_at FROM collection_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise CollectionJobNotFound(job_id)
            status = row["status"]
            if status in TERMINAL_STATUSES:
                return self.get_job(job_id)
            lease_expired = (
                status in {"claimed", "running"}
                and row["lease_expires_at"] is not None
                and row["lease_expires_at"] <= now_text
            )
            if status in {"claimed", "running"} and not lease_expired:
                connection.execute(
                    """
                    UPDATE collection_jobs
                    SET cancel_requested_at = COALESCE(cancel_requested_at, ?), updated_at = ?
                    WHERE job_id = ?
                    """,
                    (now_text, now_text, job_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE collection_jobs
                    SET status = 'cancelled', cancel_requested_at = COALESCE(cancel_requested_at, ?),
                        collector_id = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (now_text, now_text, job_id),
                )
                connection.execute(
                    """
                    UPDATE collection_handoffs
                    SET revoked_at = COALESCE(revoked_at, ?)
                    WHERE job_id = ? AND claimed_at IS NULL
                    """,
                    (now_text, job_id),
                )
        return self.get_job(job_id)

    def acknowledge_cancel(self, job_id: str, collector_id: str) -> CollectionJob:
        normalized_collector = collector_id.strip()
        now = self._now()
        now_text = _serialize_time(now)
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE collection_jobs
                SET status = 'cancelled', collector_id = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE job_id = ?
                  AND collector_id = ?
                  AND status IN ('claimed', 'running')
                  AND cancel_requested_at IS NOT NULL
                  AND lease_expires_at > ?
                """,
                (now_text, job_id, normalized_collector, now_text),
            )
            if updated.rowcount != 1:
                raise LeaseRejected(job_id)
        return self.get_job(job_id)

    def reconcile_expired_leases(self) -> tuple[str, ...]:
        now_text = _serialize_time(self._now())
        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT job_id FROM collection_jobs
                WHERE status IN ('claimed', 'running') AND lease_expires_at <= ?
                ORDER BY created_at, rowid
                """,
                (now_text,),
            ).fetchall()
            job_ids = tuple(row["job_id"] for row in rows)
            if job_ids:
                placeholders = ",".join("?" for _ in job_ids)
                connection.execute(
                    f"""
                    UPDATE collection_jobs
                    SET status = CASE
                            WHEN cancel_requested_at IS NOT NULL THEN 'cancelled'
                            ELSE 'interrupted'
                        END,
                        collector_id = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE job_id IN ({placeholders})
                    """,
                    (now_text, *job_ids),
                )
        return job_ids

    def resume(self, job_id: str) -> CollectionJob:
        now = self._now()
        now_text = _serialize_time(now)
        cancelled = False
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE collection_jobs SET updated_at = updated_at WHERE job_id = ?",
                (job_id,),
            )
            row = connection.execute(
                """
                SELECT status, handoff_version, cancel_requested_at, lease_expires_at
                FROM collection_jobs WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise CollectionJobNotFound(job_id)
            status = row["status"]
            if (
                status in {"claimed", "running"}
                and row["lease_expires_at"] is not None
                and row["lease_expires_at"] <= now_text
            ):
                status = "cancelled" if row["cancel_requested_at"] is not None else "interrupted"
                connection.execute(
                    """
                    UPDATE collection_jobs
                    SET status = ?, collector_id = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (status, now_text, job_id),
                )
                if status == "cancelled":
                    cancelled = True
            if not cancelled and row["cancel_requested_at"] is not None:
                raise InvalidCollectionTransition("Cannot resume a cancelled collection job.")
            elif not cancelled and status == "pending_codex":
                active = connection.execute(
                    """
                    SELECT 1 FROM collection_handoffs
                    WHERE job_id = ?
                      AND instance_id = ?
                      AND claimed_at IS NULL
                      AND revoked_at IS NULL
                      AND expires_at > ?
                    LIMIT 1
                    """,
                    (job_id, self.instance_id, now_text),
                ).fetchone()
                if active is not None:
                    raise InvalidCollectionTransition("Collection job already has an active handoff.")
            elif not cancelled and status not in RECOVERABLE_STATUSES:
                raise InvalidCollectionTransition(f"Cannot resume collection job from {status}.")
            if not cancelled:
                connection.execute(
                    """
                    UPDATE collection_handoffs
                    SET revoked_at = COALESCE(revoked_at, ?)
                    WHERE job_id = ?
                    """,
                    (now_text, job_id),
                )
                connection.execute(
                    """
                    UPDATE collection_jobs
                    SET status = 'pending_codex', collector_id = NULL, lease_expires_at = NULL,
                        cancel_requested_at = NULL, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (now_text, job_id),
                )
                self._issue_handoff(connection, job_id, row["handoff_version"] + 1, now)
        if cancelled:
            raise InvalidCollectionTransition("Cannot resume a cancelled collection job.")
        return self.get_job(job_id)

    def wait_for_human(
        self,
        job_id: str,
        collector_id: str,
        *,
        error: Mapping[str, Any],
        checkpoint: Mapping[str, Any] | None = None,
    ) -> CollectionJob:
        return self._leased_transition(
            job_id,
            collector_id,
            status="waiting_for_human",
            error=error,
            checkpoint=checkpoint,
        )

    def rate_limit(
        self, job_id: str, collector_id: str, *, error: Mapping[str, Any]
    ) -> CollectionJob:
        return self._leased_transition(job_id, collector_id, status="rate_limited", error=error)

    def mark_partial(
        self, job_id: str, collector_id: str, *, error: Mapping[str, Any]
    ) -> CollectionJob:
        return self._leased_transition(job_id, collector_id, status="partial", error=error)

    def fail(self, job_id: str, collector_id: str, *, error: Mapping[str, Any]) -> CollectionJob:
        return self._leased_transition(job_id, collector_id, status="failed", error=error)

    def _leased_transition(
        self,
        job_id: str,
        collector_id: str,
        *,
        status: str,
        error: Mapping[str, Any],
        checkpoint: Mapping[str, Any] | None = None,
    ) -> CollectionJob:
        normalized_collector = collector_id.strip()
        now_text = _serialize_time(self._now())
        error_json = _encode_mapping(error)
        checkpoint_json = _encode_mapping(checkpoint) if checkpoint is not None else None
        error_code = error.get("code")
        if not isinstance(error_code, str) or not error_code.strip():
            raise ValueError("Structured error must include a non-empty code.")
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE collection_jobs
                SET status = ?, collector_id = NULL, lease_expires_at = NULL,
                    checkpoint_json = COALESCE(?, checkpoint_json),
                    error_code = ?, error_json = ?, updated_at = ?
                WHERE job_id = ?
                  AND collector_id = ?
                  AND status IN ('claimed', 'running')
                  AND cancel_requested_at IS NULL
                  AND lease_expires_at > ?
                """,
                (
                    status,
                    checkpoint_json,
                    error_code,
                    error_json,
                    now_text,
                    job_id,
                    normalized_collector,
                    now_text,
                ),
            )
            if updated.rowcount != 1:
                cancelled = connection.execute(
                    """
                    SELECT 1 FROM collection_jobs
                    WHERE job_id = ? AND cancel_requested_at IS NOT NULL
                    """,
                    (job_id,),
                ).fetchone()
                if cancelled is not None:
                    raise InvalidCollectionTransition("Collection job cancellation is pending.")
                raise LeaseRejected(job_id)
        return self.get_job(job_id)

    def _issue_handoff(
        self, connection: sqlite3.Connection, job_id: str, version: int, now: datetime
    ) -> str:
        handoff_id = secrets.token_urlsafe(32)
        connection.execute(
            """
            INSERT INTO collection_handoffs(
                handoff_id, job_id, version, instance_id, expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                handoff_id,
                job_id,
                version,
                self.instance_id,
                _serialize_time(now + self.handoff_ttl),
                _serialize_time(now),
            ),
        )
        connection.execute(
            "UPDATE collection_jobs SET handoff_version = ?, updated_at = ? WHERE job_id = ?",
            (version, _serialize_time(now), job_id),
        )
        return handoff_id

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Clock must return a timezone-aware datetime.")
        return value.astimezone(timezone.utc)


def _job_from_rows(
    row: sqlite3.Row, segment_rows: list[sqlite3.Row], handoff_rows: list[sqlite3.Row]
) -> CollectionJob:
    return CollectionJob(
        job_id=row["job_id"],
        account_id=row["account_id"],
        mode=row["mode"],
        status=row["status"],
        requested_interval=UtcInterval(parse_utc(row["requested_start_at"]), parse_utc(row["requested_end_at"])),
        outcome=row["outcome"],
        remote_action_count=row["remote_action_count"],
        handoff_version=row["handoff_version"],
        collector_id=row["collector_id"],
        lease_expires_at=_optional_time(row["lease_expires_at"]),
        last_heartbeat_at=_optional_time(row["last_heartbeat_at"]),
        cancel_requested_at=_optional_time(row["cancel_requested_at"]),
        checkpoint=_optional_json(row["checkpoint_json"]),
        error=_optional_json(row["error_json"]),
        created_at=parse_utc(row["created_at"]),
        updated_at=parse_utc(row["updated_at"]),
        segments=tuple(
            CollectionSegment(
                segment_id=segment["segment_id"],
                ordinal=segment["ordinal"],
                interval=UtcInterval(parse_utc(segment["start_at"]), parse_utc(segment["end_at"])),
                status=segment["status"],
                checkpoint=_optional_json(segment["checkpoint_json"]),
                progress=_optional_json(segment["progress_json"]),
            )
            for segment in segment_rows
        ),
        handoffs=tuple(
            CollectionHandoff(
                handoff_id=handoff["handoff_id"],
                version=handoff["version"],
                instance_id=handoff["instance_id"],
                expires_at=parse_utc(handoff["expires_at"]),
                claimed_at=_optional_time(handoff["claimed_at"]),
                revoked_at=_optional_time(handoff["revoked_at"]),
                collector_id=handoff["collector_id"],
            )
            for handoff in handoff_rows
        ),
    )


def _optional_time(value: str | None) -> datetime | None:
    return parse_utc(value) if value is not None else None


def _serialize_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _optional_json(value: str | None) -> Mapping[str, Any] | None:
    return json.loads(value) if value is not None else None


def _encode_mapping(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise ValueError("Structured state must be a mapping.")
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
