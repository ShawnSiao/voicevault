from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .app_db import AppDatabase


class IndexJobError(Exception):
    """Base class for stable index-job failures."""


class IndexJobNotFound(IndexJobError):
    pass


class ActiveIndexJobExists(IndexJobError):
    pass


class IndexJobStateError(IndexJobError):
    pass


@dataclass(frozen=True)
class IndexJob:
    job_id: str
    person_id: str
    status: str
    generation_id: str | None
    retrieval_mode: str
    error: dict[str, Any] | None
    created_at: str
    started_at: str | None
    completed_at: str | None


class IndexJobService:
    def __init__(
        self,
        database: AppDatabase,
        index_service: Any,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(database, AppDatabase):
            raise TypeError("Index-job database must be an AppDatabase.")
        if not callable(getattr(index_service, "rebuild_person", None)):
            raise TypeError("Index service is invalid.")
        if not callable(clock):
            raise TypeError("Index-job clock must be callable.")
        self.database = database
        self.index_service = index_service
        self.clock = clock

    def create(self, person_id: str) -> IndexJob:
        if not isinstance(person_id, str) or not person_id.strip():
            raise ValueError("Person ID is required.")
        job_id = str(uuid.uuid4())
        with self.database.transaction(immediate=True) as connection:
            if connection.execute(
                "SELECT 1 FROM persons WHERE person_id = ?", (person_id,)
            ).fetchone() is None:
                raise ValueError("Person does not exist.")
            try:
                connection.execute(
                    """
                    INSERT INTO index_jobs(job_id, person_id, status, created_at)
                    VALUES (?, ?, 'pending', ?)
                    """,
                    (job_id, person_id, self._now()),
                )
            except sqlite3.IntegrityError:
                if connection.execute(
                    "SELECT 1 FROM index_jobs WHERE person_id = ? AND status IN ('pending', 'running')",
                    (person_id,),
                ).fetchone() is not None:
                    raise ActiveIndexJobExists("An active index job already exists.") from None
                raise
            return self._get(connection, job_id)

    def run(self, job_id: str) -> IndexJob:
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE index_jobs SET status = 'running', started_at = ?
                WHERE job_id = ? AND status = 'pending'
                """,
                (self._now(), job_id),
            )
            if cursor.rowcount != 1:
                self._raise_transition(connection, job_id)
            person_id = connection.execute(
                "SELECT person_id FROM index_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()[0]
        try:
            result = self.index_service.rebuild_person(person_id)
        except Exception:
            result = None
        with self.database.transaction(immediate=True) as connection:
            completed_at = self._now()
            if (
                result is not None
                and result.status in {"ready", "degraded", "stale"}
                and result.retrieval_mode in {"hybrid", "fulltext_only"}
            ):
                connection.execute(
                    """
                    UPDATE index_jobs
                    SET status = 'succeeded', generation_id = ?, retrieval_mode = ?,
                        error_json = NULL, completed_at = ?
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (result.generation_id, result.retrieval_mode, completed_at, job_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE index_jobs
                    SET status = 'failed', retrieval_mode = 'none',
                        error_json = '{"code":"index_build_failed"}', completed_at = ?
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (completed_at, job_id),
                )
            return self._get(connection, job_id)

    def get(self, job_id: str) -> IndexJob:
        with self.database.connect() as connection:
            return self._get(connection, job_id)

    def fail_incomplete(self, job_id: str, code: str) -> IndexJob:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("Index-job failure code is required.")
        normalized_code = code.strip()
        if (
            len(normalized_code) > 64
            or not normalized_code[0].islower()
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
                for character in normalized_code
            )
        ):
            raise ValueError("Index-job failure code is invalid.")
        error_json = json.dumps(
            {"code": normalized_code},
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE index_jobs
                SET status = 'failed', retrieval_mode = 'none',
                    error_json = ?, completed_at = ?
                WHERE job_id = ? AND status IN ('pending', 'running')
                """,
                (error_json, self._now(), job_id),
            )
            if cursor.rowcount != 1:
                self._raise_transition(connection, job_id)
            return self._get(connection, job_id)

    def list(self) -> list[IndexJob]:
        with self.database.connect() as connection:
            return [
                self._from_row(row)
                for row in connection.execute(
                    "SELECT * FROM index_jobs ORDER BY created_at, rowid"
                )
            ]

    def reconcile_incomplete(self) -> int:
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE index_jobs
                SET status = 'interrupted', error_json = '{"code":"service_restarted"}',
                    completed_at = ?
                WHERE status = 'running'
                """,
                (self._now(),),
            )
            return cursor.rowcount

    @staticmethod
    def _get(connection: sqlite3.Connection, job_id: str) -> IndexJob:
        row = connection.execute(
            "SELECT * FROM index_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise IndexJobNotFound("Index job was not found.")
        return IndexJobService._from_row(row)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> IndexJob:
        error = None if row["error_json"] is None else json.loads(row["error_json"])
        return IndexJob(
            job_id=row["job_id"],
            person_id=row["person_id"],
            status=row["status"],
            generation_id=row["generation_id"],
            retrieval_mode=row["retrieval_mode"],
            error=error,
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _raise_transition(connection: sqlite3.Connection, job_id: str) -> None:
        if connection.execute(
            "SELECT 1 FROM index_jobs WHERE job_id = ?", (job_id,)
        ).fetchone() is None:
            raise IndexJobNotFound("Index job was not found.")
        raise IndexJobStateError("Index job state transition is invalid.")

    def _now(self) -> str:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Index-job clock must return an aware datetime.")
        return value.astimezone(timezone.utc).isoformat()
