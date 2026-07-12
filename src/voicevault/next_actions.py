from __future__ import annotations

import re
from typing import Any

from .answer import list_answer_exports
from .comparison import list_comparison_exports
from .importers import load_statements_from_kb
from .kb import KnowledgeBase
from .roles import evaluate_role_coverage
from .sources import list_sources, read_source_status


NEXT_ACTION_SCHEMA_VERSION = 1


def build_research_next_actions(
    kb: KnowledgeBase,
    *,
    latest_record: dict[str, Any] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if latest_record:
        actions.extend(_statement_research_actions(kb, latest_record))
    else:
        latest_statement = _latest_statement(kb)
        if latest_statement:
            actions.extend(_statement_research_actions(kb, latest_statement.to_dict()))
    actions.extend(_draft_comparison_actions(kb))
    actions.extend(_coverage_gap_actions(kb))
    actions.extend(_source_health_actions(kb))
    return _dedupe(actions)[: max(1, limit)]


def build_research_action_audit(
    kb: KnowledgeBase,
    *,
    latest_record: dict[str, Any] | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    ready_actions = build_research_next_actions(kb, latest_record=latest_record, limit=limit)
    record = latest_record
    if record is None:
        latest_statement = _latest_statement(kb)
        record = latest_statement.to_dict() if latest_statement else None
    completed_actions = _dedupe(_completed_statement_actions(kb, record)) if record else []
    return {
        "schema_version": NEXT_ACTION_SCHEMA_VERSION,
        "summary": {
            "ready": len(ready_actions),
            "completed": len(completed_actions),
        },
        "completed_actions": completed_actions,
    }


def _statement_research_actions(kb: KnowledgeBase, record: dict[str, Any]) -> list[dict[str, Any]]:
    role_id = str(record.get("role_id") or "").strip()
    symbols = _string_list(record.get("symbols"))
    topics = _string_list(record.get("topics"))
    query = _query_for_record(record, symbols=symbols, topics=topics)
    if not role_id or not query:
        return []

    actions: list[dict[str, Any]] = []
    statement_id = str(record.get("statement_id") or "")
    if not _completed_answer_export(kb, query):
        reason = "A public statement was captured; verify the new evidence can answer a concrete question."
        actions.append(
            _action(
                action_id=f"answer-{_slug(role_id)}-{_slug(query)}",
                phase="research",
                action_type="answer",
                label="Ask evidence answer",
                action=f"Ask an evidence-backed question for {role_id}.",
                reason=reason,
                endpoint="/api/answer",
                payload={
                    "query": query,
                    "role_id": role_id,
                    "symbol": symbols[0] if symbols else "",
                    "topic": topics[0] if topics else "",
                    "limit": 5,
                },
                command=(
                    f"voicevault answer --kb {kb.root} --query \"{query}\" --role {role_id}"
                    + (f" --symbol {symbols[0]}" if symbols else "")
                    + (f" --topic {topics[0]}" if topics else "")
                    + " --json"
                ),
                audit=_recommended_audit(
                    trigger="latest_statement",
                    trigger_id=statement_id,
                    query=query,
                    reason=reason,
                    completion_key=_completion_key("answer", query),
                ),
            )
        )
    coverage = evaluate_role_coverage(kb)
    if (
        int(coverage.get("reviewed_roles_with_statements") or 0) >= int(coverage.get("min_reviewed_roles") or 2)
        and not _completed_comparison_export(kb, query)
    ):
        reason = "Role coverage is sufficient; compare viewpoints before adopting the result for release handoff."
        actions.append(
            _action(
                action_id=f"compare-{_slug(query)}",
                phase="research",
                action_type="compare",
                label="Compare roles",
                action="Compare relevant roles on the new evidence question.",
                reason=reason,
                endpoint="/api/compare",
                payload={
                    "query": query,
                    "roles": "auto",
                    "symbol": symbols[0] if symbols else "",
                    "topic": topics[0] if topics else "",
                    "limit": 3,
                    "evidence_limit": 3,
                },
                command=(
                    f"voicevault compare --kb {kb.root} --query \"{query}\" --roles auto"
                    + (f" --symbol {symbols[0]}" if symbols else "")
                    + (f" --topic {topics[0]}" if topics else "")
                    + " --json"
                ),
                audit=_recommended_audit(
                    trigger="latest_statement",
                    trigger_id=statement_id,
                    query=query,
                    reason=reason,
                    completion_key=_completion_key("compare", query),
                ),
            )
        )
    return actions


def _has_completed_answer(kb: KnowledgeBase, query: str) -> bool:
    return _completed_answer_export(kb, query) is not None


def _has_completed_comparison(kb: KnowledgeBase, query: str) -> bool:
    return _completed_comparison_export(kb, query) is not None


def _completed_answer_export(kb: KnowledgeBase, query: str) -> dict[str, Any] | None:
    normalized_query = query.strip().lower()
    for item in list_answer_exports(kb, status="deliverable"):
        if str(item.get("query") or "").strip().lower() == normalized_query:
            return item
    return None


def _completed_comparison_export(kb: KnowledgeBase, query: str) -> dict[str, Any] | None:
    normalized_query = query.strip().lower()
    for item in list_comparison_exports(kb):
        if str(item.get("query") or "").strip().lower() != normalized_query:
            continue
        if item.get("status") == "deliverable":
            return item
    return None


def _completed_statement_actions(kb: KnowledgeBase, record: dict[str, Any]) -> list[dict[str, Any]]:
    role_id = str(record.get("role_id") or "").strip()
    symbols = _string_list(record.get("symbols"))
    topics = _string_list(record.get("topics"))
    query = _query_for_record(record, symbols=symbols, topics=topics)
    statement_id = str(record.get("statement_id") or "")
    if not role_id or not query:
        return []

    actions: list[dict[str, Any]] = []
    answer_export = _completed_answer_export(kb, query)
    if answer_export:
        reason = "A deliverable answer export exists for this query, so Ask evidence answer is complete."
        actions.append(
            _completed_action(
                action_id=f"completed-answer-{_slug(role_id)}-{_slug(query)}",
                phase="research",
                action_type="answer",
                label="Ask evidence answer",
                action=f"Ask an evidence-backed question for {role_id}.",
                reason=reason,
                payload={
                    "query": query,
                    "role_id": role_id,
                    "symbol": symbols[0] if symbols else "",
                    "topic": topics[0] if topics else "",
                    "limit": 5,
                },
                trigger_id=statement_id,
                completion_key=_completion_key("answer", query),
                completed_by=_completed_by(
                    "answer_export",
                    answer_export,
                    json_field="answer_json",
                    markdown_field="answer_markdown",
                ),
            )
        )

    coverage = evaluate_role_coverage(kb)
    comparison_export = _completed_comparison_export(kb, query)
    if (
        comparison_export
        and int(coverage.get("reviewed_roles_with_statements") or 0) >= int(coverage.get("min_reviewed_roles") or 2)
    ):
        reason = "A deliverable comparison export exists for this query, so Compare roles is complete."
        actions.append(
            _completed_action(
                action_id=f"completed-compare-{_slug(query)}",
                phase="research",
                action_type="compare",
                label="Compare roles",
                action="Compare relevant roles on the new evidence question.",
                reason=reason,
                payload={
                    "query": query,
                    "roles": "auto",
                    "symbol": symbols[0] if symbols else "",
                    "topic": topics[0] if topics else "",
                    "limit": 3,
                    "evidence_limit": 3,
                },
                trigger_id=statement_id,
                completion_key=_completion_key("compare", query),
                completed_by=_completed_by(
                    "comparison_export",
                    comparison_export,
                    json_field="comparison_json",
                    markdown_field="comparison_markdown",
                ),
            )
        )
    return actions


def _draft_comparison_actions(kb: KnowledgeBase) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for item in list_comparison_exports(kb, review_status="draft")[:3]:
        query = str(item.get("query") or "").strip()
        if not query:
            continue
        actions.append(
            _action(
                action_id=f"review-comparison-{_slug(query)}",
                phase="review",
                action_type="review_comparison",
                label="Review comparison",
                action=f"Review and adopt or reject the draft comparison for {query}.",
                reason="Draft comparisons are not release-ready until reviewed.",
                endpoint="/api/comparison/review",
                payload={
                    "query": query,
                    "status": "adopted",
                    "reviewer": "local-ui",
                    "notes": "Adopted from research next actions.",
                },
                command=(
                    f"voicevault comparisons review --kb {kb.root} --query \"{query}\" "
                    "--status adopted --reviewer local-ui --notes \"Adopted from research next actions.\" --json"
                ),
            )
        )
    return actions


def _coverage_gap_actions(kb: KnowledgeBase) -> list[dict[str, Any]]:
    coverage = evaluate_role_coverage(kb)
    actions: list[dict[str, Any]] = []
    for gap in coverage.get("gaps", []):
        if not isinstance(gap, dict):
            continue
        role_id = str(gap.get("role_id") or "").strip()
        if not role_id:
            continue
        gap_name = str(gap.get("gap") or "").strip()
        if gap_name in {"unreviewed", "missing"}:
            actions.append(
                _action(
                    action_id=f"promote-profile-{_slug(role_id)}",
                    phase="capture",
                    action_type="fix",
                    label="Promote profile",
                    action=f"Generate and promote the profile for {role_id}.",
                    reason="This role has evidence but is not reviewed, so it cannot satisfy role coverage.",
                    payload={"role_id": role_id},
                    command=(
                        f"voicevault profile generate --kb {kb.root} --role {role_id} --json && "
                        f"voicevault profile promote --kb {kb.root} --role {role_id} --reviewer local-ui "
                        "--note \"Reviewed from next actions.\" --json"
                    ),
                )
            )
        elif gap_name == "insufficient_statements":
            actions.append(
                _action(
                    action_id=f"capture-more-{_slug(role_id)}",
                    phase="capture",
                    action_type="fix",
                    label="Capture evidence",
                    action=f"Capture at least one public statement for {role_id}.",
                    reason="Reviewed roles need indexed statements before they can participate in release coverage.",
                    payload={"role_id": role_id},
                    command=f"voicevault sources run --kb {kb.root} --source <source_id> --text \"<public statement>\" --json",
                )
            )
    return actions


def _source_health_actions(kb: KnowledgeBase) -> list[dict[str, Any]]:
    status = read_source_status(kb)
    latest_by_source = {
        str(item.get("source_id") or ""): item.get("latest_run")
        for item in status.get("sources", [])
        if isinstance(item, dict)
    }
    actions: list[dict[str, Any]] = []
    for source in list_sources(kb):
        source_id = str(source.get("source_id") or "").strip()
        if not source_id or source.get("status") != "active":
            continue
        latest = latest_by_source.get(source_id)
        if not latest:
            actions.append(
                _action(
                    action_id=f"run-source-{_slug(source_id)}",
                    phase="capture",
                    action_type="fix",
                    label="Run source",
                    action=f"Run source {source_id} and sync captured evidence.",
                    reason="Active sources should have a recorded run before release.",
                    payload={"source_id": source_id},
                    command=f"voicevault sources run --kb {kb.root} --source {source_id} --dry-run --json",
                )
            )
        elif isinstance(latest, dict) and latest.get("status") == "failed":
            actions.append(
                _action(
                    action_id=f"fix-source-{_slug(source_id)}",
                    phase="capture",
                    action_type="fix",
                    label="Fix source run",
                    action=f"Inspect and rerun source {source_id}.",
                    reason=str(latest.get("error") or "Latest source run failed."),
                    payload={"source_id": source_id},
                    command=f"voicevault sources status --kb {kb.root} --json",
                )
            )
    return actions


def _action(
    *,
    action_id: str,
    phase: str,
    action_type: str,
    label: str,
    action: str,
    reason: str,
    payload: dict[str, Any],
    endpoint: str = "",
    command: str = "",
    audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": NEXT_ACTION_SCHEMA_VERSION,
        "id": action_id,
        "status": "ready" if action_type != "fix" else "needs_attention",
        "phase": phase,
        "action_type": action_type,
        "label": label,
        "action": action,
        "reason": reason,
        "endpoint": endpoint,
        "payload": payload,
        "command": command,
        "audit": audit
        or _recommended_audit(
            trigger="system",
            trigger_id=action_id,
            query=str(payload.get("query") or ""),
            reason=reason,
            completion_key=f"{action_type}:{action_id}",
        ),
    }


def _recommended_audit(
    *,
    trigger: str,
    trigger_id: str,
    query: str,
    reason: str,
    completion_key: str,
) -> dict[str, Any]:
    return {
        "state": "recommended",
        "trigger": trigger,
        "trigger_id": trigger_id,
        "query": query,
        "reason": reason,
        "completion_key": completion_key,
        "completion": {"status": "pending"},
    }


def _completed_action(
    *,
    action_id: str,
    phase: str,
    action_type: str,
    label: str,
    action: str,
    reason: str,
    payload: dict[str, Any],
    trigger_id: str,
    completion_key: str,
    completed_by: dict[str, str],
) -> dict[str, Any]:
    return {
        "schema_version": NEXT_ACTION_SCHEMA_VERSION,
        "id": action_id,
        "status": "completed",
        "phase": phase,
        "action_type": action_type,
        "label": label,
        "action": action,
        "reason": reason,
        "endpoint": "",
        "payload": payload,
        "command": "",
        "completed_by": completed_by,
        "audit": {
            "state": "completed",
            "trigger": "latest_statement",
            "trigger_id": trigger_id,
            "query": str(payload.get("query") or ""),
            "reason": reason,
            "completion_key": completion_key,
            "completion": {
                "status": "completed",
                "completed_by": completed_by,
            },
        },
    }


def _completed_by(
    kind: str,
    item: dict[str, Any],
    *,
    json_field: str,
    markdown_field: str,
) -> dict[str, str]:
    return {
        "kind": kind,
        "path": str(item.get(json_field) or ""),
        "markdown_path": str(item.get(markdown_field) or ""),
        "status": str(item.get("status") or ""),
        "review_status": str(item.get("review_status") or ""),
        "generated_at": str(item.get("generated_at") or ""),
    }


def _completion_key(action_type: str, query: str) -> str:
    return f"{action_type}:{query.strip().lower()}"


def _latest_statement(kb: KnowledgeBase) -> Any | None:
    statements = load_statements_from_kb(kb)
    if not statements:
        return None
    return sorted(statements, key=lambda item: item.published_at or item.captured_at or "", reverse=True)[0]


def _query_for_record(record: dict[str, Any], *, symbols: list[str], topics: list[str]) -> str:
    title = str(record.get("title") or "").strip()
    if title:
        parts = [title]
    else:
        parts = [str(record.get("role_id") or "public evidence").strip()]
    for value in [*(symbols[:1]), *(topics[:1])]:
        if value and value.lower() not in " ".join(parts).lower():
            parts.append(value)
    return " ".join(parts).strip()


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _dedupe(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for action in actions:
        action_id = str(action.get("id") or "")
        if not action_id or action_id in seen:
            continue
        seen.add(action_id)
        result.append(action)
    return result


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip().lower()).strip("-")
    return slug[:80] or "next"
