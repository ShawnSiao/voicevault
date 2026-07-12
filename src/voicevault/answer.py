from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .kb import KnowledgeBase
from .roles import list_role_summaries
from .search import search_statements


ANSWER_SCHEMA_VERSION = 1
ROLE_ANSWER_SCHEMA_VERSION = 1
ANSWER_EXPORT_STATUS_CHOICES = ("all", "deliverable", "invalid", "evidence_backed", "no_evidence", "legacy_contract", "malformed")
DELIVERABLE_ANSWER_LANGUAGE = "zh-CN"


def answer_query(
    kb: KnowledgeBase,
    query: str,
    *,
    role_id: str = "",
    symbol: str = "",
    topic: str = "",
    limit: int = 5,
) -> dict[str, Any]:
    search = search_statements(kb, query, role_id=role_id, symbol=symbol, topic=topic, limit=limit)
    evidence = [_evidence_item(index, item) for index, item in enumerate(search["results"], start=1)]
    coverage = {
        "evidence_count": len(evidence),
        "total_matches": search["total_matches"],
        "role_count": len({item["role_id"] for item in evidence}),
    }
    citations = [_citation(item) for item in evidence]
    key_points = _key_points(evidence)
    answer_text = _answer_text(query, evidence, coverage)
    role_answer = _role_answer(kb, query, role_id=role_id, evidence=evidence, coverage=coverage)
    return {
        "schema_version": ANSWER_SCHEMA_VERSION,
        "answer_type": "local_evidence_answer",
        "answer_language": "zh-CN",
        "query": query,
        "filters": {"role_id": role_id, "symbol": symbol, "topic": topic, "limit": limit},
        "generated_at": _now_utc(),
        "confidence": _confidence(evidence),
        "coverage": coverage,
        "answer": answer_text,
        "answer_markdown": _answer_markdown(query, answer_text, role_answer, key_points, evidence),
        "role_answer": role_answer,
        "key_points": key_points,
        "citations": citations,
        "evidence": evidence,
        "uncertainty": _uncertainty(evidence),
        "search": search,
    }


def write_answer_outputs(out_dir: Path, result: dict[str, Any]) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "answer.json"
    markdown_path = out_dir / "answer.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    markdown_path.write_text(result["answer_markdown"], encoding="utf-8", newline="\n")
    return {"answer_json": json_path, "answer_markdown": markdown_path}


def list_answer_exports(kb: KnowledgeBase, *, status: str = "all") -> list[dict[str, Any]]:
    answers_dir = kb.exports_dir / "answers"
    if not answers_dir.exists():
        return []
    exports: list[dict[str, Any]] = []
    for json_path in sorted(answers_dir.glob("*/answer.json")):
        exports.append(inspect_answer_export(json_path))
    sorted_exports = sorted(exports, key=lambda item: (item["generated_at"], item["query"], item["answer_json"]), reverse=True)
    return filter_answer_exports(sorted_exports, status=status)


def inspect_answer_export(json_path: Path) -> dict[str, Any]:
    markdown_path = json_path.with_name("answer.md")
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _malformed_answer_export(json_path, markdown_path, str(exc))
    if not isinstance(payload, dict):
        return _malformed_answer_export(json_path, markdown_path, "answer.json must contain a JSON object.")

    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    evidence_count = _safe_int(coverage.get("evidence_count"))
    total_matches = _safe_int(coverage.get("total_matches"))
    citations = payload.get("citations") if isinstance(payload.get("citations"), list) else []
    citation_count = len(citations)
    key_points = payload.get("key_points") if isinstance(payload.get("key_points"), list) else []
    role_routing = payload.get("role_routing") if isinstance(payload.get("role_routing"), dict) else None
    role_answer = payload.get("role_answer") if isinstance(payload.get("role_answer"), dict) else None
    contract_errors = _answer_contract_errors(payload)
    item = {
        "query": str(payload.get("query") or ""),
        "generated_at": str(payload.get("generated_at") or ""),
        "confidence": str(payload.get("confidence") or ""),
        "answer": str(payload.get("answer") or ""),
        "answer_language": str(payload.get("answer_language") or ""),
        "filters": payload.get("filters") if isinstance(payload.get("filters"), dict) else {},
        "selected_role_id": str(payload.get("selected_role_id") or ""),
        "selection_mode": str(payload.get("selection_mode") or ""),
        "role_routing": role_routing,
        "role_answer": role_answer,
        "schema_version": _safe_int(payload.get("schema_version")),
        "contract_errors": contract_errors,
        "key_points": key_points,
        "evidence_count": evidence_count,
        "total_matches": total_matches,
        "citation_count": citation_count,
        "evidence_backed": evidence_count > 0 and citation_count > 0,
        "malformed": False,
        "error": "",
        "answer_json": str(json_path),
        "answer_markdown": str(markdown_path),
    }
    item["status"] = answer_export_status(item)
    return item


def filter_answer_exports(exports: list[dict[str, Any]], *, status: str = "all") -> list[dict[str, Any]]:
    _validate_answer_export_status(status)
    return [item for item in exports if _matches_answer_export_status(item, status)]


def summarize_answer_exports(exports: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(exports),
        "deliverable": len(filter_answer_exports(exports, status="deliverable")),
        "invalid": len(filter_answer_exports(exports, status="invalid")),
        "evidence_backed": len(filter_answer_exports(exports, status="evidence_backed")),
        "no_evidence": len(filter_answer_exports(exports, status="no_evidence")),
        "legacy_contract": len(filter_answer_exports(exports, status="legacy_contract")),
        "malformed": len(filter_answer_exports(exports, status="malformed")),
    }


def prune_answer_exports(kb: KnowledgeBase, *, status: str = "invalid", dry_run: bool = True) -> dict[str, Any]:
    all_exports = list_answer_exports(kb)
    matched = filter_answer_exports(all_exports, status=status)
    answers_root = (kb.exports_dir / "answers").resolve()
    result: dict[str, Any] = {
        "root": str(kb.root),
        "status": status,
        "dry_run": dry_run,
        "matched": len(matched),
        "removed": 0,
        "answers": matched,
        "errors": [],
    }
    for item in matched:
        answer_json = Path(item["answer_json"]).resolve()
        answer_dir = answer_json.parent
        if not _is_within(answer_dir, answers_root) or answer_dir == answers_root:
            result["errors"].append(f"Refusing to remove answer export outside answers root: {answer_dir}")
            continue
        if dry_run:
            continue
        if answer_dir.exists():
            shutil.rmtree(answer_dir)
            result["removed"] += 1
    return result


def is_deliverable_answer_export(item: dict[str, Any]) -> bool:
    return bool(
        not item.get("malformed")
        and not item.get("contract_errors")
        and item.get("evidence_backed")
        and item.get("answer_language") == DELIVERABLE_ANSWER_LANGUAGE
        and item.get("key_points")
    )


def answer_export_status(item: dict[str, Any]) -> str:
    if item.get("malformed"):
        return "malformed"
    if item.get("contract_errors"):
        return "legacy_contract"
    if is_deliverable_answer_export(item):
        return "deliverable"
    if not item.get("evidence_backed"):
        return "no_evidence"
    return "legacy_contract"


def default_answer_dir(kb: KnowledgeBase, query: str) -> Path:
    slug = _slug(query) or "answer"
    return kb.exports_dir / "answers" / slug


def _evidence_item(index: int, item: dict[str, Any]) -> dict[str, Any]:
    payload = dict(item)
    payload["ref"] = f"[{index}]"
    return payload


def _citation(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": item["ref"],
        "statement_id": item["statement_id"],
        "role_id": item["role_id"],
        "title": item["title"],
        "source_url": item["source_url"],
        "published_at": item["published_at"],
        "source_platform": item.get("source_platform", ""),
    }


def _answer_text(query: str, evidence: list[dict[str, Any]], coverage: dict[str, int]) -> str:
    if not evidence:
        return f"没有找到可引用的本地证据。声迹不能仅凭当前本地公开观点库回答「{query}」。"
    stance_counts = Counter(item.get("stance") or "unclear" for item in evidence)
    stance = stance_counts.most_common(1)[0][0]
    if stance == "unclear":
        stance_sentence = "这些证据没有形成明确的立场标签。"
    elif stance == "mixed":
        stance_sentence = "这些证据的立场是混合的，需要结合引用上下文阅读。"
    else:
        stance_sentence = f"最强的结构化立场信号是「{stance}」。"
    return (
        f"声迹在本地索引中找到 {len(evidence)} 条可引用证据，覆盖 {coverage['role_count']} 个角色，"
        f"总匹配 {coverage['total_matches']} 条。{stance_sentence}"
        "以下结论只整理已归档的公开 statement，不代表事实断言、投资建议或人物最新观点。"
    )


def _role_answer(
    kb: KnowledgeBase,
    query: str,
    *,
    role_id: str,
    evidence: list[dict[str, Any]],
    coverage: dict[str, int],
) -> dict[str, Any]:
    evidence_role_ids = [str(item.get("role_id") or "") for item in evidence if str(item.get("role_id") or "").strip()]
    unique_role_ids = sorted(set(evidence_role_ids))
    selected_role_id = str(role_id or "").strip()
    target_role_id = selected_role_id or (unique_role_ids[0] if len(unique_role_ids) == 1 else "")
    role = _role_summary(kb, target_role_id) if target_role_id else {}
    display_name = str(role.get("display_name") or target_role_id or "Multiple roles")
    mode = "single_role" if target_role_id else ("multi_role" if evidence else "no_evidence")
    refs = [str(item.get("ref") or "") for item in evidence if str(item.get("ref") or "").strip()]
    return {
        "schema_version": ROLE_ANSWER_SCHEMA_VERSION,
        "mode": mode,
        "role_id": target_role_id,
        "display_name": display_name,
        "profile_status": str(role.get("profile_status") or ""),
        "source_scope": "local_public_statements_only",
        "answer": _role_answer_text(query, display_name, mode=mode, evidence=evidence, coverage=coverage),
        "evidence_refs": refs,
        "limitations": [
            "只使用本机已同步的公开 statement。",
            "这是证据归纳，不是实时发言、身份模拟或投资建议。",
        ],
    }


def _role_answer_text(
    query: str,
    display_name: str,
    *,
    mode: str,
    evidence: list[dict[str, Any]],
    coverage: dict[str, int],
) -> str:
    if not evidence:
        return f"当前本机知识库没有足够证据归纳「{display_name}」对「{query}」的回答。"
    if mode == "single_role":
        lead = evidence[0]
        return (
            f"如果只依据「{display_name}」已归档的公开材料，"
            f"对「{query}」的回答可归纳为：{lead['excerpt']} "
            f"本回答引用 {len(evidence)} 条本机证据，需结合原文上下文审阅。"
        )
    return (
        f"本次问题没有收敛到单一角色，检索结果覆盖 {coverage['role_count']} 个角色。"
        "应先在页面选择明确角色，或使用 Auto route 后再审阅对应角色回答。"
    )


def _role_summary(kb: KnowledgeBase, role_id: str) -> dict[str, Any]:
    for role in list_role_summaries(kb):
        if str(role.get("role_id") or "") == role_id:
            return role
    return {"role_id": role_id, "display_name": role_id, "profile_status": ""}


def _key_points(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for item in evidence[:3]:
        published = item["published_at"] or item["captured_at"] or "unknown"
        points.append(
            {
                "text": f"{item['role_id']} 在「{item['title']}」中提到：{item['excerpt']}",
                "refs": [item["ref"]],
                "published_at": published,
            }
        )
    return points


def _answer_markdown(
    query: str,
    answer_text: str,
    role_answer: dict[str, Any],
    key_points: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> str:
    lines = ["# 证据答案", "", f"查询：{query}", "", "## 结论", "", answer_text]
    if role_answer.get("answer"):
        lines.extend(["", "## 角色回答", "", str(role_answer["answer"])])
    if not evidence:
        lines.extend(["", "## 关键证据", "", "没有找到可引用的本地证据。"])
        return "\n".join(lines).strip() + "\n"
    lines.extend(["", "## 关键证据", ""])
    for point in key_points:
        refs = " ".join(point["refs"])
        lines.append(f"- {refs} {point['text']}")
    lines.extend(["", "## 引用", ""])
    for item in evidence:
        source = item["source_url"] or "unknown source"
        lines.extend(
            [
                f"{item['ref']} {item['title']}",
                f"- Role: {item['role_id']}",
                f"- Published: {item['published_at'] or item['captured_at'] or 'unknown'}",
                f"- Source: {source}",
                f"- Excerpt: {item['excerpt']}",
                "",
            ]
        )
    lines.extend(["## 不确定性", "", *_markdown_bullets(_uncertainty(evidence))])
    return "\n".join(lines).strip() + "\n"


def _uncertainty(evidence: list[dict[str, Any]]) -> list[str]:
    if not evidence:
        return ["没有找到匹配的索引证据。"]
    values = ["该答案由本地规则生成，只总结已索引的公开 statement。"]
    if len(evidence) < 3:
        values.append("证据覆盖较薄，应把答案当作审阅起点。")
    if len({item.get("role_id") for item in evidence}) <= 1:
        values.append("证据来自单一角色，可能缺少分歧观点。")
    return values


def _confidence(evidence: list[dict[str, Any]]) -> str:
    if len(evidence) >= 5:
        return "high"
    if len(evidence) >= 2:
        return "medium"
    return "low"


def _markdown_bullets(values: list[str]) -> list[str]:
    return [f"- {value}" for value in values]


def _slug(value: str) -> str:
    return re.sub(r"[^\w-]+", "-", value.lower(), flags=re.UNICODE).strip("-_")[:80]


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _matches_answer_export_status(item: dict[str, Any], status: str) -> bool:
    if status == "all":
        return True
    if status == "invalid":
        return not is_deliverable_answer_export(item)
    if status == "evidence_backed":
        return bool(item.get("evidence_backed"))
    return item.get("status") == status


def _validate_answer_export_status(status: str) -> None:
    if status not in ANSWER_EXPORT_STATUS_CHOICES:
        choices = ", ".join(ANSWER_EXPORT_STATUS_CHOICES)
        raise ValueError(f"Unknown answer export status: {status}. Expected one of: {choices}")


def _answer_contract_errors(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("schema_version") != ANSWER_SCHEMA_VERSION:
        errors.append("schema_version must be 1")
    if payload.get("answer_type") != "local_evidence_answer":
        errors.append("answer_type must be local_evidence_answer")
    for field in ["answer_language", "query", "generated_at", "confidence", "answer", "answer_markdown"]:
        _require_nonempty_string(payload, field, errors)
    for field in ["filters", "coverage", "search"]:
        _require_dict(payload, field, errors)
    for field in ["key_points", "citations", "evidence", "uncertainty"]:
        _require_list(payload, field, errors)

    coverage = payload.get("coverage")
    if isinstance(coverage, dict):
        for field in ["evidence_count", "total_matches", "role_count"]:
            if not isinstance(coverage.get(field), int):
                errors.append(f"coverage.{field} must be an integer")

    key_points = payload.get("key_points")
    if isinstance(key_points, list):
        for index, item in enumerate(key_points):
            if not isinstance(item, dict):
                errors.append(f"key_points[{index}] must be an object")
                continue
            _require_nonempty_string(item, f"key_points[{index}].text", errors)
            if not isinstance(item.get("refs"), list):
                errors.append(f"key_points[{index}].refs must be a list")

    citations = payload.get("citations")
    if isinstance(citations, list):
        for index, item in enumerate(citations):
            if not isinstance(item, dict):
                errors.append(f"citations[{index}] must be an object")
                continue
            prefix = f"citations[{index}]"
            for field in ["ref", "statement_id", "role_id", "title", "published_at", "source_platform"]:
                _require_string(item, f"{prefix}.{field}", errors)
            _require_string(item, f"{prefix}.source_url", errors)

    evidence = payload.get("evidence")
    if isinstance(evidence, list):
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                errors.append(f"evidence[{index}] must be an object")
                continue
            prefix = f"evidence[{index}]"
            for field in ["ref", "statement_id", "role_id", "title", "source_url", "published_at", "captured_at", "excerpt"]:
                _require_string(item, f"{prefix}.{field}", errors)
    role_answer = payload.get("role_answer")
    if role_answer is not None:
        if not isinstance(role_answer, dict):
            errors.append("role_answer must be an object")
        else:
            if role_answer.get("schema_version") != ROLE_ANSWER_SCHEMA_VERSION:
                errors.append("role_answer.schema_version must be 1")
            for field in ["mode", "role_id", "display_name", "profile_status", "answer", "source_scope"]:
                if not isinstance(role_answer.get(field), str):
                    errors.append(f"role_answer.{field} must be a string")
            if not str(role_answer.get("answer") or "").strip():
                errors.append("role_answer.answer must be a nonempty string")
            if not isinstance(role_answer.get("evidence_refs"), list):
                errors.append("role_answer.evidence_refs must be a list")
            if not isinstance(role_answer.get("limitations"), list):
                errors.append("role_answer.limitations must be a list")
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


def _malformed_answer_export(json_path: Path, markdown_path: Path, error: str) -> dict[str, Any]:
    return {
        "query": "",
        "generated_at": "",
        "confidence": "",
        "answer": "",
        "answer_language": "",
        "filters": {},
        "selected_role_id": "",
        "selection_mode": "",
        "role_routing": None,
        "role_answer": None,
        "schema_version": 0,
        "contract_errors": ["answer.json must be valid object JSON"],
        "key_points": [],
        "evidence_count": 0,
        "total_matches": 0,
        "citation_count": 0,
        "evidence_backed": False,
        "malformed": True,
        "error": error,
        "answer_json": str(json_path),
        "answer_markdown": str(markdown_path),
        "status": "malformed",
    }


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
