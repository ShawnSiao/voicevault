from __future__ import annotations

from pathlib import Path
from typing import Any

from .importers import load_event
from .kb import KnowledgeBase


def create_event(
    kb: KnowledgeBase,
    event_id: str,
    title: str,
    date: str,
    symbols: list[str],
    topics: list[str],
    summary: str,
    overwrite: bool = False,
) -> Path:
    kb.events_dir.mkdir(parents=True, exist_ok=True)
    path = kb.events_dir / f"{event_id}.md"
    if path.exists() and not overwrite:
        raise FileExistsError(f"Event already exists: {path}")
    path.write_text(_event_markdown(event_id, title, date, symbols, topics, summary), encoding="utf-8", newline="\n")
    return path


def default_export_dir(kb: KnowledgeBase, event_id: str) -> Path:
    return kb.exports_dir / event_id


def list_events(kb: KnowledgeBase) -> list[dict[str, Any]]:
    if not kb.events_dir.is_dir():
        return []
    events: list[dict[str, Any]] = []
    for path in sorted(kb.events_dir.glob("*.md")):
        event = load_event(path)
        events.append(
            {
                "event_id": event.event_id,
                "title": event.title,
                "date": event.date,
                "symbols": event.symbols,
                "topics": event.topics,
                "path": str(path),
            }
        )
    events.sort(key=lambda item: item["event_id"])
    events.sort(key=lambda item: item["date"], reverse=True)
    return events


def _event_markdown(event_id: str, title: str, date: str, symbols: list[str], topics: list[str], summary: str) -> str:
    return "\n".join(
        [
            "---",
            f"event_id: {event_id}",
            f"date: {date}",
            "symbols:",
            *[f"  - {symbol}" for symbol in symbols],
            "topics:",
            *[f"  - {topic}" for topic in topics],
            "---",
            "",
            f"# {title}",
            "",
            summary.strip() or "补充事件背景。",
            "",
        ]
    )
