from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .app_db import AppDatabase
from .post_archive import normalize_post_text, post_content_sha256


class HistoricalArchiveImportError(Exception):
    """A historical archive cannot be safely imported into the person archive."""


_XUEQIU_URL = re.compile(r"https://xueqiu\.com/(?P<user_id>[0-9]+)/(?P<post_id>[0-9]+)\Z")
_PUBLISHED_AT = re.compile(
    r"^\s*(?:修改于)?(?P<timestamp>[0-9]{4}-[0-9]{2}-[0-9]{2}\s+[0-9]{2}:[0-9]{2})"
)
_PUBLISHED_MONTH_DAY = re.compile(
    r"^\s*(?:修改于)?(?P<month>[0-9]{2})-(?P<day>[0-9]{2})\s+(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2})"
)
_PUBLISHED_RELATIVE = re.compile(r"^\s*(?P<day>今天|昨天)\s+(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2})")
_CHINA_TIME = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class _HistoricalRecord:
    external_post_id: str
    canonical_url: str
    published_at: str
    captured_at: str
    content_text: str
    content_hash: str


@dataclass(frozen=True)
class _Preflight:
    source_fingerprint: str
    records: tuple[_HistoricalRecord, ...]
    report: dict[str, object]


class HistoricalArchiveImporter:
    """Import a completed legacy post archive without inventing collection evidence."""

    def __init__(self, database: AppDatabase) -> None:
        if not isinstance(database, AppDatabase):
            raise TypeError("Historical archive import database must be an AppDatabase.")
        self.database = database

    def preview(
        self,
        archive_path: str | Path,
        *,
        person_id: str,
        account_id: str,
    ) -> dict[str, object]:
        """Validate an archive and its target binding without changing database state."""
        return self._preflight(archive_path, person_id=person_id, account_id=account_id).report

    def import_archive(
        self,
        archive_path: str | Path,
        *,
        person_id: str,
        account_id: str,
    ) -> dict[str, object]:
        """Write validated historical posts and revisions without coverage or evidence rows."""
        preflight = self._preflight(archive_path, person_id=person_id, account_id=account_id)
        now = _serialize_utc(datetime.now(timezone.utc))
        run_id = str(uuid.uuid4())
        imported = {"posts": 0, "revisions": 0}
        existing = {"posts": 0, "revisions": 0}
        seen_existing = {"posts": set(), "revisions": set()}

        with self.database.transaction(immediate=True) as connection:
            self._require_target_binding(connection, person_id=person_id, account_id=account_id)
            connection.execute(
                """
                INSERT INTO legacy_import_runs(
                    run_id, source_fingerprint, status, created_at
                ) VALUES (?, ?, 'running', ?)
                """,
                (run_id, preflight.source_fingerprint, now),
            )
            for record in preflight.records:
                post_id, post_created = self._resolve_post(
                    connection,
                    account_id=account_id,
                    record=record,
                    imported_at=now,
                )
                _count(post_created, "posts", post_id, imported, existing, seen_existing)
                revision_id, revision_created = self._resolve_revision(
                    connection,
                    post_id=post_id,
                    record=record,
                )
                _count(revision_created, "revisions", revision_id, imported, existing, seen_existing)

            report = {
                "run_id": run_id,
                "status": "succeeded",
                "source_fingerprint": preflight.source_fingerprint,
                "person_id": person_id,
                "account_id": account_id,
                "imported": imported,
                "existing": existing,
                **preflight.report,
            }
            connection.execute(
                """
                UPDATE legacy_import_runs
                SET status = 'succeeded', summary_json = ?, completed_at = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (json.dumps(report, ensure_ascii=False, sort_keys=True), now, run_id),
            )
        return report

    def _preflight(
        self,
        archive_path: str | Path,
        *,
        person_id: str,
        account_id: str,
    ) -> _Preflight:
        target = self._target_binding(person_id=person_id, account_id=account_id)
        path = Path(archive_path).expanduser()
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HistoricalArchiveImportError("Historical archive must be readable UTF-8 JSON.") from exc
        if not isinstance(payload, list):
            raise HistoricalArchiveImportError("Historical archive root must be a JSON array.")

        fingerprint = hashlib.sha256(raw).hexdigest()
        post_ids = [item.get("post_id") if isinstance(item, dict) else None for item in payload]
        duplicates = {
            post_id
            for post_id, count in Counter(post_ids).items()
            if isinstance(post_id, str) and post_id.strip() and count > 1
        }
        records: list[_HistoricalRecord] = []
        skipped: list[dict[str, object]] = []
        time_parse_errors = 0

        for ordinal, item in enumerate(payload, start=1):
            post_id = item.get("post_id") if isinstance(item, dict) else None
            reason = _record_problem(item, target["platform"], target["external_user_id"], duplicates)
            if reason is None:
                try:
                    record = _validated_record(item)
                except ValueError as exc:
                    reason = str(exc)
                    time_parse_errors += 1
                else:
                    records.append(record)
                    continue
            skipped.append(
                {
                    "ordinal": ordinal,
                    "post_id": post_id if isinstance(post_id, str) else None,
                    "reason": reason,
                }
            )

        duplicate_records = sum(
            1 for post_id in post_ids if isinstance(post_id, str) and post_id in duplicates
        )
        report: dict[str, object] = {
            "source_fingerprint": fingerprint,
            "total_records": len(payload),
            "valid_records": len(records),
            "skipped_records": len(skipped),
            "duplicate_records": duplicate_records,
            "time_parse_errors": time_parse_errors,
            "skipped_examples": skipped[:100],
            "provenance": "historical_archive_import",
            "coverage_created": False,
            "evidence_created": False,
        }
        return _Preflight(fingerprint, tuple(records), report)

    def _target_binding(self, *, person_id: str, account_id: str) -> sqlite3.Row:
        with self.database.connect() as connection:
            return self._require_target_binding(connection, person_id=person_id, account_id=account_id)

    @staticmethod
    def _require_target_binding(
        connection: sqlite3.Connection, *, person_id: str, account_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT account_id, person_id, platform, external_user_id
            FROM platform_accounts
            WHERE account_id = ? AND person_id = ?
            """,
            (account_id, person_id),
        ).fetchone()
        if row is None:
            raise HistoricalArchiveImportError("Target account does not belong to the selected person.")
        if row["platform"] != "xueqiu":
            raise HistoricalArchiveImportError("Historical Xueqiu archive requires a Xueqiu target account.")
        return row

    @staticmethod
    def _resolve_post(
        connection: sqlite3.Connection,
        *,
        account_id: str,
        record: _HistoricalRecord,
        imported_at: str,
    ) -> tuple[str, bool]:
        row = connection.execute(
            """
            SELECT post_id, published_at, canonical_url
            FROM posts
            WHERE account_id = ? AND external_post_id = ?
            """,
            (account_id, record.external_post_id),
        ).fetchone()
        if row is not None:
            if row["published_at"] != record.published_at or row["canonical_url"] != record.canonical_url:
                raise HistoricalArchiveImportError(
                    f"Historical post identity conflicts with existing post: {record.external_post_id}"
                )
            return row["post_id"], False

        post_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO posts(
                post_id, account_id, external_post_id, published_at, canonical_url, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                post_id,
                account_id,
                record.external_post_id,
                record.published_at,
                record.canonical_url,
                imported_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO content_dispositions(
                post_id, state, reason, changed_at, purged_content_hash
            ) VALUES (?, 'active', NULL, ?, NULL)
            """,
            (post_id, imported_at),
        )
        return post_id, True

    @staticmethod
    def _resolve_revision(
        connection: sqlite3.Connection, *, post_id: str, record: _HistoricalRecord
    ) -> tuple[str, bool]:
        row = connection.execute(
            """
            SELECT revision_id, content_text
            FROM post_revisions
            WHERE post_id = ? AND content_hash = ?
            """,
            (post_id, record.content_hash),
        ).fetchone()
        if row is not None:
            if row["content_text"] != record.content_text:
                raise HistoricalArchiveImportError("Existing revision does not match its content hash.")
            return row["revision_id"], False

        revision_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO post_revisions(
                revision_id, post_id, content_hash, content_text, captured_at, first_seen_job_id
            ) VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (
                revision_id,
                post_id,
                record.content_hash,
                record.content_text,
                record.captured_at,
            ),
        )
        return revision_id, True


def _record_problem(
    item: object, expected_platform: str, expected_user_id: str, duplicates: set[str]
) -> str | None:
    if not isinstance(item, dict):
        return "record_not_object"
    post_id = item.get("post_id")
    if not isinstance(post_id, str) or not post_id.isdecimal():
        return "invalid_post_id"
    if post_id in duplicates:
        return "duplicate_post_id"
    if item.get("content_complete") is not True:
        return "content_incomplete"
    content = item.get("content")
    if not isinstance(content, str):
        return "missing_content"
    try:
        normalize_post_text(content)
    except Exception:
        return "missing_content"
    source_url = item.get("source_url")
    if not isinstance(source_url, str):
        return "invalid_source_url"
    match = _XUEQIU_URL.fullmatch(source_url)
    if match is None or expected_platform != "xueqiu":
        return "invalid_source_url"
    if match.group("user_id") != expected_user_id or match.group("post_id") != post_id:
        return "source_account_mismatch"
    return None


def _validated_record(item: dict[str, object]) -> _HistoricalRecord:
    post_id = item["post_id"]
    source_url = item["source_url"]
    content = normalize_post_text(item["content"])
    captured_at = _parse_captured_at(item.get("collected_at"))
    return _HistoricalRecord(
        external_post_id=post_id,
        canonical_url=source_url,
        published_at=_parse_published_at(item.get("publish_time_text"), captured_at),
        captured_at=captured_at,
        content_text=content,
        content_hash=post_content_sha256(content),
    )


def _parse_published_at(value: object, captured_at: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_published_at")
    match = _PUBLISHED_AT.match(value)
    if match is not None:
        try:
            local = datetime.strptime(match.group("timestamp"), "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise ValueError("invalid_published_at") from exc
        return _serialize_utc(local.replace(tzinfo=_CHINA_TIME).astimezone(timezone.utc))

    reference = datetime.fromisoformat(captured_at).astimezone(_CHINA_TIME)
    month_day = _PUBLISHED_MONTH_DAY.match(value)
    if month_day is not None:
        try:
            local = datetime(
                reference.year,
                int(month_day.group("month")),
                int(month_day.group("day")),
                int(month_day.group("hour")),
                int(month_day.group("minute")),
            )
            if local.date() > reference.date():
                local = local.replace(year=local.year - 1)
        except ValueError as exc:
            raise ValueError("invalid_published_at") from exc
        return _serialize_utc(local.replace(tzinfo=_CHINA_TIME).astimezone(timezone.utc))

    relative = _PUBLISHED_RELATIVE.match(value)
    if relative is None:
        raise ValueError("invalid_published_at")
    day = reference.date()
    if relative.group("day") == "昨天":
        day -= timedelta(days=1)
    try:
        local = datetime(
            day.year, day.month, day.day, int(relative.group("hour")), int(relative.group("minute"))
        )
    except ValueError as exc:
        raise ValueError("invalid_published_at") from exc
    return _serialize_utc(local.replace(tzinfo=_CHINA_TIME).astimezone(timezone.utc))


def _parse_captured_at(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid_collected_at")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid_collected_at") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid_collected_at")
    return _serialize_utc(parsed.astimezone(timezone.utc))


def _serialize_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _count(
    created: bool,
    key: str,
    identifier: str,
    imported: dict[str, int],
    existing: dict[str, int],
    seen_existing: dict[str, set[str]],
) -> None:
    if created:
        imported[key] += 1
    elif identifier not in seen_existing[key]:
        existing[key] += 1
        seen_existing[key].add(identifier)
