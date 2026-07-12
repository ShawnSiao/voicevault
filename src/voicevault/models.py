from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Statement:
    statement_id: str
    role_id: str
    source_type: str
    source_url: str
    published_at: str
    captured_at: str
    title: str
    body: str
    symbols: list[str]
    topics: list[str]
    stance: str
    time_horizon: str
    confidence: str
    notes: str
    source_platform: str = ""
    source_user_id: str = ""
    source_author: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement_id": self.statement_id,
            "role_id": self.role_id,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "published_at": self.published_at,
            "captured_at": self.captured_at,
            "title": self.title,
            "body": self.body,
            "symbols": self.symbols,
            "topics": self.topics,
            "stance": self.stance,
            "time_horizon": self.time_horizon,
            "confidence": self.confidence,
            "notes": self.notes,
            "source_platform": self.source_platform,
            "source_user_id": self.source_user_id,
            "source_author": self.source_author,
        }


@dataclass(frozen=True)
class Event:
    event_id: str
    title: str
    date: str
    summary: str
    symbols: list[str]
    topics: list[str]
    source_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "title": self.title,
            "date": self.date,
            "summary": self.summary,
            "symbols": self.symbols,
            "topics": self.topics,
            "source_notes": self.source_notes,
        }


@dataclass(frozen=True)
class AnalysisOutput:
    json_path: Any
    markdown_path: Any
