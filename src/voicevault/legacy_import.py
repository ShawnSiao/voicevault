from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

from .app_db import AppDatabase
from .importers import load_statements_from_kb
from .kb import KnowledgeBase
from .person_archive import canonicalize_external_user_id, canonicalize_platform


class LegacyImporter:
    """Read legacy Role/Statement files and write only to the new application DB."""

    def __init__(self, database: AppDatabase) -> None:
        if not isinstance(database, AppDatabase):
            raise TypeError("Legacy import database must be an AppDatabase.")
        self.database = database

    def import_kb(self, root: str | Path) -> dict[str, Any]:
        source_root = Path(root).expanduser()
        kb = KnowledgeBase.from_path(source_root)
        statements = load_statements_from_kb(kb)
        run_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        imported = {key: 0 for key in ("persons", "accounts", "posts", "revisions")}
        existing = {key: 0 for key in ("persons", "accounts", "posts", "revisions")}
        seen_existing = {key: set() for key in existing}
        skipped: list[dict[str, str]] = []

        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO legacy_import_runs(
                    run_id, source_fingerprint, status, created_at
                ) VALUES (?, ?, 'running', ?)
                """,
                (run_id, _source_fingerprint(kb.roles_dir), now),
            )
            for statement in statements:
                statement_id = statement.statement_id.strip()
                source_key = f"{statement.role_id}|{statement_id}" if statement_id else ""
                try:
                    platform = canonicalize_platform(statement.source_platform)
                    external_user_id = canonicalize_external_user_id(
                        platform, statement.source_user_id
                    )
                    if not statement_id or not statement.body.strip() or not statement.source_url.strip():
                        raise ValueError
                    published_at = _legacy_time(statement.published_at)
                    captured_at = _legacy_time(statement.captured_at)
                except (TypeError, ValueError):
                    skipped.append(
                        {
                            "source_key": source_key or "unknown",
                            "reason": "identity_or_content_uncertain",
                        }
                    )
                    continue

                person_id, created = _mapped_or_create_person(
                    connection,
                    run_id,
                    statement.role_id,
                    statement.source_author or statement.role_id,
                    now,
                )
                _count(created, "persons", person_id, imported, existing, seen_existing)
                account_source = f"{statement.role_id}|{platform}|{external_user_id}"
                account_id, created = _mapped_or_create_account(
                    connection,
                    run_id,
                    account_source,
                    person_id,
                    platform,
                    external_user_id,
                    statement.source_author or None,
                    now,
                )
                _count(created, "accounts", account_id, imported, existing, seen_existing)
                post_id, created = _mapped_or_create_post(
                    connection,
                    run_id,
                    source_key,
                    account_id,
                    statement_id,
                    published_at,
                    statement.source_url,
                    now,
                )
                _count(created, "posts", post_id, imported, existing, seen_existing)
                digest = hashlib.sha256(statement.body.encode("utf-8")).hexdigest()
                revision_source = f"{source_key}|{digest}"
                revision_id, created = _mapped_or_create_revision(
                    connection,
                    run_id,
                    revision_source,
                    post_id,
                    digest,
                    statement.body,
                    captured_at,
                    now,
                )
                _count(created, "revisions", revision_id, imported, existing, seen_existing)

            report = {
                "run_id": run_id,
                "status": "succeeded",
                "imported": imported,
                "existing": existing,
                "skipped": skipped,
            }
            connection.execute(
                """
                UPDATE legacy_import_runs
                SET status = 'succeeded', summary_json = ?, completed_at = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (
                    json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    datetime.now(timezone.utc).isoformat(),
                    run_id,
                ),
            )
        return report


def _mapping(connection, source_kind: str, source_key: str, target_kind: str) -> str | None:
    row = connection.execute(
        """
        SELECT target_id FROM legacy_import_mappings
        WHERE source_kind = ? AND source_key = ? AND target_kind = ?
        """,
        (source_kind, source_key, target_kind),
    ).fetchone()
    return None if row is None else row["target_id"]


def _record_mapping(connection, source_kind, source_key, target_kind, target_id, run_id, now) -> None:
    connection.execute(
        """
        INSERT INTO legacy_import_mappings(
            source_kind, source_key, target_kind, target_id, first_run_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (source_kind, source_key, target_kind, target_id, run_id, now),
    )


def _mapped_or_create_person(connection, run_id, role_id, display_name, now):
    target = _mapping(connection, "role", role_id, "person")
    if target is not None:
        return target, False
    target = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO persons(person_id, display_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (target, display_name.strip() or role_id, now, now),
    )
    _record_mapping(connection, "role", role_id, "person", target, run_id, now)
    return target, True


def _mapped_or_create_account(connection, run_id, source_key, person_id, platform, external_user_id, display_name, now):
    target = _mapping(connection, "account", source_key, "platform_account")
    if target is not None:
        return target, False
    target = str(uuid.uuid4())
    connection.execute(
        """
        INSERT INTO platform_accounts(
            account_id, person_id, platform, external_user_id, display_name,
            archive_basis_confirmed_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (target, person_id, platform, external_user_id, display_name, now, now),
    )
    _record_mapping(connection, "account", source_key, "platform_account", target, run_id, now)
    return target, True


def _mapped_or_create_post(connection, run_id, source_key, account_id, external_post_id, published_at, url, now):
    target = _mapping(connection, "statement", source_key, "post")
    if target is not None:
        return target, False
    target = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?)",
        (target, account_id, external_post_id, published_at, url, now),
    )
    connection.execute(
        "INSERT INTO content_dispositions VALUES (?, 'active', NULL, ?, NULL)",
        (target, now),
    )
    _record_mapping(connection, "statement", source_key, "post", target, run_id, now)
    return target, True


def _mapped_or_create_revision(connection, run_id, source_key, post_id, digest, body, captured_at, now):
    target = _mapping(connection, "statement_revision", source_key, "post_revision")
    if target is not None:
        return target, False
    target = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, NULL)",
        (target, post_id, digest, body, captured_at),
    )
    _record_mapping(connection, "statement_revision", source_key, "post_revision", target, run_id, now)
    return target, True


def _count(created, key, target_id, imported, existing, seen_existing) -> None:
    if created:
        imported[key] += 1
    elif target_id not in seen_existing[key]:
        existing[key] += 1
        seen_existing[key].add(target_id)


def _legacy_time(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Legacy time is missing.")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed_date = datetime.strptime(text, "%Y-%m-%d").date()
        parsed = datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _source_fingerprint(roles_dir: Path) -> str:
    digest = hashlib.sha256()
    if roles_dir.exists():
        for path in sorted(item for item in roles_dir.rglob("*") if item.is_file()):
            digest.update(path.relative_to(roles_dir).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()
