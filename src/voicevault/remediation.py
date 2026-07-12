from __future__ import annotations

import re
from typing import Any

from .action_runs import read_action_runs
from .answer import list_answer_exports
from .answer_quality import audit_answer_quality
from .answer_regression import audit_answer_regression
from .comparison import list_comparison_exports
from .kb import KnowledgeBase
from .source_jobs import read_source_job_status


REMEDIATION_SCHEMA_VERSION = 1
READY_STATUS = "ready"
BLOCKED_STATUS = "blocked"


def build_remediation_queue(kb: KnowledgeBase, *, limit: int = 20) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    items.extend(_action_run_items(kb))
    items.extend(_answer_items(kb))
    items.extend(_answer_quality_items(kb))
    items.extend(_answer_regression_items(kb))
    items.extend(_comparison_items(kb))
    items.extend(_source_job_items(kb))
    deduped = _dedupe_items(items)
    sorted_items = sorted(deduped, key=_sort_key)[: max(1, limit)]
    summary = _summary(sorted_items)
    return {
        "schema_version": REMEDIATION_SCHEMA_VERSION,
        "ok": summary["blocked"] == 0,
        "root": str(kb.root),
        "summary": summary,
        "items": sorted_items,
    }


def _action_run_items(kb: KnowledgeBase) -> list[dict[str, Any]]:
    status = read_action_runs(kb)
    items: list[dict[str, Any]] = []
    for run in status.get("runs", []):
        if not isinstance(run, dict):
            continue
        if run.get("status") != "failed" or not run.get("retryable"):
            continue
        run_id = str(run.get("run_id") or "").strip()
        if not run_id:
            continue
        payload = run.get("payload") if isinstance(run.get("payload"), dict) else {}
        query = str(payload.get("query") or "").strip()
        items.append(
            _item(
                item_id=f"retry-action-run-{_slug(run_id)}",
                status=READY_STATUS,
                severity="high",
                phase="recovery",
                action_type="retry_action_run",
                label="Retry failed action",
                action=f"Retry failed {run.get('action_type') or 'action'} run.",
                reason=str(run.get("error") or "Action run failed and can be retried."),
                endpoint="/api/action-runs/retry",
                payload={"run_id": run_id},
                source={"kind": "action_run", "run_id": run_id, "query": query},
            )
        )
    return items


def _answer_items(kb: KnowledgeBase) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for answer in list_answer_exports(kb):
        if not isinstance(answer, dict):
            continue
        status = str(answer.get("status") or "").strip()
        if status == "deliverable":
            continue
        query = str(answer.get("query") or "").strip()
        answer_json = str(answer.get("answer_json") or "").strip()
        if query:
            items.append(
                _item(
                    item_id=f"rerun-answer-{_slug(answer_json or query)}",
                    status=READY_STATUS,
                    severity="medium",
                    phase="research_quality",
                    action_type="rerun_answer",
                    label="Rerun answer",
                    action=f"Regenerate non-deliverable answer for {query}.",
                    reason=f"Answer export status is {status or 'unknown'}, so it is not release quality.",
                    endpoint="/api/answer",
                    payload={
                        "query": query,
                        "role_id": str(answer.get("selected_role_id") or ""),
                        "auto_route": not bool(str(answer.get("selected_role_id") or "").strip()),
                        "limit": 5,
                    },
                    source={"kind": "answer_export", "path": answer_json, "status": status},
                )
            )
        else:
            items.append(
                _item(
                    item_id=f"inspect-answer-{_slug(answer_json or 'unknown-answer')}",
                    status=BLOCKED_STATUS,
                    severity="medium",
                    phase="research_quality",
                    action_type="inspect_answer",
                    label="Inspect answer",
                    action="Inspect malformed answer export before rerun.",
                    reason=f"Answer export status is {status or 'unknown'} and query is missing.",
                    command=f"voicevault answers list --kb {kb.root} --status invalid --json",
                    payload={},
                    source={"kind": "answer_export", "path": answer_json, "status": status},
                )
            )
    return items


def _answer_quality_items(kb: KnowledgeBase) -> list[dict[str, Any]]:
    audit = audit_answer_quality(kb)
    items: list[dict[str, Any]] = []
    for answer in audit.get("items", []):
        if not isinstance(answer, dict):
            continue
        if answer.get("status") == "pass":
            continue
        failed_checks = answer.get("failed_checks") if isinstance(answer.get("failed_checks"), list) else []
        if "role_answer" not in failed_checks:
            continue
        query = str(answer.get("query") or "").strip()
        if not query:
            continue
        items.append(
            _item(
                item_id=f"improve-answer-{_slug(answer.get('answer_json') or query)}",
                status=READY_STATUS,
                severity="medium",
                phase="answer_quality",
                action_type="improve_answer",
                label="Improve answer",
                action=f"Regenerate answer quality metadata for {query}.",
                reason="Answer is deliverable, but missing structured role_answer for role-specific UI use.",
                endpoint=str(answer.get("recommended_endpoint") or "/api/answer"),
                payload=answer.get("payload") if isinstance(answer.get("payload"), dict) else {"query": query, "auto_route": True, "limit": 5},
                source={"kind": "answer_quality", "path": str(answer.get("answer_json") or ""), "failed_checks": failed_checks},
            )
        )
    return items


def _answer_regression_items(kb: KnowledgeBase) -> list[dict[str, Any]]:
    audit = audit_answer_regression(kb)
    items: list[dict[str, Any]] = []
    for question in audit.get("items", []):
        if not isinstance(question, dict):
            continue
        if question.get("status") == "pass":
            continue
        query = str(question.get("query") or "").strip()
        if not query:
            continue
        failed_checks = question.get("failed_checks") if isinstance(question.get("failed_checks"), list) else []
        payload = question.get("payload") if isinstance(question.get("payload"), dict) else {"query": query, "auto_route": True, "limit": 5}
        items.append(
            _item(
                item_id=f"fix-answer-regression-{_slug(question.get('id') or query)}",
                status=READY_STATUS,
                severity="high",
                phase="answer_regression",
                action_type="fix_answer_regression",
                label="Fix regression answer",
                action=f"Regenerate fixed regression answer for {query}.",
                reason="Fixed answer regression question is failing and should be repaired before handoff.",
                endpoint=str(question.get("recommended_endpoint") or "/api/answer"),
                payload=payload,
                source={
                    "kind": "answer_regression",
                    "id": str(question.get("id") or ""),
                    "status": str(question.get("status") or ""),
                    "failed_checks": failed_checks,
                },
            )
        )
    return items


def _comparison_items(kb: KnowledgeBase) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for comparison in list_comparison_exports(kb):
        if not isinstance(comparison, dict):
            continue
        review_status = str(comparison.get("review_status") or "").strip()
        status = str(comparison.get("status") or "").strip()
        if status == "deliverable" and review_status == "adopted":
            continue
        query = str(comparison.get("query") or "").strip()
        comparison_json = str(comparison.get("comparison_json") or "").strip()
        if review_status == "draft" and query:
            items.append(
                _item(
                    item_id=f"review-comparison-{_slug(comparison_json or query)}",
                    status=READY_STATUS,
                    severity="high",
                    phase="review",
                    action_type="review_comparison",
                    label="Review comparison",
                    action=f"Review and adopt or reject draft comparison for {query}.",
                    reason="Draft comparisons do not pass release handoff until reviewed.",
                    endpoint="/api/comparison/review",
                    payload={
                        "query": query,
                        "status": "adopted",
                        "reviewer": "local-ui",
                        "notes": "Adopted from remediation queue.",
                    },
                    source={"kind": "comparison_export", "path": comparison_json, "status": review_status},
                )
            )
        else:
            items.append(
                _item(
                    item_id=f"inspect-comparison-{_slug(comparison_json or query or 'unknown-comparison')}",
                    status=BLOCKED_STATUS if not query else READY_STATUS,
                    severity="medium",
                    phase="review",
                    action_type="inspect_comparison",
                    label="Inspect comparison",
                    action=f"Inspect comparison export with status {review_status or status or 'unknown'}.",
                    reason="Comparison export is not adopted and may block release quality.",
                    command=f"voicevault comparisons list --kb {kb.root} --json",
                    payload={"query": query} if query else {},
                    source={"kind": "comparison_export", "path": comparison_json, "status": review_status or status},
                )
            )
    return items


def _source_job_items(kb: KnowledgeBase) -> list[dict[str, Any]]:
    status = read_source_job_status(kb)
    items: list[dict[str, Any]] = []
    for job in status.get("jobs", []):
        if not isinstance(job, dict):
            continue
        if job.get("status") != "failed":
            continue
        job_id = str(job.get("job_id") or "").strip()
        if not job_id:
            continue
        items.append(
            _item(
                item_id=f"retry-source-job-{_slug(job_id)}",
                status=READY_STATUS,
                severity="high",
                phase="source_jobs",
                action_type="retry_source_job",
                label="Retry source job",
                action=f"Move failed source job for {job.get('source_id') or 'source'} back to pending.",
                reason=str(job.get("last_error") or "Source job failed."),
                command=f"voicevault sources retry --kb {kb.root} --job {job_id} --json",
                payload={"job_id": job_id},
                source={"kind": "source_job", "job_id": job_id, "source_id": str(job.get("source_id") or "")},
            )
        )
    return items


def _item(
    *,
    item_id: str,
    status: str,
    severity: str,
    phase: str,
    action_type: str,
    label: str,
    action: str,
    reason: str,
    payload: dict[str, Any],
    source: dict[str, Any],
    endpoint: str = "",
    command: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": REMEDIATION_SCHEMA_VERSION,
        "id": item_id,
        "status": status,
        "severity": severity,
        "phase": phase,
        "action_type": action_type,
        "label": label,
        "action": action,
        "reason": reason,
        "endpoint": endpoint,
        "command": command,
        "payload": dict(payload),
        "source": dict(source),
    }


def _dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        item_id = str(item.get("id") or "")
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        deduped.append(item)
    return deduped


def _summary(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(items),
        "ready": sum(1 for item in items if item.get("status") == READY_STATUS),
        "blocked": sum(1 for item in items if item.get("status") == BLOCKED_STATUS),
        "high": sum(1 for item in items if item.get("severity") == "high"),
        "medium": sum(1 for item in items if item.get("severity") == "medium"),
        "action_run_retries": sum(1 for item in items if item.get("action_type") == "retry_action_run"),
        "answer_repairs": sum(1 for item in items if item.get("action_type") == "rerun_answer"),
        "answer_quality_repairs": sum(1 for item in items if item.get("action_type") == "improve_answer"),
        "answer_regression_repairs": sum(1 for item in items if item.get("action_type") == "fix_answer_regression"),
        "comparison_reviews": sum(1 for item in items if item.get("action_type") == "review_comparison"),
        "source_job_repairs": sum(1 for item in items if item.get("action_type") == "retry_source_job"),
    }


def _sort_key(item: dict[str, Any]) -> tuple[int, int, str, str]:
    severity_rank = {"high": 0, "medium": 1, "low": 2}.get(str(item.get("severity") or ""), 3)
    status_rank = {READY_STATUS: 0, BLOCKED_STATUS: 1}.get(str(item.get("status") or ""), 2)
    return (status_rank, severity_rank, str(item.get("phase") or ""), str(item.get("id") or ""))


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip().lower()).strip("-")
    return slug[:96] or "item"
