from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from .importers import load_statements_from_kb, split_list, stable_statement_id
from .index import VoiceVaultIndex
from .kb import KnowledgeBase
from .models import Statement


@dataclass(frozen=True)
class SyncResult:
    captures_seen: int
    notes_written: int
    duplicates_skipped: int
    statements_indexed: int
    source_files: list[str]
    errors: list[dict[str, str]]
    archived_files: list[str]
    capture_files: list[dict[str, Any]]


def sync_once(kb: KnowledgeBase, archive_processed: bool = False) -> SyncResult:
    kb.inbox_captures_dir.mkdir(parents=True, exist_ok=True)
    kb.inbox_archive_dir.mkdir(parents=True, exist_ok=True)
    captures, capture_files = _load_capture_statements_lenient(kb.inbox_captures_dir)
    source_files = [Path(item["source_file"]) for item in capture_files]
    errors = [
        {"source_file": item["source_file"], "message": item["error"]}
        for item in capture_files
        if item["status"] == "failed"
    ]
    notes_written = 0
    duplicates_skipped = 0
    for item in capture_files:
        item_notes_written = 0
        item_duplicates_skipped = 0
        for statement in item.pop("_statements", []):
            if write_statement_note(kb, statement):
                item_notes_written += 1
            else:
                item_duplicates_skipped += 1
        item["notes_written"] = item_notes_written
        item["duplicates_skipped"] = item_duplicates_skipped
        notes_written += item_notes_written
        duplicates_skipped += item_duplicates_skipped
    statements = load_statements_from_kb(kb)
    indexed = VoiceVaultIndex(kb).rebuild(statements)
    archived_files = _archive_processed_files(kb, capture_files) if archive_processed else []
    result = SyncResult(
        captures_seen=len(captures),
        notes_written=notes_written,
        duplicates_skipped=duplicates_skipped,
        statements_indexed=indexed,
        source_files=[str(path) for path in source_files],
        errors=errors,
        archived_files=archived_files,
        capture_files=capture_files,
    )
    _write_capture_status(kb, capture_files)
    _write_sync_status(kb, result)
    return result


def watch_sync(kb: KnowledgeBase, interval_seconds: float, archive_processed: bool = False) -> Iterator[SyncResult]:
    while True:
        yield sync_once(kb, archive_processed=archive_processed)
        time.sleep(max(interval_seconds, 0.1))


def load_capture_statements(captures_dir: Path) -> tuple[list[Statement], list[Path]]:
    statements: list[Statement] = []
    source_files: list[Path] = []
    if not captures_dir.exists():
        return statements, source_files

    for path in sorted(captures_dir.iterdir()):
        if path.suffix.lower() not in {".json", ".jsonl"} or not path.is_file():
            continue
        source_files.append(path)
        statements.extend(_load_capture_file(path))
    return statements, source_files


def read_sync_status(kb: KnowledgeBase) -> dict[str, Any]:
    path = _sync_status_path(kb)
    if not path.is_file():
        return {
            "ok": False,
            "status_path": str(path),
            "last_result": None,
            "warnings": ["Sync has not been run."],
        }
    return json.loads(path.read_text(encoding="utf-8"))


def read_capture_status(kb: KnowledgeBase) -> dict[str, Any]:
    path = _capture_status_path(kb)
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        payload = {
            "ok": False,
            "status_path": str(path),
            "updated_at": "",
            "files": [],
            "warnings": ["Capture sync has not been run."],
        }
    inbox_files = [str(path) for path in _capture_file_paths(kb.inbox_captures_dir)]
    payload["inbox_files"] = inbox_files
    payload["pending_count"] = len(inbox_files)
    payload["summary"] = _capture_summary(payload.get("files", []))
    return payload


def validate_capture_path(path: Path) -> dict[str, Any]:
    path = Path(path)
    if path.is_dir():
        files = [validate_capture_path(child) for child in _capture_file_paths(path)]
        errors = [error for item in files for error in item["errors"]]
        return {
            "ok": not errors,
            "path": str(path),
            "records": sum(item["records"] for item in files),
            "errors": errors,
            "files": files,
        }
    if not path.is_file():
        return {"ok": False, "path": str(path), "records": 0, "errors": [f"{path}: file not found"], "files": []}
    if path.suffix.lower() not in {".json", ".jsonl"}:
        return {
            "ok": False,
            "path": str(path),
            "records": 0,
            "errors": [f"{path}: expected .json or .jsonl"],
            "files": [],
        }
    try:
        records = len(_load_capture_file(path))
    except ValueError as exc:
        return {"ok": False, "path": str(path), "records": 0, "errors": [str(exc)], "files": []}
    return {
        "ok": True,
        "path": str(path),
        "records": records,
        "errors": [],
        "files": [],
        "digest": _file_digest(path),
    }


def _load_capture_statements_lenient(captures_dir: Path) -> tuple[list[Statement], list[dict[str, Any]]]:
    statements: list[Statement] = []
    file_results: list[dict[str, Any]] = []
    if not captures_dir.exists():
        return statements, file_results

    for path in _capture_file_paths(captures_dir):
        digest = _file_digest(path)
        try:
            file_statements = _load_capture_file(path)
        except ValueError as exc:
            file_results.append(
                {
                    "source_file": str(path),
                    "digest": digest,
                    "status": "failed",
                    "records_seen": 0,
                    "notes_written": 0,
                    "duplicates_skipped": 0,
                    "error": str(exc),
                    "archived_to": "",
                    "last_seen_at": _now_utc(),
                }
            )
            continue
        statements.extend(file_statements)
        file_results.append(
            {
                "source_file": str(path),
                "digest": digest,
                "status": "processed",
                "records_seen": len(file_statements),
                "notes_written": 0,
                "duplicates_skipped": 0,
                "error": "",
                "archived_to": "",
                "last_seen_at": _now_utc(),
                "_statements": file_statements,
            }
        )
    return statements, file_results


def write_statement_note(kb: KnowledgeBase, statement: Statement) -> bool:
    platform = _safe_segment(statement.source_platform or "unknown", "unknown")
    role_id = _safe_segment(statement.role_id, "role")
    note_dir = kb.roles_dir / role_id / "statements" / platform
    note_dir.mkdir(parents=True, exist_ok=True)
    date_prefix = _date_prefix(statement.published_at or statement.captured_at)
    statement_segment = _safe_segment(statement.statement_id, "statement")
    if any(note_dir.glob(f"*-{platform}-{statement_segment}.md")):
        return False
    filename = f"{date_prefix}-{platform}-{statement_segment}.md"
    path = note_dir / filename
    if path.exists():
        return False
    path.write_text(_statement_markdown(statement), encoding="utf-8", newline="\n")
    return True


def _load_capture_file(path: Path) -> list[Statement]:
    if path.suffix.lower() == ".jsonl":
        statements: list[Statement] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSONL record: {exc}") from exc
            statements.append(_statement_from_capture(payload, f"{path}:{line_number}"))
        return statements

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON file: {exc}") from exc

    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
        items = payload["items"]
    else:
        items = [payload]
    return [_statement_from_capture(item, str(path)) for item in items]


def _statement_from_capture(payload: Any, source_label: str) -> Statement:
    if not isinstance(payload, dict):
        raise ValueError(f"{source_label}: capture record must be a JSON object")

    body = _first_text(payload, "body", "text", "content", "full_text")
    if not body:
        raise ValueError(f"{source_label}: capture record is missing body/text/content")

    source_url = _first_text(payload, "source_url", "url", "permalink", "link")
    platform = _safe_segment(
        _first_text(payload, "source_platform", "platform") or _platform_from_url(source_url) or "unknown",
        "unknown",
    )
    source_user_id = _first_text(payload, "source_user_id", "platform_user_id", "user_id", "username", "handle")
    source_author = _first_text(payload, "source_author", "author", "display_name", "name")
    role_id = _safe_identifier(
        _first_text(payload, "role_id") or "-".join(part for part in [platform, source_user_id or source_author] if part),
        "role",
    )
    statement_id = _safe_identifier(
        _first_text(payload, "statement_id", "id", "post_id", "external_id")
        or stable_statement_id(role_id, body, source_url),
        "stmt",
    )
    captured_at = _first_text(payload, "captured_at", "collected_at", "ingested_at") or _now_utc()
    title = _first_text(payload, "title") or _title_from_body(body)
    return Statement(
        statement_id=statement_id,
        role_id=role_id,
        source_type=_first_text(payload, "source_type") or "post",
        source_url=source_url,
        published_at=_first_text(payload, "published_at", "created_at", "posted_at"),
        captured_at=captured_at,
        title=title,
        body=body,
        symbols=split_list(payload.get("symbols")),
        topics=split_list(payload.get("topics")),
        stance=_first_text(payload, "stance") or "unclear",
        time_horizon=_first_text(payload, "time_horizon") or "unknown",
        confidence=_first_text(payload, "confidence") or "low",
        notes=_first_text(payload, "notes"),
        source_platform=platform,
        source_user_id=source_user_id,
        source_author=source_author,
    )


def _statement_markdown(statement: Statement) -> str:
    frontmatter = [
        "---",
        f"statement_id: {_inline(statement.statement_id)}",
        f"role_id: {_inline(statement.role_id)}",
        f"source_type: {_inline(statement.source_type)}",
        f"source_platform: {_inline(statement.source_platform)}",
        f"source_user_id: {_inline(statement.source_user_id)}",
        f"source_author: {_inline(statement.source_author)}",
        f"source_url: {_inline(statement.source_url)}",
        f"published_at: {_inline(statement.published_at)}",
        f"captured_at: {_inline(statement.captured_at)}",
        f"title: {_inline(statement.title)}",
        *_yaml_list("symbols", statement.symbols),
        *_yaml_list("topics", statement.topics),
        f"stance: {_inline(statement.stance)}",
        f"time_horizon: {_inline(statement.time_horizon)}",
        f"confidence: {_inline(statement.confidence)}",
        f"notes: {_inline(statement.notes)}",
        *_yaml_list(
            "tags",
            [
                "voicevault/statement",
                f"voicevault/platform/{statement.source_platform or 'unknown'}",
                f"voicevault/role/{statement.role_id}",
            ],
        ),
        "---",
    ]
    lines = [
        *frontmatter,
        "",
        f"# {statement.title}",
        "",
        statement.body.strip(),
        "",
        "## Source",
        "",
        f"- Platform: {statement.source_platform or 'unknown'}",
        f"- Author: {statement.source_author or statement.source_user_id or 'unknown'}",
        f"- URL: {statement.source_url or 'unknown'}",
    ]
    return "\n".join(lines).strip() + "\n"


def _yaml_list(key: str, values: list[str]) -> list[str]:
    if not values:
        return [f"{key}:"]
    lines = [f"{key}:"]
    lines.extend(f"  - {_inline(value)}" for value in values)
    return lines


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).replace("\r\n", "\n").strip()
        if text:
            return text
    return ""


def _inline(value: str) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def _title_from_body(body: str) -> str:
    collapsed = re.sub(r"\s+", " ", body).strip()
    return collapsed[:80] if collapsed else "Untitled statement"


def _platform_from_url(source_url: str) -> str:
    if not source_url:
        return ""
    host = urlparse(source_url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    known = {
        "x.com": "x",
        "twitter.com": "x",
        "weibo.com": "weibo",
        "xueqiu.com": "snowball",
    }
    return known.get(host, host.split(".")[0])


def _safe_identifier(value: str, fallback_prefix: str) -> str:
    raw = value.strip()
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", raw.lower()).strip("-_")
    if cleaned:
        return cleaned
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10] if raw else "unknown"
    return f"{fallback_prefix}_{digest}"


def _safe_segment(value: str, fallback: str) -> str:
    return _safe_identifier(value or fallback, fallback)


def _date_prefix(value: str) -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", value or "")
    return match.group(0) if match else "undated"


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_sync_status(kb: KnowledgeBase, result: SyncResult) -> None:
    path = _sync_status_path(kb)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ok": not result.errors,
        "status_path": str(path),
        "last_run_at": _now_utc(),
        "last_result": {
            "captures_seen": result.captures_seen,
            "notes_written": result.notes_written,
            "duplicates_skipped": result.duplicates_skipped,
            "statements_indexed": result.statements_indexed,
            "source_files": result.source_files,
            "errors": result.errors,
            "archived_files": result.archived_files,
            "capture_files": result.capture_files,
        },
        "warnings": ["One or more capture files failed to sync."] if result.errors else [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def _sync_status_path(kb: KnowledgeBase) -> Path:
    return kb.state_dir / "sync-status.json"


def _capture_status_path(kb: KnowledgeBase) -> Path:
    return kb.state_dir / "capture-status.json"


def _write_capture_status(kb: KnowledgeBase, files: list[dict[str, Any]]) -> None:
    path = _capture_status_path(kb)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ok": all(item["status"] == "processed" for item in files),
        "status_path": str(path),
        "updated_at": _now_utc(),
        "summary": _capture_summary(files),
        "files": files,
        "warnings": ["One or more capture files failed."] if any(item["status"] == "failed" for item in files) else [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def _capture_summary(files: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "processed": sum(1 for item in files if item.get("status") == "processed"),
        "failed": sum(1 for item in files if item.get("status") == "failed"),
        "total": len(files),
        "records_seen": sum(int(item.get("records_seen", 0)) for item in files),
        "notes_written": sum(int(item.get("notes_written", 0)) for item in files),
        "duplicates_skipped": sum(int(item.get("duplicates_skipped", 0)) for item in files),
    }


def _capture_file_paths(captures_dir: Path) -> list[Path]:
    if not captures_dir.exists():
        return []
    return [
        path
        for path in sorted(captures_dir.iterdir())
        if path.is_file() and path.suffix.lower() in {".json", ".jsonl"}
    ]


def _archive_processed_files(kb: KnowledgeBase, capture_files: list[dict[str, Any]]) -> list[str]:
    archive_dir = kb.inbox_archive_dir / _now_utc()[:10]
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived: list[str] = []
    for item in capture_files:
        if item["status"] != "processed":
            continue
        source_path = Path(item["source_file"])
        if not source_path.exists():
            continue
        target = _unique_archive_path(archive_dir / source_path.name, item["digest"])
        shutil.move(str(source_path), str(target))
        item["archived_to"] = str(target)
        archived.append(str(target))
    return archived


def _unique_archive_path(path: Path, digest: str) -> Path:
    if not path.exists():
        return path
    candidate = path.with_name(f"{path.stem}-{digest[:8]}{path.suffix}")
    counter = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{digest[:8]}-{counter}{path.suffix}")
        counter += 1
    return candidate


def _file_digest(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()
