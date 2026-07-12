from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from pathlib import Path
from typing import Any

from .answer import list_answer_exports
from .kb import KnowledgeBase


ANSWER_REGRESSION_SCHEMA_VERSION = 1
ANSWER_REGRESSION_MIN_QUESTIONS = 4
PASS_STATUS = "pass"
REVIEW_STATUS = "review"
FAIL_STATUS = "fail"


def default_answer_regression_suite_path(kb: KnowledgeBase) -> Path:
    return kb.content_dir / "evaluations" / "questions.json"


def default_answer_regression_changelog_path(kb: KnowledgeBase) -> Path:
    return kb.content_dir / "evaluations" / "questions.changelog.jsonl"


def load_answer_regression_suite(kb: KnowledgeBase, *, suite_path: Path | None = None) -> dict[str, Any]:
    path = suite_path or default_answer_regression_suite_path(kb)
    if not path.exists():
        return {
            "schema_version": ANSWER_REGRESSION_SCHEMA_VERSION,
            "root": str(kb.root),
            "suite_path": str(path),
            "questions": [],
            "errors": [],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _suite_error(kb, path, f"questions.json must be readable JSON: {exc}")
    if not isinstance(payload, dict):
        return _suite_error(kb, path, "questions.json must contain a JSON object.")
    if payload.get("schema_version") != ANSWER_REGRESSION_SCHEMA_VERSION:
        return _suite_error(kb, path, "schema_version must be 1.")
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list):
        return _suite_error(kb, path, "questions must be a list.")

    questions: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_questions):
        if not isinstance(raw, dict):
            errors.append(f"questions[{index}] must be an object")
            continue
        question = _question_payload(raw, index)
        if not question["query"]:
            errors.append(f"questions[{index}].query must be a nonempty string")
            continue
        if question["id"] in seen_ids:
            errors.append(f"questions[{index}].id must be unique")
            continue
        seen_ids.add(question["id"])
        questions.append(question)
    return {
        "schema_version": ANSWER_REGRESSION_SCHEMA_VERSION,
        "root": str(kb.root),
        "suite_path": str(path),
        "questions": questions,
        "errors": errors,
    }


def load_answer_regression_changelog(kb: KnowledgeBase, *, limit: int = 50) -> dict[str, Any]:
    path = default_answer_regression_changelog_path(kb)
    if not path.exists():
        return {
            "schema_version": ANSWER_REGRESSION_SCHEMA_VERSION,
            "root": str(kb.root),
            "changelog_path": str(path),
            "changes": [],
            "errors": [],
        }
    changes: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {
            "schema_version": ANSWER_REGRESSION_SCHEMA_VERSION,
            "root": str(kb.root),
            "changelog_path": str(path),
            "changes": [],
            "errors": [f"questions.changelog.jsonl must be readable: {exc}"],
        }
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {index + 1} must be JSON: {exc}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"line {index + 1} must contain a JSON object")
            continue
        changes.append(_change_payload(payload))
    limited = changes[-max(1, limit) :]
    return {
        "schema_version": ANSWER_REGRESSION_SCHEMA_VERSION,
        "root": str(kb.root),
        "changelog_path": str(path),
        "changes": limited,
        "errors": errors,
    }


def upsert_answer_regression_question(kb: KnowledgeBase, question_payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(question_payload, dict):
        raise ValueError("question payload must be a JSON object")
    suite = load_answer_regression_suite(kb)
    if suite["errors"]:
        raise ValueError("; ".join(suite["errors"]))

    questions = list(suite["questions"])
    question = _question_payload(question_payload, len(questions))
    if not question["query"]:
        raise ValueError("query is required")
    if not question["expected_role_id"]:
        question["expected_role_id"] = question["role_id"]

    now = _utc_now()
    actor = _actor(question_payload.get("updated_by"))
    replaced = False
    before: dict[str, Any] | None = None
    for index, existing in enumerate(questions):
        if existing["id"] == question["id"]:
            before = existing
            question["created_at"] = str(existing.get("created_at") or now).strip()
            question["updated_at"] = now
            question["updated_by"] = actor
            questions[index] = question
            replaced = True
            break
    if not replaced:
        question["created_at"] = str(question_payload.get("created_at") or now).strip()
        question["updated_at"] = now
        question["updated_by"] = actor
        questions.append(question)

    saved_suite = _write_answer_regression_suite(kb, questions)
    change = _append_answer_regression_change(
        kb,
        action="update" if replaced else "create",
        question=question,
        before=before,
        actor=actor,
        changed_at=now,
    )
    return {
        "schema_version": ANSWER_REGRESSION_SCHEMA_VERSION,
        "root": str(kb.root),
        "suite_path": saved_suite["suite_path"],
        "question": question,
        "suite": saved_suite,
        "change": change,
    }


def delete_answer_regression_question(kb: KnowledgeBase, question_id: str, *, updated_by: str = "local-ui") -> dict[str, Any]:
    normalized_id = str(question_id or "").strip()
    if not normalized_id:
        raise ValueError("question id is required")
    suite = load_answer_regression_suite(kb)
    if suite["errors"]:
        raise ValueError("; ".join(suite["errors"]))

    before = next((question for question in suite["questions"] if question["id"] == normalized_id), None)
    questions = [question for question in suite["questions"] if question["id"] != normalized_id]
    if len(questions) == len(suite["questions"]):
        raise ValueError("answer regression question not found")

    saved_suite = _write_answer_regression_suite(kb, questions)
    now = _utc_now()
    actor = _actor(updated_by)
    change = _append_answer_regression_change(
        kb,
        action="delete",
        question=before or {"id": normalized_id},
        before=before,
        actor=actor,
        changed_at=now,
    )
    return {
        "schema_version": ANSWER_REGRESSION_SCHEMA_VERSION,
        "root": str(kb.root),
        "suite_path": saved_suite["suite_path"],
        "deleted_id": normalized_id,
        "suite": saved_suite,
        "change": change,
    }


def export_answer_regression_suite(
    kb: KnowledgeBase,
    *,
    out_path: Path | None = None,
    suite_path: Path | None = None,
) -> dict[str, Any]:
    suite = load_answer_regression_suite(kb, suite_path=suite_path)
    payload = {
        "schema_version": ANSWER_REGRESSION_SCHEMA_VERSION,
        "kind": "answer_regression_suite",
        "ok": not suite["errors"],
        "root": str(kb.root),
        "suite_path": suite["suite_path"],
        "exported_at": _utc_now(),
        "question_count": len(suite["questions"]),
        "questions": suite["questions"],
        "errors": suite["errors"],
    }
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        payload["export_path"] = str(out_path)
    return payload


def import_answer_regression_suite(
    kb: KnowledgeBase,
    suite_input: dict[str, Any] | str | Path,
    *,
    dry_run: bool = True,
    updated_by: str = "local-import",
) -> dict[str, Any]:
    actor = _actor(updated_by)
    incoming, input_path, input_errors = _load_import_payload(suite_input)
    existing_suite = load_answer_regression_suite(kb)
    errors = list(input_errors)
    if existing_suite["errors"]:
        errors.extend(existing_suite["errors"])

    raw_questions = incoming.get("questions") if isinstance(incoming, dict) else None
    if not isinstance(raw_questions, list):
        errors.append("import payload questions must be a list")
        raw_questions = []

    incoming_questions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_questions):
        if not isinstance(raw, dict):
            errors.append(f"questions[{index}] must be an object")
            continue
        question = _question_payload(raw, index)
        if not question["query"]:
            errors.append(f"questions[{index}].query must be a nonempty string")
            continue
        if question["id"] in seen_ids:
            errors.append(f"questions[{index}].id must be unique")
            continue
        seen_ids.add(question["id"])
        incoming_questions.append(question)

    existing_by_id = {question["id"]: question for question in existing_suite["questions"]}
    now = _utc_now()
    planned: list[dict[str, Any]] = []
    summary = {"create": 0, "update": 0, "unchanged": 0, "errors": len(errors)}

    for question in incoming_questions:
        before = existing_by_id.get(question["id"])
        if before is None:
            action = "create"
            next_question = dict(question)
            next_question["created_at"] = str(question.get("created_at") or now).strip()
            next_question["updated_at"] = now
            next_question["updated_by"] = actor
        else:
            next_question = dict(question)
            next_question["created_at"] = str(before.get("created_at") or question.get("created_at") or now).strip()
            if _questions_equal(before, next_question):
                action = "unchanged"
                next_question = before
            else:
                action = "update"
                next_question["updated_at"] = now
                next_question["updated_by"] = actor
        summary[action] += 1
        planned.append(
            {
                "action": action,
                "question_id": question["id"],
                "question": next_question,
                "before": before,
            }
        )

    applied = False
    changes: list[dict[str, Any]] = []
    suite = existing_suite
    if not dry_run and not errors:
        applied = True
        planned_by_id = {item["question_id"]: item for item in planned if item["action"] in {"create", "update", "unchanged"}}
        merged: list[dict[str, Any]] = []
        handled: set[str] = set()
        for existing in existing_suite["questions"]:
            plan = planned_by_id.get(existing["id"])
            if plan:
                merged.append(plan["question"])
                handled.add(existing["id"])
            else:
                merged.append(existing)
        for plan in planned:
            if plan["action"] == "create" and plan["question_id"] not in handled:
                merged.append(plan["question"])
                handled.add(plan["question_id"])
        suite = _write_answer_regression_suite(kb, merged)
        for plan in planned:
            if plan["action"] not in {"create", "update"}:
                continue
            changes.append(
                _append_answer_regression_change(
                    kb,
                    action=f"import_{plan['action']}",
                    question=plan["question"],
                    before=plan["before"],
                    actor=actor,
                    changed_at=now,
                )
            )

    return {
        "schema_version": ANSWER_REGRESSION_SCHEMA_VERSION,
        "ok": not errors,
        "root": str(kb.root),
        "input_path": str(input_path) if input_path else "",
        "suite_path": existing_suite["suite_path"],
        "dry_run": dry_run,
        "applied": applied,
        "updated_by": actor,
        "summary": summary,
        "planned_changes": planned,
        "changes": changes,
        "suite": suite,
        "errors": errors,
    }


def audit_answer_regression(kb: KnowledgeBase, *, suite_path: Path | None = None, limit: int = 100) -> dict[str, Any]:
    suite = load_answer_regression_suite(kb, suite_path=suite_path)
    answer_exports = list_answer_exports(kb)
    answers_by_query = _answers_by_query(answer_exports)
    items = [_audit_question(question, answers_by_query) for question in suite["questions"]]
    sorted_items = sorted(items, key=_sort_key)[: max(1, limit)]
    summary = _summary(sorted_items, suite["errors"])
    return {
        "schema_version": ANSWER_REGRESSION_SCHEMA_VERSION,
        "ok": not suite["errors"] and summary["review"] == 0 and summary["failed"] == 0,
        "root": str(kb.root),
        "suite_path": suite["suite_path"],
        "changelog_path": str(default_answer_regression_changelog_path(kb)),
        "summary": summary,
        "items": sorted_items,
        "recent_changes": load_answer_regression_changelog(kb, limit=12)["changes"],
        "errors": suite["errors"],
    }


def audit_answer_regression_coverage(
    kb: KnowledgeBase,
    *,
    min_questions: int = ANSWER_REGRESSION_MIN_QUESTIONS,
) -> dict[str, Any]:
    audit = audit_answer_regression(kb, limit=10000)
    summary = dict(audit["summary"])
    missing_source_url = sum(1 for item in audit["items"] if not str(item.get("source_url") or "").strip())
    missing_rationale = sum(1 for item in audit["items"] if not str(item.get("rationale") or "").strip())
    missing_updated_by = sum(1 for item in audit["items"] if not str(item.get("updated_by") or "").strip())
    missing_timestamps = sum(
        1
        for item in audit["items"]
        if not str(item.get("created_at") or "").strip() or not str(item.get("updated_at") or "").strip()
    )
    missing_provenance = sum(1 for item in audit["items"] if _missing_provenance_fields(item))
    summary.update(
        {
            "min_questions": max(0, int(min_questions)),
            "missing_source_url": missing_source_url,
            "missing_rationale": missing_rationale,
            "missing_updated_by": missing_updated_by,
            "missing_timestamps": missing_timestamps,
            "missing_provenance": missing_provenance,
        }
    )
    checks = [
        _coverage_check(
            "suite_valid",
            not audit["errors"],
            "Answer regression suite is valid JSON with schema_version 1.",
            f"Run: voicevault evaluations answers --kb {kb.root} --json, then fix questions.json errors.",
        ),
        _coverage_check(
            "minimum_questions",
            summary["total"] >= summary["min_questions"],
            f"{summary['total']} fixed answer regression question(s); requires {summary['min_questions']}.",
            f"Run: voicevault evaluations import --kb {kb.root} --input <answer-regression-suite.json> --yes --json, or add questions in the local UI.",
        ),
        _coverage_check(
            "all_questions_passed",
            summary["total"] > 0 and summary["passed"] == summary["total"] and summary["review"] == 0 and summary["failed"] == 0,
            f"{summary['passed']} passed, {summary['review']} review, {summary['failed']} failed.",
            f"Run: voicevault evaluations answers --kb {kb.root} --json, then rerun failed questions from the UI or voicevault answer.",
        ),
        _coverage_check(
            "question_provenance",
            missing_provenance == 0,
            f"{missing_provenance} fixed question(s) missing source URL, rationale, owner, or timestamps.",
            f"Run: voicevault evaluations export --kb {kb.root} --out <answer-regression-suite.json> --json, fill provenance, then voicevault evaluations import --kb {kb.root} --input <answer-regression-suite.json> --yes --json.",
        ),
    ]
    result = dict(audit)
    result["ok"] = all(check["ok"] for check in checks)
    result["summary"] = summary
    result["checks"] = checks
    return result


def _write_answer_regression_suite(kb: KnowledgeBase, questions: list[dict[str, Any]]) -> dict[str, Any]:
    path = default_answer_regression_suite_path(kb)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": ANSWER_REGRESSION_SCHEMA_VERSION,
        "questions": questions,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return load_answer_regression_suite(kb)


def _question_payload(raw: dict[str, Any], index: int) -> dict[str, Any]:
    query = str(raw.get("query") or "").strip()
    role_id = str(raw.get("role_id") or "").strip()
    expected_role_id = str(raw.get("expected_role_id") or role_id).strip()
    return {
        "id": str(raw.get("id") or _slug(query) or f"question-{index + 1}").strip(),
        "query": query,
        "role_id": role_id,
        "symbol": str(raw.get("symbol") or "").strip(),
        "topic": str(raw.get("topic") or "").strip(),
        "limit": _safe_int(raw.get("limit"), default=5),
        "expected_role_id": expected_role_id,
        "min_evidence": _safe_int(raw.get("min_evidence"), default=1),
        "requires_role_answer": bool(raw.get("requires_role_answer", True)),
        "source_url": str(raw.get("source_url") or raw.get("sourceUrl") or "").strip(),
        "rationale": str(raw.get("rationale") or "").strip(),
        "created_at": str(raw.get("created_at") or raw.get("createdAt") or "").strip(),
        "updated_at": str(raw.get("updated_at") or raw.get("updatedAt") or "").strip(),
        "updated_by": str(raw.get("updated_by") or raw.get("updatedBy") or "").strip(),
    }


def _audit_question(question: dict[str, Any], answers_by_query: dict[str, dict[str, Any]]) -> dict[str, Any]:
    answer = answers_by_query.get(_normalize_query(question["query"]))
    checks = _checks(question, answer)
    failed_checks = [check["id"] for check in checks if check["status"] != PASS_STATUS]
    status = _status(checks)
    actual_role_id = _actual_role_id(answer) if answer else ""
    payload = {
        "query": question["query"],
        "role_id": question["role_id"],
        "symbol": question["symbol"],
        "topic": question["topic"],
        "limit": question["limit"],
        "auto_route": not bool(question["role_id"]),
    }
    return {
        "schema_version": ANSWER_REGRESSION_SCHEMA_VERSION,
        "id": question["id"],
        "status": status,
        "score": _score(checks),
        "query": question["query"],
        "role_id": question["role_id"],
        "symbol": question["symbol"],
        "topic": question["topic"],
        "expected_role_id": question["expected_role_id"],
        "actual_role_id": actual_role_id,
        "min_evidence": question["min_evidence"],
        "requires_role_answer": question["requires_role_answer"],
        "source_url": question["source_url"],
        "rationale": question["rationale"],
        "created_at": question["created_at"],
        "updated_at": question["updated_at"],
        "updated_by": question["updated_by"],
        "evidence_count": int(answer.get("evidence_count") or 0) if answer else 0,
        "citation_count": int(answer.get("citation_count") or 0) if answer else 0,
        "answer_json": str(answer.get("answer_json") or "") if answer else "",
        "answer_markdown": str(answer.get("answer_markdown") or "") if answer else "",
        "failed_checks": failed_checks,
        "checks": checks,
        "recommended_endpoint": "/api/answer" if status != PASS_STATUS else "",
        "payload": payload,
    }


def _checks(question: dict[str, Any], answer: dict[str, Any] | None) -> list[dict[str, str]]:
    if answer is None:
        return [
            _check("answer_export", False, FAIL_STATUS, "Fixed regression question must have an answer export."),
            _check("deliverable", False, FAIL_STATUS, "Answer export must be deliverable."),
            _check("evidence", False, FAIL_STATUS, "Answer must meet the minimum evidence threshold."),
            _check("role_answer", not question["requires_role_answer"], FAIL_STATUS, "Answer must include role_answer."),
            _check("expected_role", not bool(question["expected_role_id"]), FAIL_STATUS, "Answer must match expected role."),
        ]
    evidence_count = int(answer.get("evidence_count") or 0)
    actual_role_id = _actual_role_id(answer)
    expected_role_id = question["expected_role_id"]
    return [
        _check("answer_export", True, PASS_STATUS, "Fixed regression answer export exists."),
        _check("deliverable", answer.get("status") == "deliverable", FAIL_STATUS, "Answer export must be deliverable."),
        _check(
            "evidence",
            evidence_count >= int(question["min_evidence"] or 0),
            FAIL_STATUS,
            "Answer must meet the minimum evidence threshold.",
        ),
        _check(
            "role_answer",
            not question["requires_role_answer"] or _has_role_answer(answer),
            FAIL_STATUS,
            "Answer must include role_answer.",
        ),
        _check(
            "expected_role",
            not expected_role_id or actual_role_id == expected_role_id,
            FAIL_STATUS,
            "Answer must match expected role.",
        ),
    ]


def _check(check_id: str, ok: bool, fail_status: str, message: str) -> dict[str, str]:
    return {"id": check_id, "status": PASS_STATUS if ok else fail_status, "message": message}


def _has_role_answer(answer: dict[str, Any]) -> bool:
    role_answer = answer.get("role_answer") if isinstance(answer.get("role_answer"), dict) else {}
    return bool(
        role_answer
        and role_answer.get("schema_version") == 1
        and str(role_answer.get("answer") or "").strip()
        and isinstance(role_answer.get("evidence_refs"), list)
        and role_answer.get("evidence_refs")
    )


def _actual_role_id(answer: dict[str, Any] | None) -> str:
    if not answer:
        return ""
    selected = str(answer.get("selected_role_id") or "").strip()
    if selected:
        return selected
    role_answer = answer.get("role_answer") if isinstance(answer.get("role_answer"), dict) else {}
    role_id = str(role_answer.get("role_id") or "").strip()
    if role_id:
        return role_id
    filters = answer.get("filters") if isinstance(answer.get("filters"), dict) else {}
    return str(filters.get("role_id") or "").strip()


def _answers_by_query(answers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_query: dict[str, dict[str, Any]] = {}
    for answer in answers:
        query = _normalize_query(str(answer.get("query") or ""))
        if query and query not in by_query:
            by_query[query] = answer
    return by_query


def _summary(items: list[dict[str, Any]], errors: list[str]) -> dict[str, int]:
    return {
        "total": len(items),
        "passed": sum(1 for item in items if item.get("status") == PASS_STATUS),
        "review": sum(1 for item in items if item.get("status") == REVIEW_STATUS),
        "failed": sum(1 for item in items if item.get("status") == FAIL_STATUS),
        "missing_answers": sum(1 for item in items if "answer_export" in item.get("failed_checks", [])),
        "role_mismatches": sum(1 for item in items if "expected_role" in item.get("failed_checks", [])),
        "suite_errors": len(errors),
    }


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
            score -= 25
        elif check["status"] == REVIEW_STATUS:
            score -= 15
    return max(0, score)


def _sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    status_rank = {FAIL_STATUS: 0, REVIEW_STATUS: 1, PASS_STATUS: 2}.get(str(item.get("status") or ""), 3)
    return (status_rank, -int(item.get("score") or 0), str(item.get("id") or ""))


def _suite_error(kb: KnowledgeBase, path: Path, message: str) -> dict[str, Any]:
    return {
        "schema_version": ANSWER_REGRESSION_SCHEMA_VERSION,
        "root": str(kb.root),
        "suite_path": str(path),
        "questions": [],
        "errors": [message],
    }


def _append_answer_regression_change(
    kb: KnowledgeBase,
    *,
    action: str,
    question: dict[str, Any],
    before: dict[str, Any] | None,
    actor: str,
    changed_at: str,
) -> dict[str, Any]:
    path = default_answer_regression_changelog_path(kb)
    path.parent.mkdir(parents=True, exist_ok=True)
    change = {
        "schema_version": ANSWER_REGRESSION_SCHEMA_VERSION,
        "changed_at": changed_at,
        "action": action,
        "question_id": str(question.get("id") or "").strip(),
        "updated_by": actor,
        "question": question,
        "before": before,
    }
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(change, ensure_ascii=False) + "\n")
    return change


def _change_payload(raw: dict[str, Any]) -> dict[str, Any]:
    question = raw.get("question") if isinstance(raw.get("question"), dict) else {}
    before = raw.get("before") if isinstance(raw.get("before"), dict) else None
    return {
        "schema_version": ANSWER_REGRESSION_SCHEMA_VERSION,
        "changed_at": str(raw.get("changed_at") or "").strip(),
        "action": str(raw.get("action") or "").strip(),
        "question_id": str(raw.get("question_id") or question.get("id") or "").strip(),
        "updated_by": str(raw.get("updated_by") or "").strip(),
        "question": question,
        "before": before,
    }


def _load_import_payload(suite_input: dict[str, Any] | str | Path) -> tuple[dict[str, Any], Path | None, list[str]]:
    if isinstance(suite_input, dict):
        return suite_input, None, []
    path = Path(suite_input)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, path, [f"import payload must be readable JSON: {exc}"]
    if not isinstance(payload, dict):
        return {}, path, ["import payload must contain a JSON object"]
    return payload, path, []


def _questions_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return {key: left.get(key) for key in sorted(_question_compare_keys())} == {
        key: right.get(key) for key in sorted(_question_compare_keys())
    }


def _question_compare_keys() -> set[str]:
    return {
        "id",
        "query",
        "role_id",
        "symbol",
        "topic",
        "limit",
        "expected_role_id",
        "min_evidence",
        "requires_role_answer",
        "source_url",
        "rationale",
        "created_at",
        "updated_at",
        "updated_by",
    }


def _missing_provenance_fields(item: dict[str, Any]) -> list[str]:
    fields = []
    for field in ["source_url", "rationale", "updated_by", "created_at", "updated_at"]:
        if not str(item.get(field) or "").strip():
            fields.append(field)
    return fields


def _coverage_check(check_id: str, ok: bool, message: str, remediation: str) -> dict[str, Any]:
    check = {"id": check_id, "ok": ok, "message": message}
    if not ok:
        check["remediation"] = remediation
    return check


def _actor(value: Any) -> str:
    return str(value or "local-ui").strip() or "local-ui"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_query(value: str) -> str:
    return " ".join(value.lower().split())


def _slug(value: str) -> str:
    return re.sub(r"[^\w-]+", "-", value.lower(), flags=re.UNICODE).strip("-_")[:80]
