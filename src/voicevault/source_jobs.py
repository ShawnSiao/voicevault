from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .kb import KnowledgeBase
from .sources import get_source, list_sources, record_source_run_error, run_source


SOURCE_JOBS_FILENAME = "source-jobs.json"
SOURCE_JOBS_LIMIT = 200
SOURCE_JOB_STATUSES = {"pending", "completed", "failed"}


def enqueue_source_jobs(
    kb: KnowledgeBase,
    *,
    source_id: str | None = None,
    due_at: str = "",
) -> dict[str, Any]:
    sources = _sources_to_enqueue(kb, source_id=source_id)
    jobs, errors = _read_jobs(kb)
    pending_sources = {job["source_id"] for job in jobs if job["status"] == "pending"}
    created: list[dict[str, Any]] = []
    for source in sources:
        current_source_id = str(source.get("source_id") or "")
        if current_source_id in pending_sources:
            continue
        created.append(_new_source_job(source, due_at=due_at))
    if created:
        _write_jobs(kb, [*created, *jobs][:SOURCE_JOBS_LIMIT])
    all_jobs = [*created, *jobs][:SOURCE_JOBS_LIMIT]
    return {
        "root": str(kb.root),
        "status_path": str(_source_jobs_path(kb)),
        "created": len(created),
        "jobs": created,
        "summary": _summarize_jobs(all_jobs, errors),
        "errors": errors,
    }


def read_source_job_status(kb: KnowledgeBase, *, status_filter: str = "all") -> dict[str, Any]:
    jobs, errors = _read_jobs(kb)
    if status_filter != "all" and status_filter not in SOURCE_JOB_STATUSES:
        raise ValueError(f"Unsupported source job status filter: {status_filter}")
    filtered_jobs = jobs if status_filter == "all" else [job for job in jobs if job["status"] == status_filter]
    summary = _summarize_jobs(jobs, errors)
    return {
        "ok": bool(summary["failed"] == 0 and summary["malformed"] == 0),
        "root": str(kb.root),
        "status_path": str(_source_jobs_path(kb)),
        "status_filter": status_filter,
        "summary": summary,
        "jobs": filtered_jobs,
        "errors": errors,
    }


def drain_source_jobs(kb: KnowledgeBase, *, limit: int = 0, dry_run: bool = False) -> dict[str, Any]:
    if limit < 0:
        raise ValueError("Source job drain limit must be zero or greater.")
    jobs, _ = _read_jobs(kb)
    pending_jobs = [job for job in jobs if job["status"] == "pending"]
    if limit:
        pending_jobs = pending_jobs[:limit]
    drained_jobs = [_drain_source_job(kb, job, dry_run=dry_run) for job in pending_jobs]
    status = read_source_job_status(kb)
    completed = sum(1 for job in drained_jobs if job["status"] == "completed")
    failed = sum(1 for job in drained_jobs if job["status"] == "failed")
    return {
        "root": str(kb.root),
        "status_path": status["status_path"],
        "dry_run": dry_run,
        "limit": limit,
        "processed": len(drained_jobs),
        "completed": completed,
        "failed": failed,
        "jobs": drained_jobs,
        "summary": status["summary"],
        "errors": status["errors"],
        "ok": bool(failed == 0 and not status["errors"]),
    }


def get_source_job(kb: KnowledgeBase, job_id: str) -> dict[str, Any]:
    jobs, _ = _read_jobs(kb)
    for job in jobs:
        if job["job_id"] == job_id:
            return job
    raise FileNotFoundError(f"Source job not found: {job_id}")


def complete_source_job(kb: KnowledgeBase, job_id: str, run: dict[str, Any]) -> dict[str, Any]:
    def update(job: dict[str, Any]) -> dict[str, Any]:
        attempts = _safe_int(job.get("attempts")) + 1
        return {
            **job,
            "status": "completed",
            "attempts": attempts,
            "completed_at": _now_utc(),
            "last_attempt_at": _now_utc(),
            "last_error": "",
            "run_id": str(run.get("run_id") or ""),
            "capture_path": str(run.get("capture_path") or ""),
        }

    return _update_source_job(kb, job_id, update)


def fail_source_job(kb: KnowledgeBase, job_id: str, error: str) -> dict[str, Any]:
    def update(job: dict[str, Any]) -> dict[str, Any]:
        attempts = _safe_int(job.get("attempts")) + 1
        return {
            **job,
            "status": "failed",
            "attempts": attempts,
            "last_attempt_at": _now_utc(),
            "last_error": error,
        }

    return _update_source_job(kb, job_id, update)


def retry_source_job(kb: KnowledgeBase, job_id: str, *, due_at: str = "") -> dict[str, Any]:
    def update(job: dict[str, Any]) -> dict[str, Any]:
        if job["status"] != "failed":
            raise ValueError("Only failed source jobs can be retried.")
        return {
            **job,
            "status": "pending",
            "due_at": due_at or _now_utc(),
            "completed_at": "",
            "last_error": "",
            "run_id": "",
            "capture_path": "",
        }

    return _update_source_job(kb, job_id, update)


def _drain_source_job(kb: KnowledgeBase, job: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    job_id = str(job.get("job_id") or "")
    source_id = str(job.get("source_id") or "")
    try:
        if not source_id:
            raise ValueError("Source job has no source_id.")
        result = run_source(kb, source_id, dry_run=dry_run)
        updated_job = complete_source_job(kb, job_id, result["run"])
        return {
            "job_id": job_id,
            "source_id": source_id,
            "status": "completed",
            "dry_run": dry_run,
            "record_count": result["record_count"],
            "written": result["written"],
            "capture_path": result["capture_path"],
            "run_id": result["run"]["run_id"],
            "job": updated_job,
        }
    except Exception as exc:
        error = str(exc)
        run = _record_drain_error(kb, source_id, error, dry_run=dry_run)
        updated_job = fail_source_job(kb, job_id, error)
        return {
            "job_id": job_id,
            "source_id": source_id,
            "status": "failed",
            "dry_run": dry_run,
            "record_count": 0,
            "written": 0,
            "capture_path": str(run.get("capture_path") or ""),
            "run_id": str(run.get("run_id") or ""),
            "error": error,
            "job": updated_job,
        }


def _record_drain_error(kb: KnowledgeBase, source_id: str, error: str, *, dry_run: bool) -> dict[str, Any]:
    if not source_id:
        return {}
    try:
        return record_source_run_error(kb, source_id, error, dry_run=dry_run)
    except Exception:
        return {}


def _sources_to_enqueue(kb: KnowledgeBase, *, source_id: str | None) -> list[dict[str, Any]]:
    if source_id:
        source = get_source(kb, source_id)
        if source.get("status") != "active":
            raise ValueError(f"Source is not active: {source_id}")
        return [source]
    return [source for source in list_sources(kb) if source.get("status") == "active"]


def _new_source_job(source: dict[str, Any], *, due_at: str) -> dict[str, Any]:
    created_at = _now_utc()
    source_id = str(source.get("source_id") or "")
    return {
        "job_id": f"{source_id}:{created_at}:{uuid4().hex[:8]}",
        "source_id": source_id,
        "status": "pending",
        "adapter": str(source.get("adapter") or ""),
        "role_id": str(source.get("role_id") or ""),
        "platform": str(source.get("platform") or ""),
        "created_at": created_at,
        "due_at": due_at or created_at,
        "attempts": 0,
        "last_attempt_at": "",
        "completed_at": "",
        "last_error": "",
        "run_id": "",
        "capture_path": "",
    }


def _update_source_job(kb: KnowledgeBase, job_id: str, update: Any) -> dict[str, Any]:
    jobs, _ = _read_jobs(kb)
    updated_job: dict[str, Any] | None = None
    updated_jobs: list[dict[str, Any]] = []
    for job in jobs:
        if job["job_id"] == job_id:
            updated_job = update(job)
            updated_jobs.append(updated_job)
        else:
            updated_jobs.append(job)
    if updated_job is None:
        raise FileNotFoundError(f"Source job not found: {job_id}")
    _write_jobs(kb, updated_jobs)
    return updated_job


def _read_jobs(kb: KnowledgeBase) -> tuple[list[dict[str, Any]], list[str]]:
    path = _source_jobs_path(kb)
    if not path.is_file():
        return [], []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [], [f"Invalid source jobs JSON: {exc}"]
    if not isinstance(payload, dict):
        return [], [f"Source jobs must be a JSON object: {path}"]
    raw_jobs = payload.get("jobs", [])
    if not isinstance(raw_jobs, list):
        return [], [f"Source jobs must contain a jobs list: {path}"]
    jobs: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, raw_job in enumerate(raw_jobs):
        if not isinstance(raw_job, dict):
            errors.append(f"Source job {index} must be a JSON object: {path}")
            continue
        jobs.append(_normalize_job(raw_job))
    return jobs[:SOURCE_JOBS_LIMIT], errors


def _write_jobs(kb: KnowledgeBase, jobs: list[dict[str, Any]]) -> None:
    path = _source_jobs_path(kb)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": _now_utc(),
                "jobs": jobs[:SOURCE_JOBS_LIMIT],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )


def _normalize_job(payload: dict[str, Any]) -> dict[str, Any]:
    status = str(payload.get("status") or "")
    if status not in SOURCE_JOB_STATUSES:
        status = "failed"
    return {
        "job_id": str(payload.get("job_id") or ""),
        "source_id": str(payload.get("source_id") or ""),
        "status": status,
        "adapter": str(payload.get("adapter") or ""),
        "role_id": str(payload.get("role_id") or ""),
        "platform": str(payload.get("platform") or ""),
        "created_at": str(payload.get("created_at") or ""),
        "due_at": str(payload.get("due_at") or ""),
        "attempts": _safe_int(payload.get("attempts")),
        "last_attempt_at": str(payload.get("last_attempt_at") or ""),
        "completed_at": str(payload.get("completed_at") or ""),
        "last_error": str(payload.get("last_error") or ""),
        "run_id": str(payload.get("run_id") or ""),
        "capture_path": str(payload.get("capture_path") or ""),
    }


def _summarize_jobs(jobs: list[dict[str, Any]], errors: list[str]) -> dict[str, int]:
    return {
        "total": len(jobs),
        "pending": sum(1 for job in jobs if job["status"] == "pending"),
        "completed": sum(1 for job in jobs if job["status"] == "completed"),
        "failed": sum(1 for job in jobs if job["status"] == "failed"),
        "malformed": len(errors),
    }


def _source_jobs_path(kb: KnowledgeBase) -> Path:
    return kb.state_dir / SOURCE_JOBS_FILENAME


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
