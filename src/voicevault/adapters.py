from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .capture import build_capture_record
from .kb import KnowledgeBase


LOCAL_JSONL_ADAPTERS = {"local-jsonl", "local_jsonl"}


def validate_adapter_config(kb: KnowledgeBase, source: dict[str, Any]) -> dict[str, Any]:
    source_id = str(source.get("source_id") or "")
    adapter = str(source.get("adapter") or "manual").strip().lower()
    base = {
        "source_id": source_id,
        "adapter": adapter or "manual",
        "role_id": str(source.get("role_id") or ""),
        "platform": str(source.get("platform") or ""),
        "status": "ready",
        "ok": True,
        "message": "Adapter config is ready.",
        "record_count": 0,
        "details": {},
    }
    if source.get("status") == "disabled":
        base.update(
            {
                "status": "disabled",
                "message": "Source is disabled; adapter config was not checked.",
            }
        )
        return base
    if source.get("status") == "malformed":
        return _adapter_validation_failed(base, "Source config is malformed.")
    if adapter in {"manual", ""}:
        base["message"] = "Manual adapter is ready; provide --text when running the source."
        return base
    if adapter in LOCAL_JSONL_ADAPTERS:
        try:
            records = run_local_jsonl_adapter(kb, source)
        except Exception as exc:
            return _adapter_validation_failed(base, str(exc))
        base["record_count"] = len(records)
        base["message"] = f"local-jsonl adapter is ready with {len(records)} record(s)."
        base["details"] = {"input_path": str(_resolve_input_path(kb, _adapter_input_path(source)))}
        return base
    return _adapter_validation_failed(base, f"Unsupported source adapter: {adapter}")


def run_local_jsonl_adapter(
    kb: KnowledgeBase,
    source: dict[str, Any],
    *,
    title: str = "",
    source_url: str = "",
    published_at: str = "",
    captured_at: str = "",
    symbols: list[str] | None = None,
    topics: list[str] | None = None,
    stance: str = "unclear",
    time_horizon: str = "unknown",
    confidence: str = "low",
    notes: str = "",
) -> list[dict[str, Any]]:
    adapter_config = source.get("adapter_config") or {}
    if not isinstance(adapter_config, dict):
        raise ValueError("Source adapter_config must be a JSON object.")
    input_path = _resolve_input_path(kb, str(adapter_config.get("input_path") or adapter_config.get("path") or ""))
    raw_items = _read_json_records(input_path)
    records = [
        build_record_from_source_item(
            source,
            item,
            title=title,
            source_url=source_url,
            published_at=published_at,
            captured_at=captured_at,
            symbols=symbols,
            topics=topics,
            stance=stance,
            time_horizon=time_horizon,
            confidence=confidence,
            notes=notes,
        )
        for item in raw_items
    ]
    if not records:
        raise ValueError(f"Adapter input has no records: {input_path}")
    return records


def build_record_from_source_item(
    source: dict[str, Any],
    item: dict[str, Any],
    *,
    title: str = "",
    source_url: str = "",
    published_at: str = "",
    captured_at: str = "",
    symbols: list[str] | None = None,
    topics: list[str] | None = None,
    stance: str = "unclear",
    time_horizon: str = "unknown",
    confidence: str = "low",
    notes: str = "",
) -> dict[str, Any]:
    item_symbols = _list_value(item.get("symbols"))
    item_topics = _list_value(item.get("topics"))
    return build_capture_record(
        role_id=str(source["role_id"]),
        platform=str(source["platform"]),
        text=str(item.get("text") or item.get("body") or item.get("content") or item.get("full_text") or ""),
        url=str(item.get("source_url") or item.get("url") or item.get("permalink") or item.get("link") or source_url or source.get("source_url") or ""),
        title=str(item.get("title") or title or ""),
        author=str(item.get("author") or item.get("source_author") or item.get("display_name") or item.get("name") or source.get("display_name") or ""),
        platform_user_id=str(item.get("platform_user_id") or item.get("source_user_id") or item.get("user_id") or item.get("username") or item.get("handle") or ""),
        published_at=str(item.get("published_at") or item.get("created_at") or item.get("posted_at") or published_at or ""),
        captured_at=str(item.get("captured_at") or item.get("collected_at") or item.get("ingested_at") or captured_at or ""),
        symbols=item_symbols or symbols or list(source.get("symbols") or []),
        topics=item_topics or topics or list(source.get("topics") or []),
        statement_id=str(item.get("statement_id") or ""),
        stance=str(item.get("stance") or stance),
        time_horizon=str(item.get("time_horizon") or time_horizon),
        confidence=str(item.get("confidence") or confidence),
        notes=str(item.get("notes") or notes or source.get("notes") or ""),
    )


def _read_json_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"Adapter input not found: {path}")
    if path.suffix.lower() == ".jsonl":
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid adapter JSONL record at {path}:{line_number}: {exc}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"Adapter JSONL record must be an object at {path}:{line_number}")
            records.append(item)
        return records
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        if not all(isinstance(item, dict) for item in payload):
            raise ValueError(f"Adapter JSON list must contain objects: {path}")
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        records = payload["records"]
        if not all(isinstance(item, dict) for item in records):
            raise ValueError(f"Adapter JSON records must contain objects: {path}")
        return records
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        records = payload["items"]
        if not all(isinstance(item, dict) for item in records):
            raise ValueError(f"Adapter JSON items must contain objects: {path}")
        return records
    if isinstance(payload, dict):
        return [payload]
    raise ValueError(f"Adapter JSON must be an object, object list, records object, or items object: {path}")


def _resolve_input_path(kb: KnowledgeBase, value: str) -> Path:
    if not value.strip():
        raise ValueError("local-jsonl adapter requires adapter_config.input_path.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = kb.root / path
    return path.resolve()


def _list_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).replace(";", ",").split(",") if part.strip()]


def _adapter_input_path(source: dict[str, Any]) -> str:
    adapter_config = source.get("adapter_config") or {}
    if not isinstance(adapter_config, dict):
        raise ValueError("Source adapter_config must be a JSON object.")
    return str(adapter_config.get("input_path") or adapter_config.get("path") or "")


def _adapter_validation_failed(base: dict[str, Any], message: str) -> dict[str, Any]:
    failed = dict(base)
    failed.update({"status": "failed", "ok": False, "message": message})
    return failed
