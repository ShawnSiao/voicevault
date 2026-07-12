from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters import LOCAL_JSONL_ADAPTERS, run_local_jsonl_adapter, validate_adapter_config
from .capture import append_capture_record, build_capture_record
from .kb import KnowledgeBase


SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
SOURCE_STATUS_FILENAME = "source-status.json"
SOURCE_STATUS_LIMIT = 100


def create_source(
    kb: KnowledgeBase,
    *,
    source_id: str,
    role_id: str,
    platform: str,
    source_url: str = "",
    display_name: str = "",
    adapter: str = "manual",
    adapter_config: dict[str, Any] | None = None,
    symbols: list[str] | None = None,
    topics: list[str] | None = None,
    tags: list[str] | None = None,
    cadence: str = "",
    notes: str = "",
    enabled: bool = True,
    overwrite: bool = False,
) -> dict[str, Any]:
    normalized_source_id = source_id.strip()
    _validate_source_id(normalized_source_id)
    if not role_id.strip():
        raise ValueError("Source role ID is required.")
    if not platform.strip():
        raise ValueError("Source platform is required.")
    if adapter_config is not None and not isinstance(adapter_config, dict):
        raise ValueError("Source adapter_config must be a JSON object.")
    path = _source_path(kb, normalized_source_id)
    if path.exists() and not overwrite:
        raise FileExistsError(f"Source config already exists: {path}")
    kb.sources_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_id": normalized_source_id,
        "role_id": role_id.strip(),
        "display_name": display_name.strip() or normalized_source_id,
        "platform": platform.strip(),
        "source_url": source_url.strip(),
        "adapter": adapter.strip() or "manual",
        "adapter_config": adapter_config or {},
        "enabled": enabled,
        "status": "active" if enabled else "disabled",
        "symbols": symbols or [],
        "topics": topics or [],
        "tags": tags or [],
        "cadence": cadence.strip(),
        "notes": notes.strip(),
        "created_at": _now_utc(),
        "updated_at": _now_utc(),
        "config_path": str(path),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return payload


def list_sources(kb: KnowledgeBase) -> list[dict[str, Any]]:
    if not kb.sources_dir.exists():
        return []
    sources: list[dict[str, Any]] = []
    for path in sorted(kb.sources_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            sources.append(_malformed_source(path))
            continue
        if not isinstance(payload, dict):
            sources.append(_malformed_source(path))
            continue
        payload["config_path"] = str(path)
        payload.setdefault("status", "active" if payload.get("enabled", True) else "disabled")
        payload.setdefault("symbols", [])
        payload.setdefault("topics", [])
        payload.setdefault("tags", [])
        payload.setdefault("adapter_config", {})
        sources.append(payload)
    return sorted(sources, key=lambda item: str(item.get("source_id") or ""))


def summarize_sources(sources: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(sources),
        "active": sum(1 for item in sources if item.get("status") == "active"),
        "disabled": sum(1 for item in sources if item.get("status") == "disabled"),
        "malformed": sum(1 for item in sources if item.get("status") == "malformed"),
    }


def validate_source_adapters(kb: KnowledgeBase) -> dict[str, Any]:
    rows = [validate_adapter_config(kb, source) for source in list_sources(kb)]
    summary = {
        "total": len(rows),
        "checked": sum(1 for item in rows if item.get("status") != "disabled"),
        "ready": sum(1 for item in rows if item.get("status") == "ready"),
        "disabled": sum(1 for item in rows if item.get("status") == "disabled"),
        "failed": sum(1 for item in rows if item.get("status") == "failed"),
    }
    return {
        "ok": bool(summary["checked"] > 0 and summary["failed"] == 0),
        "summary": summary,
        "sources": rows,
    }


def read_source_status(kb: KnowledgeBase) -> dict[str, Any]:
    runs, errors = _read_source_runs(kb)
    sources = list_sources(kb)
    summary = _summarize_source_runs(sources, runs, errors)
    latest_by_source = _latest_runs_by_source(runs)
    source_rows = [
        {
            "source_id": str(source.get("source_id") or ""),
            "status": str(source.get("status") or ""),
            "role_id": str(source.get("role_id") or ""),
            "platform": str(source.get("platform") or ""),
            "adapter": str(source.get("adapter") or ""),
            "config_path": str(source.get("config_path") or ""),
            "latest_run": latest_by_source.get(str(source.get("source_id") or "")),
        }
        for source in sources
    ]
    return {
        "ok": bool(
            summary["active_sources"] > 0
            and summary["active_without_runs"] == 0
            and summary["active_failed_latest"] == 0
            and summary["malformed"] == 0
        ),
        "status_path": str(_source_status_path(kb)),
        "summary": summary,
        "sources": source_rows,
        "runs": runs,
        "errors": errors,
    }


def record_source_run_error(
    kb: KnowledgeBase,
    source_id: str,
    error: str,
    *,
    dry_run: bool = False,
    out: Path | None = None,
) -> dict[str, Any]:
    normalized_source_id = source_id.strip()
    _validate_source_id(normalized_source_id)
    source = _get_source_for_status(kb, normalized_source_id)
    capture_path = out or kb.inbox_captures_dir / f"source-{normalized_source_id}.jsonl"
    return _record_source_run(
        kb,
        source_id=normalized_source_id,
        source=source,
        status="failed",
        dry_run=dry_run,
        written=0,
        capture_path=capture_path,
        error=error,
    )


def run_source(
    kb: KnowledgeBase,
    source_id: str,
    *,
    text: str = "",
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
    dry_run: bool = False,
    out: Path | None = None,
) -> dict[str, Any]:
    normalized_source_id = source_id.strip()
    source = get_source(kb, normalized_source_id)
    if source.get("status") != "active":
        raise ValueError(f"Source is not active: {source_id}")
    adapter = str(source.get("adapter") or "manual").strip().lower()
    if adapter in {"manual", ""}:
        records = [
            build_capture_record(
                role_id=str(source["role_id"]),
                platform=str(source["platform"]),
                text=text,
                url=source_url or str(source.get("source_url") or ""),
                title=title,
                author=str(source.get("display_name") or ""),
                published_at=published_at,
                captured_at=captured_at,
                symbols=symbols if symbols is not None else list(source.get("symbols") or []),
                topics=topics if topics is not None else list(source.get("topics") or []),
                stance=stance,
                time_horizon=time_horizon,
                confidence=confidence,
                notes=notes or str(source.get("notes") or ""),
            )
        ]
    elif adapter in LOCAL_JSONL_ADAPTERS:
        records = run_local_jsonl_adapter(
            kb,
            source,
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
    else:
        raise ValueError(f"Unsupported source adapter: {adapter}")
    capture_path = out or kb.inbox_captures_dir / f"source-{normalized_source_id}.jsonl"
    if not dry_run:
        for record in records:
            append_capture_record(capture_path, record)
    written = 0 if dry_run else len(records)
    run = _record_source_run(
        kb,
        source_id=normalized_source_id,
        source=source,
        status="dry_run" if dry_run else "written",
        dry_run=dry_run,
        written=written,
        capture_path=capture_path,
        error="",
    )
    return {
        "source_id": normalized_source_id,
        "status": run["status"],
        "dry_run": dry_run,
        "capture_path": str(capture_path),
        "written": written,
        "record_count": len(records),
        "record": records[0],
        "records": records,
        "run": run,
    }


def get_source(kb: KnowledgeBase, source_id: str) -> dict[str, Any]:
    _validate_source_id(source_id.strip())
    path = _source_path(kb, source_id)
    if not path.is_file():
        raise FileNotFoundError(f"Source config not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Source config must be a JSON object: {path}")
    payload["config_path"] = str(path)
    payload.setdefault("status", "active" if payload.get("enabled", True) else "disabled")
    payload.setdefault("symbols", [])
    payload.setdefault("topics", [])
    payload.setdefault("tags", [])
    payload.setdefault("adapter_config", {})
    return payload


def _source_path(kb: KnowledgeBase, source_id: str) -> Path:
    return kb.sources_dir / f"{source_id.strip()}.json"


def _source_status_path(kb: KnowledgeBase) -> Path:
    return kb.state_dir / SOURCE_STATUS_FILENAME


def _record_source_run(
    kb: KnowledgeBase,
    *,
    source_id: str,
    source: dict[str, Any] | None,
    status: str,
    dry_run: bool,
    written: int,
    capture_path: Path,
    error: str,
) -> dict[str, Any]:
    ran_at = _now_utc()
    run = {
        "run_id": f"{source_id}:{ran_at}",
        "source_id": source_id,
        "status": status,
        "adapter": str((source or {}).get("adapter") or ""),
        "role_id": str((source or {}).get("role_id") or ""),
        "platform": str((source or {}).get("platform") or ""),
        "dry_run": dry_run,
        "written": written,
        "capture_path": str(capture_path),
        "ran_at": ran_at,
        "error": error,
    }
    existing_runs, _ = _read_source_runs(kb)
    runs = [run, *existing_runs][:SOURCE_STATUS_LIMIT]
    path = _source_status_path(kb)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": ran_at,
                "runs": runs,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return run


def _read_source_runs(kb: KnowledgeBase) -> tuple[list[dict[str, Any]], list[str]]:
    path = _source_status_path(kb)
    if not path.is_file():
        return [], []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [], [f"Invalid source status JSON: {exc}"]
    if not isinstance(payload, dict):
        return [], [f"Source status must be a JSON object: {path}"]
    raw_runs = payload.get("runs", [])
    if not isinstance(raw_runs, list):
        return [], [f"Source status runs must be a list: {path}"]
    runs: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, raw_run in enumerate(raw_runs):
        if not isinstance(raw_run, dict):
            errors.append(f"Source status run {index} must be a JSON object: {path}")
            continue
        runs.append(_normalize_source_run(raw_run))
    return runs[:SOURCE_STATUS_LIMIT], errors


def _normalize_source_run(payload: dict[str, Any]) -> dict[str, Any]:
    status = str(payload.get("status") or "")
    if status not in {"written", "dry_run", "failed"}:
        status = "failed"
    return {
        "run_id": str(payload.get("run_id") or ""),
        "source_id": str(payload.get("source_id") or ""),
        "status": status,
        "adapter": str(payload.get("adapter") or ""),
        "role_id": str(payload.get("role_id") or ""),
        "platform": str(payload.get("platform") or ""),
        "dry_run": bool(payload.get("dry_run", False)),
        "written": int(payload.get("written") or 0),
        "capture_path": str(payload.get("capture_path") or ""),
        "ran_at": str(payload.get("ran_at") or ""),
        "error": str(payload.get("error") or ""),
    }


def _summarize_source_runs(
    sources: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    errors: list[str],
) -> dict[str, int]:
    active_source_ids = {
        str(source.get("source_id") or "")
        for source in sources
        if source.get("status") == "active" and source.get("source_id")
    }
    latest_by_source = _latest_runs_by_source(runs)
    return {
        "total": len(runs),
        "written": sum(1 for run in runs if run.get("status") == "written"),
        "dry_run": sum(1 for run in runs if run.get("status") == "dry_run"),
        "failed": sum(1 for run in runs if run.get("status") == "failed"),
        "active_sources": len(active_source_ids),
        "active_without_runs": sum(1 for source_id in active_source_ids if source_id not in latest_by_source),
        "active_failed_latest": sum(
            1 for source_id in active_source_ids if latest_by_source.get(source_id, {}).get("status") == "failed"
        ),
        "malformed": len(errors),
    }


def _latest_runs_by_source(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        source_id = str(run.get("source_id") or "")
        if source_id and source_id not in latest:
            latest[source_id] = run
    return latest


def _get_source_for_status(kb: KnowledgeBase, source_id: str) -> dict[str, Any] | None:
    try:
        return get_source(kb, source_id)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return None


def _validate_source_id(source_id: str) -> None:
    if not SOURCE_ID_PATTERN.match(source_id):
        raise ValueError("Source ID must start with a letter or number and contain only letters, numbers, dots, underscores, or hyphens.")


def _malformed_source(path: Path) -> dict[str, Any]:
    return {
        "source_id": path.stem,
        "role_id": "",
        "display_name": path.stem,
        "platform": "",
        "source_url": "",
        "adapter": "",
        "adapter_config": {},
        "enabled": False,
        "status": "malformed",
        "symbols": [],
        "topics": [],
        "tags": [],
        "config_path": str(path),
    }


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
