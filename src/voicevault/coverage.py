from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .app_db import AppDatabase


SHANGHAI = ZoneInfo("Asia/Shanghai")


class CoverageDomainError(Exception):
    """Base class for stable coverage domain failures."""


class CoverageAccountNotFound(CoverageDomainError):
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        super().__init__(f"Platform account not found: {account_id}")


class CoverageAccountUnconfirmed(CoverageDomainError):
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        super().__init__(f"Platform account archive basis is not confirmed: {account_id}")


@dataclass(frozen=True, order=True)
class UtcInterval:
    start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        start = _as_utc(self.start_at)
        end = _as_utc(self.end_at)
        if start >= end:
            raise ValueError("Interval start must be before interval end.")
        object.__setattr__(self, "start_at", start)
        object.__setattr__(self, "end_at", end)


def page_date_range_to_utc(start_date: str | date, end_date: str | date) -> UtcInterval:
    start_day = _parse_date(start_date)
    end_day = _parse_date(end_date)
    if end_day < start_day:
        raise ValueError("End date must not be before start date.")
    local_start = datetime.combine(start_day, time.min, tzinfo=SHANGHAI)
    local_end = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=SHANGHAI)
    return UtcInterval(local_start, local_end)


def serialize_utc(value: datetime) -> str:
    utc = _as_utc(value)
    timespec = "microseconds" if utc.microsecond else "seconds"
    return utc.isoformat(timespec=timespec).replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _as_utc(parsed)


def merge_intervals(intervals: list[UtcInterval]) -> list[UtcInterval]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for current in ordered[1:]:
        previous = merged[-1]
        if current.start_at <= previous.end_at:
            merged[-1] = UtcInterval(previous.start_at, max(previous.end_at, current.end_at))
        else:
            merged.append(current)
    return merged


def subtract_intervals(requested: UtcInterval, covered: list[UtcInterval]) -> list[UtcInterval]:
    clipped = [
        UtcInterval(max(requested.start_at, interval.start_at), min(requested.end_at, interval.end_at))
        for interval in covered
        if interval.end_at > requested.start_at and interval.start_at < requested.end_at
    ]
    missing: list[UtcInterval] = []
    cursor = requested.start_at
    for interval in merge_intervals(clipped):
        if cursor < interval.start_at:
            missing.append(UtcInterval(cursor, interval.start_at))
        cursor = max(cursor, interval.end_at)
    if cursor < requested.end_at:
        missing.append(UtcInterval(cursor, requested.end_at))
    return missing


class CoverageRepository:
    def __init__(self, database: AppDatabase) -> None:
        self.database = database

    def record_validated_complete(
        self,
        account_id: str,
        interval: UtcInterval,
    ) -> None:
        with self.database.transaction() as connection:
            _insert_validated_complete(
                connection,
                account_id=account_id,
                interval=interval,
                job_id=None,
                recorded_at=datetime.now(timezone.utc),
            )

    def merged(self, account_id: str) -> list[UtcInterval]:
        with self.database.connect() as connection:
            self._require_account(connection, account_id)
            rows = connection.execute(
                "SELECT start_at, end_at FROM coverage_intervals WHERE account_id = ? ORDER BY start_at, end_at",
                (account_id,),
            ).fetchall()
        return merge_intervals([UtcInterval(parse_utc(row["start_at"]), parse_utc(row["end_at"])) for row in rows])

    def missing(self, account_id: str, requested: UtcInterval) -> list[UtcInterval]:
        return subtract_intervals(requested, self.merged(account_id))

    @staticmethod
    def _require_account(connection: sqlite3.Connection, account_id: str) -> sqlite3.Row:
        account = connection.execute(
            "SELECT account_id, archive_basis_confirmed_at FROM platform_accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if account is None:
            raise CoverageAccountNotFound(account_id)
        return account


def insert_validated_complete(
    connection: sqlite3.Connection,
    *,
    account_id: str,
    interval: UtcInterval,
    job_id: str,
    recorded_at: datetime,
) -> bool:
    """Insert proven coverage on the caller's transaction without committing it."""
    return _insert_validated_complete(
        connection,
        account_id=account_id,
        interval=interval,
        job_id=job_id,
        recorded_at=recorded_at,
    )


def _insert_validated_complete(
    connection: sqlite3.Connection,
    *,
    account_id: str,
    interval: UtcInterval,
    job_id: str | None,
    recorded_at: datetime,
) -> bool:
    account = CoverageRepository._require_account(connection, account_id)
    if account["archive_basis_confirmed_at"] is None:
        raise CoverageAccountUnconfirmed(account_id)
    if job_id is not None:
        job = connection.execute(
            "SELECT account_id FROM collection_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if job is None or job["account_id"] != account_id:
            raise CoverageDomainError("Coverage job does not belong to the account.")
    inserted = connection.execute(
        """
        INSERT OR IGNORE INTO coverage_intervals(
            coverage_id, account_id, start_at, end_at, recorded_at, job_id
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            account_id,
            serialize_utc(interval.start_at),
            serialize_utc(interval.end_at),
            serialize_utc(recorded_at),
            job_id,
        ),
    )
    return inserted.rowcount == 1


def _parse_date(value: str | date) -> date:
    if isinstance(value, datetime):
        raise ValueError("Page date must not include a time.")
    return date.fromisoformat(value) if isinstance(value, str) else value


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC interval timestamps must include a timezone.")
    return value.astimezone(timezone.utc)
