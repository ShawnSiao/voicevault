from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .kb import KnowledgeBase


ACTION_RUNS_FILENAME = "action-runs.json"
ACTION_RUNS_LIMIT = 200
ACTION_RUN_STATUSES = {"completed", "failed"}
ACTION_RUN_RETRYABLE_TYPES = {"answer", "compare", "comparison_review"}


def record_action_run(
    kb: KnowledgeBase,
    *,
    action_type: str,
    status: str,
    payload: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    error: str = "",
    source: str = "local_api",
    action_id: str = "",
    started_at: str = "",
) -> dict[str, Any]:
    completed_at = _now_utc()
    normalized_action_type = str(action_type or "").strip() or "unknown"
    normalized_status = status if status in ACTION_RUN_STATUSES else "failed"
    normalized_result = _json_object(result)
    normalized_error = str(error or "").strip()
    if normalized_status == "failed" and not normalized_error:
        normalized_error = _fallback_error(normalized_action_type, normalized_result)
    run = {
        "schema_version": 1,
        "run_id": f"{normalized_action_type}:{completed_at}:{uuid4().hex[:8]}",
        "action_id": str(action_id or "").strip(),
        "action_type": normalized_action_type,
        "status": normalized_status,
        "retryable": _is_retryable(normalized_action_type, normalized_status),
        "source": str(source or "local_api").strip() or "local_api",
        "started_at": started_at or completed_at,
        "completed_at": completed_at,
        "resolved_by": "",
        "resolved_at": "",
        "payload": _json_object(payload),
        "result": normalized_result,
        "error": normalized_error,
    }
    existing_runs, _ = _read_runs(kb)
    _write_runs(kb, [run, *existing_runs][:ACTION_RUNS_LIMIT])
    return run


def read_action_run(kb: KnowledgeBase, run_id: str) -> dict[str, Any] | None:
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return None
    runs, _ = _read_runs(kb)
    for run in runs:
        if run["run_id"] == normalized_run_id:
            return run
    return None


def resolve_action_run(kb: KnowledgeBase, run_id: str, *, resolved_by: str) -> dict[str, Any] | None:
    normalized_run_id = str(run_id or "").strip()
    normalized_resolved_by = str(resolved_by or "").strip()
    if not normalized_run_id or not normalized_resolved_by:
        return None
    runs, _ = _read_runs(kb)
    resolved_at = _now_utc()
    updated: dict[str, Any] | None = None
    next_runs: list[dict[str, Any]] = []
    for run in runs:
        if run["run_id"] == normalized_run_id:
            run = {
                **run,
                "retryable": False,
                "resolved_by": normalized_resolved_by,
                "resolved_at": resolved_at,
            }
            updated = run
        next_runs.append(run)
    if updated is None:
        return None
    _write_runs(kb, next_runs)
    return updated


def read_action_runs(kb: KnowledgeBase, *, status_filter: str = "all") -> dict[str, Any]:
    runs, errors = _read_runs(kb)
    if status_filter != "all" and status_filter not in ACTION_RUN_STATUSES:
        raise ValueError(f"Unsupported action run status filter: {status_filter}")
    filtered_runs = runs if status_filter == "all" else [run for run in runs if run["status"] == status_filter]
    summary = _summarize_runs(runs, errors)
    return {
        "schema_version": 1,
        "ok": bool(summary["malformed"] == 0),
        "root": str(kb.root),
        "status_path": str(_action_runs_path(kb)),
        "status_filter": status_filter,
        "summary": summary,
        "runs": filtered_runs,
        "errors": errors,
    }


def _read_runs(kb: KnowledgeBase) -> tuple[list[dict[str, Any]], list[str]]:
    path = _action_runs_path(kb)
    if not path.is_file():
        return [], []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [], [f"Invalid action runs JSON: {exc}"]
    if not isinstance(payload, dict):
        return [], [f"Action runs must be a JSON object: {path}"]
    raw_runs = payload.get("runs", [])
    if not isinstance(raw_runs, list):
        return [], [f"Action runs must contain a runs list: {path}"]
    runs: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, raw_run in enumerate(raw_runs):
        if not isinstance(raw_run, dict):
            errors.append(f"Action run {index} must be a JSON object: {path}")
            continue
        runs.append(_normalize_run(raw_run))
    return runs[:ACTION_RUNS_LIMIT], errors


def _write_runs(kb: KnowledgeBase, runs: list[dict[str, Any]]) -> None:
    path = _action_runs_path(kb)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": _now_utc(),
                "runs": runs[:ACTION_RUNS_LIMIT],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )


def _normalize_run(payload: dict[str, Any]) -> dict[str, Any]:
    status = str(payload.get("status") or "")
    if status not in ACTION_RUN_STATUSES:
        status = "failed"
    action_type = str(payload.get("action_type") or "")
    result = _json_object(payload.get("result"))
    error = str(payload.get("error") or "").strip()
    if status == "failed" and not error:
        error = _fallback_error(action_type or "unknown", result)
    return {
        "schema_version": 1,
        "run_id": str(payload.get("run_id") or ""),
        "action_id": str(payload.get("action_id") or ""),
        "action_type": action_type,
        "status": status,
        "retryable": _bool_or_default(payload.get("retryable"), _is_retryable(action_type, status)),
        "source": str(payload.get("source") or ""),
        "started_at": str(payload.get("started_at") or ""),
        "completed_at": str(payload.get("completed_at") or ""),
        "resolved_by": str(payload.get("resolved_by") or ""),
        "resolved_at": str(payload.get("resolved_at") or ""),
        "payload": _json_object(payload.get("payload")),
        "result": result,
        "error": error,
    }


def _summarize_runs(runs: list[dict[str, Any]], errors: list[str]) -> dict[str, int]:
    return {
        "total": len(runs),
        "completed": sum(1 for run in runs if run["status"] == "completed"),
        "failed": sum(1 for run in runs if run["status"] == "failed"),
        "retryable_failed": sum(1 for run in runs if run["status"] == "failed" and run.get("retryable")),
        "malformed": len(errors),
    }


def _json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return {str(key): str(item) for key, item in value.items()}
    return dict(value)


def _fallback_error(action_type: str, result: dict[str, Any]) -> str:
    result_error = str(result.get("error") or "").strip()
    if result_error:
        return result_error
    llm_status = str(result.get("llm_status") or "").strip()
    if llm_status:
        return f"{action_type} failed with llm_status={llm_status}"
    return f"{action_type} failed without error detail"


def _action_runs_path(kb: KnowledgeBase):
    return kb.state_dir / ACTION_RUNS_FILENAME


def _is_retryable(action_type: str, status: str) -> bool:
    return status == "failed" and action_type in ACTION_RUN_RETRYABLE_TYPES


def _bool_or_default(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
