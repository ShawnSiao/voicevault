from __future__ import annotations

from typing import Any

from .answer import list_answer_exports
from .kb import KnowledgeBase


ANSWER_QUALITY_SCHEMA_VERSION = 1
PASS_STATUS = "pass"
REVIEW_STATUS = "review"
FAIL_STATUS = "fail"


def audit_answer_quality(kb: KnowledgeBase, *, limit: int = 50) -> dict[str, Any]:
    items = [_audit_item(answer) for answer in list_answer_exports(kb)]
    limited_items = sorted(items, key=_sort_key)[: max(1, limit)]
    summary = _summary(limited_items)
    return {
        "schema_version": ANSWER_QUALITY_SCHEMA_VERSION,
        "ok": summary["review"] == 0 and summary["failed"] == 0,
        "root": str(kb.root),
        "summary": summary,
        "items": limited_items,
    }


def _audit_item(answer: dict[str, Any]) -> dict[str, Any]:
    checks = _checks(answer)
    failed_checks = [check["id"] for check in checks if check["status"] != PASS_STATUS]
    status = _status(checks)
    score = _score(checks)
    role_id = _repair_role_id(answer)
    query = str(answer.get("query") or "").strip()
    payload = {
        "query": query,
        "role_id": role_id,
        "auto_route": not bool(role_id),
        "limit": 5,
    }
    return {
        "schema_version": ANSWER_QUALITY_SCHEMA_VERSION,
        "status": status,
        "score": score,
        "query": query,
        "answer_json": str(answer.get("answer_json") or ""),
        "answer_markdown": str(answer.get("answer_markdown") or ""),
        "selected_role_id": str(answer.get("selected_role_id") or ""),
        "role_id": role_id,
        "evidence_count": int(answer.get("evidence_count") or 0),
        "citation_count": int(answer.get("citation_count") or 0),
        "failed_checks": failed_checks,
        "checks": checks,
        "recommended_endpoint": "/api/answer" if query and status != PASS_STATUS else "",
        "payload": payload if query and status != PASS_STATUS else {},
    }


def _checks(answer: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _check(
            "deliverable",
            answer.get("status") == "deliverable",
            fail_status=FAIL_STATUS,
            message="Answer export must satisfy the v1 deliverable contract.",
        ),
        _check(
            "evidence",
            int(answer.get("evidence_count") or 0) > 0 and int(answer.get("citation_count") or 0) > 0,
            fail_status=FAIL_STATUS,
            message="Answer must include cited local evidence.",
        ),
        _check(
            "key_points",
            bool(answer.get("key_points")),
            fail_status=REVIEW_STATUS,
            message="Answer should expose key points for scanning and review.",
        ),
        _check(
            "role_answer",
            _has_role_answer(answer),
            fail_status=REVIEW_STATUS,
            message="Answer should include structured role_answer for role-specific product use.",
        ),
    ]


def _check(check_id: str, ok: bool, *, fail_status: str, message: str) -> dict[str, str]:
    return {
        "id": check_id,
        "status": PASS_STATUS if ok else fail_status,
        "message": message,
    }


def _has_role_answer(answer: dict[str, Any]) -> bool:
    role_answer = answer.get("role_answer") if isinstance(answer.get("role_answer"), dict) else {}
    return bool(
        role_answer
        and role_answer.get("schema_version") == 1
        and str(role_answer.get("answer") or "").strip()
        and str(role_answer.get("source_scope") or "").strip()
        and isinstance(role_answer.get("evidence_refs"), list)
        and role_answer.get("evidence_refs")
    )


def _status(checks: list[dict[str, str]]) -> str:
    statuses = {check["status"] for check in checks}
    if FAIL_STATUS in statuses:
        return FAIL_STATUS
    if REVIEW_STATUS in statuses:
        return REVIEW_STATUS
    return PASS_STATUS


def _score(checks: list[dict[str, str]]) -> int:
    score = 100
    for check in checks:
        if check["status"] == FAIL_STATUS:
            score -= 35
        elif check["status"] == REVIEW_STATUS:
            score -= 15
    return max(0, score)


def _repair_role_id(answer: dict[str, Any]) -> str:
    selected = str(answer.get("selected_role_id") or "").strip()
    if selected:
        return selected
    role_answer = answer.get("role_answer") if isinstance(answer.get("role_answer"), dict) else {}
    role_id = str(role_answer.get("role_id") or "").strip()
    if role_id:
        return role_id
    filters = answer.get("filters") if isinstance(answer.get("filters"), dict) else {}
    return str(filters.get("role_id") or "").strip()


def _summary(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(items),
        "passed": sum(1 for item in items if item.get("status") == PASS_STATUS),
        "review": sum(1 for item in items if item.get("status") == REVIEW_STATUS),
        "failed": sum(1 for item in items if item.get("status") == FAIL_STATUS),
        "missing_role_answer": sum(1 for item in items if "role_answer" in item.get("failed_checks", [])),
        "missing_evidence": sum(1 for item in items if "evidence" in item.get("failed_checks", [])),
    }


def _sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    status_rank = {FAIL_STATUS: 0, REVIEW_STATUS: 1, PASS_STATUS: 2}.get(str(item.get("status") or ""), 3)
    return (status_rank, -int(item.get("score") or 0), str(item.get("answer_json") or ""))
