from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .answer import answer_query
from .kb import KnowledgeBase
from .roles import list_role_summaries
from .routing import ROLE_ROUTING_SCHEMA_VERSION, suggest_roles


COMPARISON_SCHEMA_VERSION = 1
COMPARISON_EXPORT_STATUS_CHOICES = ("all", "deliverable", "invalid", "evidence_backed", "no_evidence", "legacy_contract", "malformed")
COMPARISON_REVIEW_STATUS_CHOICES = ("draft", "reviewed", "adopted", "rejected")
COMPARISON_REVIEW_STATUS_FILTER_CHOICES = ("all",) + COMPARISON_REVIEW_STATUS_CHOICES
COMPARISON_REVIEWED_STATUSES = ("reviewed", "adopted")
DELIVERABLE_COMPARISON_LANGUAGE = "zh-CN"
MAX_COMPARISON_ROLE_LIMIT = 20
MAX_COMPARISON_EVIDENCE_LIMIT = 20


def compare_roles(
    kb: KnowledgeBase,
    query: str,
    *,
    roles: str | list[str] = "auto",
    symbol: str = "",
    topic: str = "",
    limit: int = 3,
    evidence_limit: int = 3,
) -> dict[str, Any]:
    role_limit = _safe_limit(limit, default=3, maximum=MAX_COMPARISON_ROLE_LIMIT)
    per_role_limit = _safe_limit(evidence_limit, default=3, maximum=MAX_COMPARISON_EVIDENCE_LIMIT)
    role_summaries = {item["role_id"]: item for item in list_role_summaries(kb)}
    mode, requested_role_ids = _requested_role_ids(roles)
    routing = _empty_routing(query, symbol=symbol, topic=topic, limit=role_limit)

    if mode == "auto":
        routing = suggest_roles(kb, query, symbol=symbol, topic=topic, limit=role_limit)
        selected_role_ids = [str(item.get("role_id") or "") for item in routing.get("routes", [])]
        keep_no_evidence = False
    elif mode == "all":
        selected_role_ids = list(role_summaries)
        keep_no_evidence = False
    else:
        selected_role_ids = requested_role_ids
        keep_no_evidence = True

    role_answers: list[dict[str, Any]] = []
    for role_id in _dedupe(selected_role_ids):
        if len(role_answers) >= role_limit:
            break
        item = _role_answer_item(
            kb,
            query,
            role_id=role_id,
            role_summary=role_summaries.get(role_id, {}),
            symbol=symbol,
            topic=topic,
            evidence_limit=per_role_limit,
        )
        if item["status"] == "no_evidence" and not keep_no_evidence:
            continue
        role_answers.append(item)

    coverage = _coverage(role_answers)
    consensus = _consensus(role_answers)
    divergences = _divergences(role_answers)
    comparison_answer = _comparison_answer_text(query, coverage, consensus, divergences)
    result: dict[str, Any] = {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "comparison_type": "local_evidence_role_comparison",
        "answer_language": DELIVERABLE_COMPARISON_LANGUAGE,
        "query": query,
        "filters": {
            "roles": _roles_filter_value(roles),
            "symbol": symbol,
            "topic": topic,
            "limit": role_limit,
            "evidence_limit": per_role_limit,
        },
        "generated_at": _now_utc(),
        "confidence": _comparison_confidence(coverage),
        "routing": routing,
        "coverage": coverage,
        "review": _default_review(),
        "comparison_answer": comparison_answer,
        "consensus": consensus,
        "divergences": divergences,
        "role_answers": role_answers,
    }
    result["comparison_markdown"] = _comparison_markdown(result)
    return result


def write_comparison_outputs(out_dir: Path, result: dict[str, Any]) -> dict[str, Path]:
    result["comparison_markdown"] = _comparison_markdown(result)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "comparison.json"
    markdown_path = out_dir / "comparison.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    markdown_path.write_text(str(result["comparison_markdown"]), encoding="utf-8", newline="\n")
    return {"comparison_json": json_path, "comparison_markdown": markdown_path}


def list_comparison_exports(
    kb: KnowledgeBase,
    *,
    status: str = "all",
    review_status: str = "all",
) -> list[dict[str, Any]]:
    comparisons_dir = kb.exports_dir / "comparisons"
    if not comparisons_dir.exists():
        return []
    exports = [inspect_comparison_export(path) for path in sorted(comparisons_dir.glob("*/comparison.json"))]
    sorted_exports = sorted(exports, key=lambda item: (item["generated_at"], item["query"], item["comparison_json"]), reverse=True)
    return filter_comparison_exports(sorted_exports, status=status, review_status=review_status)


def inspect_comparison_export(json_path: Path) -> dict[str, Any]:
    markdown_path = json_path.with_name("comparison.md")
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _malformed_comparison_export(json_path, markdown_path, str(exc))
    if not isinstance(payload, dict):
        return _malformed_comparison_export(json_path, markdown_path, "comparison.json must contain a JSON object.")

    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    role_answers = payload.get("role_answers") if isinstance(payload.get("role_answers"), list) else []
    evidence_count = _safe_int(coverage.get("evidence_count"))
    role_count = _safe_int(coverage.get("role_count"))
    review = _review_payload(payload)
    contract_errors = _comparison_contract_errors(payload)
    item = {
        "query": str(payload.get("query") or ""),
        "generated_at": str(payload.get("generated_at") or ""),
        "confidence": str(payload.get("confidence") or ""),
        "answer_language": str(payload.get("answer_language") or ""),
        "comparison_answer": str(payload.get("comparison_answer") or ""),
        "schema_version": _safe_int(payload.get("schema_version")),
        "contract_errors": contract_errors,
        "role_count": role_count,
        "evidence_count": evidence_count,
        "role_ids": [str(item.get("role_id") or "") for item in role_answers if isinstance(item, dict)],
        "evidence_backed": evidence_count > 0,
        "review_status": review["status"],
        "reviewed_at": review["reviewed_at"],
        "reviewer": review["reviewer"],
        "review_notes": review["notes"],
        "reviewed": review["status"] in COMPARISON_REVIEWED_STATUSES,
        "adopted": review["status"] == "adopted",
        "malformed": False,
        "error": "",
        "comparison_json": str(json_path),
        "comparison_markdown": str(markdown_path),
    }
    item["status"] = comparison_export_status(item)
    return item


def filter_comparison_exports(
    exports: list[dict[str, Any]],
    *,
    status: str = "all",
    review_status: str = "all",
) -> list[dict[str, Any]]:
    _validate_comparison_export_status(status)
    _validate_comparison_review_filter_status(review_status)
    return [
        item
        for item in exports
        if _matches_comparison_export_status(item, status)
        and _matches_comparison_review_status(item, review_status)
    ]


def summarize_comparison_exports(exports: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(exports),
        "deliverable": len(filter_comparison_exports(exports, status="deliverable")),
        "invalid": len(filter_comparison_exports(exports, status="invalid")),
        "evidence_backed": len(filter_comparison_exports(exports, status="evidence_backed")),
        "no_evidence": len(filter_comparison_exports(exports, status="no_evidence")),
        "legacy_contract": len(filter_comparison_exports(exports, status="legacy_contract")),
        "malformed": len(filter_comparison_exports(exports, status="malformed")),
        "draft": len(filter_comparison_exports(exports, review_status="draft")),
        "reviewed": len(filter_comparison_exports(exports, review_status="reviewed")),
        "adopted": len(filter_comparison_exports(exports, review_status="adopted")),
        "rejected": len(filter_comparison_exports(exports, review_status="rejected")),
    }


def is_deliverable_comparison_export(item: dict[str, Any]) -> bool:
    return bool(
        not item.get("malformed")
        and not item.get("contract_errors")
        and item.get("evidence_backed")
        and item.get("role_count", 0) > 0
        and item.get("answer_language") == DELIVERABLE_COMPARISON_LANGUAGE
    )


def is_reviewed_comparison_export(item: dict[str, Any]) -> bool:
    return is_deliverable_comparison_export(item) and item.get("review_status") in COMPARISON_REVIEWED_STATUSES


def is_adopted_comparison_export(item: dict[str, Any]) -> bool:
    return is_deliverable_comparison_export(item) and item.get("review_status") == "adopted"


def comparison_export_status(item: dict[str, Any]) -> str:
    if item.get("malformed"):
        return "malformed"
    if item.get("contract_errors"):
        return "legacy_contract"
    if is_deliverable_comparison_export(item):
        return "deliverable"
    if not item.get("evidence_backed"):
        return "no_evidence"
    return "legacy_contract"


def default_comparison_dir(kb: KnowledgeBase, query: str) -> Path:
    slug = _slug(query) or "comparison"
    return kb.exports_dir / "comparisons" / slug


def review_comparison_export(
    json_path: Path,
    *,
    status: str,
    reviewer: str = "",
    notes: str = "",
) -> dict[str, Any]:
    _validate_comparison_review_status(status)
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot review comparison export: {json_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Cannot review comparison export: {json_path}: comparison.json must contain a JSON object.")
    payload["review"] = {
        "status": status,
        "reviewed_at": _now_utc() if status != "draft" else "",
        "reviewer": str(reviewer or "").strip(),
        "notes": str(notes or "").strip(),
    }
    output = write_comparison_outputs(json_path.parent, payload)
    return {
        "comparison": payload,
        "comparison_json": str(output["comparison_json"]),
        "comparison_markdown": str(output["comparison_markdown"]),
    }


def _role_answer_item(
    kb: KnowledgeBase,
    query: str,
    *,
    role_id: str,
    role_summary: dict[str, Any],
    symbol: str,
    topic: str,
    evidence_limit: int,
) -> dict[str, Any]:
    answer = answer_query(kb, query, role_id=role_id, symbol=symbol, topic=topic, limit=evidence_limit)
    evidence = answer.get("evidence") if isinstance(answer.get("evidence"), list) else []
    evidence_count = int(answer.get("coverage", {}).get("evidence_count") or 0)
    return {
        "role_id": role_id,
        "display_name": str(role_summary.get("display_name") or role_id),
        "profile_status": str(role_summary.get("profile_status") or ""),
        "status": "evidence_backed" if evidence_count > 0 else "no_evidence",
        "confidence": str(answer.get("confidence") or "low"),
        "evidence_count": evidence_count,
        "total_matches": int(answer.get("coverage", {}).get("total_matches") or 0),
        "dominant_stance": _dominant_value(evidence, "stance"),
        "time_horizons": _unique_values(evidence, "time_horizon"),
        "symbols": _unique_nested_values(evidence, "symbols"),
        "topics": _unique_nested_values(evidence, "topics"),
        "key_points": answer.get("key_points") if isinstance(answer.get("key_points"), list) else [],
        "citations": answer.get("citations") if isinstance(answer.get("citations"), list) else [],
        "answer": answer,
    }


def _coverage(role_answers: list[dict[str, Any]]) -> dict[str, int]:
    evidence_backed = [item for item in role_answers if item["status"] == "evidence_backed"]
    return {
        "role_count": len(role_answers),
        "evidence_backed_role_count": len(evidence_backed),
        "no_evidence_role_count": len(role_answers) - len(evidence_backed),
        "evidence_count": sum(int(item.get("evidence_count") or 0) for item in role_answers),
        "total_matches": sum(int(item.get("total_matches") or 0) for item in role_answers),
    }


def _consensus(role_answers: list[dict[str, Any]]) -> dict[str, Any]:
    evidence_roles = [item for item in role_answers if item["status"] == "evidence_backed"]
    if not evidence_roles:
        return {
            "summary": "当前本地索引没有为这些角色找到可引用证据。",
            "shared_stance": "none",
            "points": [],
        }
    stance_counts = Counter(item.get("dominant_stance") or "unclear" for item in evidence_roles)
    shared_stance, stance_count = stance_counts.most_common(1)[0]
    points = [f"{len(evidence_roles)} 个角色有可引用证据，合计 {sum(item['evidence_count'] for item in evidence_roles)} 条"]
    if stance_count >= 2:
        points.append(f"{stance_count} 个角色的主要结构化立场为「{shared_stance}」")
    common_topics = _common_values(evidence_roles, "topics")
    if common_topics:
        points.append(f"共同主题：{', '.join(common_topics[:5])}")
    return {
        "summary": "；".join(points) + "。",
        "shared_stance": shared_stance,
        "points": points,
    }


def _divergences(role_answers: list[dict[str, Any]]) -> list[dict[str, str]]:
    evidence_roles = [item for item in role_answers if item["status"] == "evidence_backed"]
    if len(evidence_roles) < 2:
        return [{"type": "coverage_gap", "summary": "可引用角色少于 2 个，无法形成角色间分歧判断。"}]
    divergences: list[dict[str, str]] = []
    stance_groups = _group_roles_by_value(evidence_roles, "dominant_stance")
    if len(stance_groups) > 1:
        summary = "; ".join(f"{stance}: {', '.join(role_ids)}" for stance, role_ids in stance_groups.items())
        divergences.append({"type": "stance", "summary": f"结构化立场不同：{summary}。"})
    horizon_groups: dict[str, list[str]] = {}
    for item in evidence_roles:
        horizons = item.get("time_horizons") or ["unclear"]
        for horizon in horizons:
            horizon_groups.setdefault(str(horizon), []).append(str(item["role_id"]))
    if len(horizon_groups) > 1:
        summary = "; ".join(f"{horizon}: {', '.join(sorted(set(role_ids)))}" for horizon, role_ids in horizon_groups.items())
        divergences.append({"type": "time_horizon", "summary": f"时间视角不同：{summary}。"})
    if not divergences:
        divergences.append({"type": "none", "summary": "未发现结构化立场或时间视角上的明显分歧；仍需阅读原始引用确认语境。"})
    return divergences


def _comparison_answer_text(query: str, coverage: dict[str, int], consensus: dict[str, Any], divergences: list[dict[str, str]]) -> str:
    if coverage["evidence_count"] <= 0:
        return f"证据不足。声迹没有在当前本地公开观点库中找到可引用证据来对比「{query}」。"
    return (
        f"声迹在本地索引中找到 {coverage['evidence_count']} 条可引用证据，覆盖 "
        f"{coverage['evidence_backed_role_count']} 个有证据角色。{consensus['summary']} "
        f"主要分歧：{divergences[0]['summary']} "
        "以下内容只整理已归档公开 statement，不代表事实断言、投资建议或人物最新观点。"
    )


def _comparison_markdown(result: dict[str, Any]) -> str:
    review = _review_payload(result)
    lines = [
        "# 角色对比回答",
        "",
        f"查询：{result['query']}",
        "",
        "## 审阅",
        "",
        f"- Status: {review['status']}",
        f"- Reviewed at: {review['reviewed_at'] or 'pending'}",
        f"- Reviewer: {review['reviewer'] or 'pending'}",
        f"- Notes: {review['notes'] or 'pending'}",
        "",
        "## 结论",
        "",
        str(result["comparison_answer"]),
        "",
        "## 共识",
        "",
    ]
    points = result.get("consensus", {}).get("points") or []
    lines.extend([f"- {point}" for point in points] or ["- 没有形成可引用共识。"])
    lines.extend(["", "## 分歧", ""])
    lines.extend([f"- {item['summary']}" for item in result.get("divergences", [])])
    lines.extend(["", "## 角色对比", ""])
    role_answers = result.get("role_answers") if isinstance(result.get("role_answers"), list) else []
    if not role_answers:
        lines.append("没有可对比的角色证据。")
    for item in role_answers:
        lines.extend(
            [
                f"### {item['role_id']}",
                "",
                f"- Status: {item['status']}",
                f"- Evidence: {item['evidence_count']}",
                f"- Stance: {item.get('dominant_stance') or 'unclear'}",
                f"- Time horizon: {', '.join(item.get('time_horizons') or ['unclear'])}",
                "",
            ]
        )
        for point in item.get("key_points", [])[:3]:
            refs = " ".join(point.get("refs") or [])
            lines.append(f"- {refs} {point.get('text') or ''}")
        lines.append("")
    lines.extend(["## 引用", ""])
    for item in role_answers:
        for citation in item.get("citations", []):
            source = citation.get("source_url") or "unknown source"
            lines.append(f"- {item['role_id']} {citation.get('ref')}: {citation.get('title')} ({source})")
    lines.extend(
        [
            "",
            "## 不确定性",
            "",
            "- 该对比由本地规则生成，只总结已索引的公开 statement。",
            "- 分歧判断来自结构化 stance、time_horizon 和证据覆盖度，不能替代人工审阅原文。",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def _requested_role_ids(roles: str | list[str]) -> tuple[str, list[str]]:
    if isinstance(roles, list):
        return "explicit", [str(role).strip() for role in roles if str(role).strip()]
    normalized = str(roles or "auto").strip()
    lowered = normalized.lower()
    if lowered in {"", "__auto__", "auto"}:
        return "auto", []
    if lowered == "all":
        return "all", []
    return "explicit", [part.strip() for part in normalized.split(",") if part.strip()]


def _empty_routing(query: str, *, symbol: str, topic: str, limit: int) -> dict[str, Any]:
    return {
        "schema_version": ROLE_ROUTING_SCHEMA_VERSION,
        "query": query,
        "filters": {"symbol": symbol, "topic": topic, "limit": limit},
        "generated_at": _now_utc(),
        "suggested_role_id": "",
        "confidence": "none",
        "route_count": 0,
        "routes": [],
        "no_match_reason": "",
    }


def _dominant_value(evidence: list[dict[str, Any]], field: str) -> str:
    values = [str(item.get(field) or "unclear") for item in evidence if isinstance(item, dict)]
    if not values:
        return "unclear"
    return Counter(values).most_common(1)[0][0]


def _unique_values(evidence: list[dict[str, Any]], field: str) -> list[str]:
    return sorted({str(item.get(field) or "unclear") for item in evidence if isinstance(item, dict)})


def _unique_nested_values(evidence: list[dict[str, Any]], field: str) -> list[str]:
    values: set[str] = set()
    for item in evidence:
        raw_values = item.get(field) if isinstance(item, dict) else []
        if not isinstance(raw_values, list):
            continue
        values.update(str(value) for value in raw_values if str(value))
    return sorted(values)


def _common_values(role_answers: list[dict[str, Any]], field: str) -> list[str]:
    sets = [set(item.get(field) or []) for item in role_answers if item.get(field)]
    if not sets:
        return []
    return sorted(set.intersection(*sets))


def _group_roles_by_value(role_answers: list[dict[str, Any]], field: str) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for item in role_answers:
        groups.setdefault(str(item.get(field) or "unclear"), []).append(str(item["role_id"]))
    return {key: sorted(values) for key, values in sorted(groups.items())}


def _comparison_confidence(coverage: dict[str, int]) -> str:
    if coverage["evidence_backed_role_count"] >= 3 and coverage["evidence_count"] >= 6:
        return "high"
    if coverage["evidence_backed_role_count"] >= 2 and coverage["evidence_count"] >= 3:
        return "medium"
    if coverage["evidence_count"] > 0:
        return "low"
    return "none"


def _roles_filter_value(roles: str | list[str]) -> str:
    if isinstance(roles, list):
        return ",".join(str(role) for role in roles)
    return str(roles or "auto")


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


def _matches_comparison_export_status(item: dict[str, Any], status: str) -> bool:
    if status == "all":
        return True
    if status == "invalid":
        return not is_deliverable_comparison_export(item)
    if status == "evidence_backed":
        return bool(item.get("evidence_backed"))
    return item.get("status") == status


def _matches_comparison_review_status(item: dict[str, Any], status: str) -> bool:
    if status == "all":
        return True
    return item.get("review_status") == status


def _validate_comparison_export_status(status: str) -> None:
    if status not in COMPARISON_EXPORT_STATUS_CHOICES:
        choices = ", ".join(COMPARISON_EXPORT_STATUS_CHOICES)
        raise ValueError(f"Unknown comparison export status: {status}. Expected one of: {choices}")


def _validate_comparison_review_filter_status(status: str) -> None:
    if status not in COMPARISON_REVIEW_STATUS_FILTER_CHOICES:
        choices = ", ".join(COMPARISON_REVIEW_STATUS_FILTER_CHOICES)
        raise ValueError(f"Unknown comparison review status: {status}. Expected one of: {choices}")


def _validate_comparison_review_status(status: str) -> None:
    if status not in COMPARISON_REVIEW_STATUS_CHOICES:
        choices = ", ".join(COMPARISON_REVIEW_STATUS_CHOICES)
        raise ValueError(f"Unknown comparison review status: {status}. Expected one of: {choices}")


def _default_review() -> dict[str, str]:
    return {
        "status": "draft",
        "reviewed_at": "",
        "reviewer": "",
        "notes": "",
    }


def _review_payload(payload: dict[str, Any]) -> dict[str, str]:
    review = payload.get("review") if isinstance(payload.get("review"), dict) else {}
    status = str(review.get("status") or "draft")
    if status not in COMPARISON_REVIEW_STATUS_CHOICES:
        status = "draft"
    return {
        "status": status,
        "reviewed_at": str(review.get("reviewed_at") or ""),
        "reviewer": str(review.get("reviewer") or ""),
        "notes": str(review.get("notes") or ""),
    }


def _comparison_contract_errors(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("schema_version") != COMPARISON_SCHEMA_VERSION:
        errors.append("schema_version must be 1")
    if payload.get("comparison_type") != "local_evidence_role_comparison":
        errors.append("comparison_type must be local_evidence_role_comparison")
    for field in ["answer_language", "query", "generated_at", "confidence", "comparison_answer", "comparison_markdown"]:
        _require_nonempty_string(payload, field, errors)
    for field in ["filters", "routing", "coverage", "consensus"]:
        _require_dict(payload, field, errors)
    for field in ["divergences", "role_answers"]:
        _require_list(payload, field, errors)
    coverage = payload.get("coverage")
    if isinstance(coverage, dict):
        for field in ["role_count", "evidence_backed_role_count", "no_evidence_role_count", "evidence_count", "total_matches"]:
            if not isinstance(coverage.get(field), int):
                errors.append(f"coverage.{field} must be an integer")
    role_answers = payload.get("role_answers")
    if isinstance(role_answers, list):
        for index, item in enumerate(role_answers):
            if not isinstance(item, dict):
                errors.append(f"role_answers[{index}] must be an object")
                continue
            prefix = f"role_answers[{index}]"
            for field in ["role_id", "display_name", "status", "confidence", "dominant_stance"]:
                _require_string(item, f"{prefix}.{field}", errors)
            if not isinstance(item.get("answer"), dict):
                errors.append(f"{prefix}.answer must be an object")
    return errors


def _require_nonempty_string(payload: dict[str, Any], dotted_path: str, errors: list[str]) -> None:
    field = dotted_path.rsplit(".", 1)[-1]
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{dotted_path} must be a nonempty string")


def _require_string(payload: dict[str, Any], dotted_path: str, errors: list[str]) -> None:
    field = dotted_path.rsplit(".", 1)[-1]
    if not isinstance(payload.get(field), str):
        errors.append(f"{dotted_path} must be a string")


def _require_dict(payload: dict[str, Any], field: str, errors: list[str]) -> None:
    if not isinstance(payload.get(field), dict):
        errors.append(f"{field} must be an object")


def _require_list(payload: dict[str, Any], field: str, errors: list[str]) -> None:
    if not isinstance(payload.get(field), list):
        errors.append(f"{field} must be a list")


def _safe_int(value: object, default: int = 0) -> int:
    return value if isinstance(value, int) else default


def _safe_limit(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


def _malformed_comparison_export(json_path: Path, markdown_path: Path, error: str) -> dict[str, Any]:
    return {
        "query": "",
        "generated_at": "",
        "confidence": "",
        "answer_language": "",
        "comparison_answer": "",
        "schema_version": 0,
        "contract_errors": ["comparison.json must be valid object JSON"],
        "role_count": 0,
        "evidence_count": 0,
        "role_ids": [],
        "evidence_backed": False,
        "review_status": "draft",
        "reviewed_at": "",
        "reviewer": "",
        "review_notes": "",
        "reviewed": False,
        "adopted": False,
        "malformed": True,
        "error": error,
        "comparison_json": str(json_path),
        "comparison_markdown": str(markdown_path),
        "status": "malformed",
    }


def _slug(value: str) -> str:
    return re.sub(r"[^\w-]+", "-", value.lower(), flags=re.UNICODE).strip("-_")[:80]


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
