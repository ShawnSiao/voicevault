from __future__ import annotations

import csv
import hashlib
from datetime import date
from pathlib import Path
from typing import Any

from .kb import KnowledgeBase
from .markdown import first_heading, read_markdown
from .models import Event, Statement

REQUIRED_CSV_COLUMNS = [
    "statement_id",
    "role_id",
    "source_type",
    "source_url",
    "published_at",
    "captured_at",
    "title",
    "body",
    "symbols",
    "topics",
    "stance",
    "time_horizon",
    "confidence",
    "notes",
]


def split_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    parts = str(value).replace(";", ",").split(",")
    return [part.strip() for part in parts if part.strip()]


def stable_statement_id(role_id: str, body: str, source_url: str) -> str:
    digest = hashlib.sha1(f"{role_id}|{source_url}|{body}".encode("utf-8")).hexdigest()[:12]
    return f"stmt_{digest}"


def load_statements_from_kb(kb: KnowledgeBase) -> list[Statement]:
    statements: list[Statement] = []
    if not kb.roles_dir.exists():
        return statements
    for role_dir in sorted(path for path in kb.roles_dir.iterdir() if path.is_dir()):
        statements.extend(load_role_statements(role_dir))
    return statements


def load_role_statements(role_dir: Path) -> list[Statement]:
    statements: list[Statement] = []
    csv_path = role_dir / "statements.csv"
    if csv_path.exists():
        statements.extend(_load_csv_statements(csv_path, role_dir.name))

    theses_dir = role_dir / "theses"
    if theses_dir.exists():
        for path in sorted(theses_dir.glob("*.md")):
            statements.append(_load_markdown_statement(path, role_dir.name, "thesis"))

    statements_dir = role_dir / "statements"
    if statements_dir.exists():
        for path in sorted(statements_dir.rglob("*.md")):
            statements.append(_load_markdown_statement(path, role_dir.name, "post"))
    return statements


def load_event(path: Path) -> Event:
    metadata, body = read_markdown(path)
    title = str(metadata.get("title") or first_heading(body, path.stem))
    return Event(
        event_id=str(metadata.get("event_id") or path.stem),
        title=title,
        date=str(metadata.get("date") or ""),
        summary=body.strip(),
        symbols=split_list(metadata.get("symbols")),
        topics=split_list(metadata.get("topics")),
        source_notes=str(metadata.get("source_notes") or ""),
    )


def _load_csv_statements(path: Path, default_role_id: str) -> list[Statement]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in REQUIRED_CSV_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing required CSV columns: {', '.join(missing)}")
        return [_statement_from_row(row, default_role_id) for row in reader if any(row.values())]


def _statement_from_row(row: dict[str, str], default_role_id: str) -> Statement:
    role_id = (row.get("role_id") or default_role_id).strip()
    body = (row.get("body") or "").strip()
    source_url = (row.get("source_url") or "").strip()
    statement_id = (row.get("statement_id") or "").strip() or stable_statement_id(role_id, body, source_url)
    return Statement(
        statement_id=statement_id,
        role_id=role_id,
        source_type=(row.get("source_type") or "post").strip(),
        source_url=source_url,
        published_at=(row.get("published_at") or "").strip(),
        captured_at=(row.get("captured_at") or "").strip(),
        title=(row.get("title") or "").strip(),
        body=body,
        symbols=split_list(row.get("symbols")),
        topics=split_list(row.get("topics")),
        stance=(row.get("stance") or "unclear").strip() or "unclear",
        time_horizon=(row.get("time_horizon") or "unknown").strip() or "unknown",
        confidence=(row.get("confidence") or "low").strip() or "low",
        notes=(row.get("notes") or "").strip(),
        source_platform=(row.get("source_platform") or "").strip(),
        source_user_id=(row.get("source_user_id") or row.get("platform_user_id") or "").strip(),
        source_author=(row.get("source_author") or row.get("author") or "").strip(),
    )


def _load_thesis_statement(path: Path, role_id: str) -> Statement:
    return _load_markdown_statement(path, role_id, "thesis")


def _load_markdown_statement(path: Path, default_role_id: str, default_source_type: str) -> Statement:
    metadata, body = read_markdown(path)
    source_url = str(metadata.get("source_url") or "")
    title = str(metadata.get("title") or first_heading(body, path.stem))
    statement_body = _statement_body(body)
    role_id = str(metadata.get("role_id") or default_role_id)
    return Statement(
        statement_id=str(metadata.get("statement_id") or stable_statement_id(role_id, statement_body, source_url)),
        role_id=role_id,
        source_type=str(metadata.get("source_type") or default_source_type),
        source_url=source_url,
        published_at=str(metadata.get("published_at") or ""),
        captured_at=str(metadata.get("captured_at") or date.today().isoformat()),
        title=title,
        body=statement_body,
        symbols=split_list(metadata.get("symbols")),
        topics=split_list(metadata.get("topics")),
        stance=str(metadata.get("stance") or "unclear"),
        time_horizon=str(metadata.get("time_horizon") or "unknown"),
        confidence=str(metadata.get("confidence") or "low"),
        notes=str(metadata.get("notes") or ""),
        source_platform=str(metadata.get("source_platform") or metadata.get("platform") or ""),
        source_user_id=str(metadata.get("source_user_id") or metadata.get("platform_user_id") or ""),
        source_author=str(metadata.get("source_author") or metadata.get("author") or ""),
    )


def _statement_body(body: str) -> str:
    lines = body.replace("\r\n", "\n").strip().splitlines()
    if lines and lines[0].strip().startswith("# "):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]

    content: list[str] = []
    for line in lines:
        if line.strip() in {"## Source", "## Notes", "## Research Notes"}:
            break
        content.append(line)
    return "\n".join(content).strip()
