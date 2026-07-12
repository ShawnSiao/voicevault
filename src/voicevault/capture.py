from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def build_capture_record(
    *,
    role_id: str,
    platform: str,
    text: str,
    url: str = "",
    title: str = "",
    author: str = "",
    platform_user_id: str = "",
    published_at: str = "",
    captured_at: str = "",
    symbols: list[str] | None = None,
    topics: list[str] | None = None,
    statement_id: str = "",
    stance: str = "unclear",
    time_horizon: str = "unknown",
    confidence: str = "low",
    notes: str = "",
) -> dict[str, Any]:
    if not text.strip():
        raise ValueError("Capture text is required.")
    record = {
        "role_id": role_id.strip(),
        "platform": platform.strip() or "unknown",
        "platform_user_id": platform_user_id.strip(),
        "author": author.strip(),
        "url": url.strip(),
        "published_at": published_at.strip(),
        "captured_at": captured_at.strip() or _now_utc(),
        "title": title.strip(),
        "text": text.strip(),
        "symbols": symbols or [],
        "topics": topics or [],
        "stance": stance.strip() or "unclear",
        "time_horizon": time_horizon.strip() or "unknown",
        "confidence": confidence.strip() or "low",
        "notes": notes.strip(),
    }
    if statement_id.strip():
        record["statement_id"] = statement_id.strip()
    return record


def append_capture_record(path: Path, record: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return path


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
