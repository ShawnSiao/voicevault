from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .kb import KnowledgeBase


def list_analysis_exports(kb: KnowledgeBase) -> list[dict[str, Any]]:
    exports: list[dict[str, Any]] = []
    if not kb.exports_dir.exists():
        return exports
    for json_path in sorted(kb.exports_dir.glob("*/analysis.json")):
        exports.append(_analysis_export_item(json_path))
    return sorted(exports, key=lambda item: (item["date"], item["event_id"], item["analysis_json"]), reverse=True)


def summarize_analysis_exports(exports: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(exports),
        "ready": sum(1 for item in exports if item["status"] == "ready"),
        "malformed": sum(1 for item in exports if item["status"] == "malformed"),
        "roles": sum(int(item.get("role_count") or 0) for item in exports if item["status"] == "ready"),
        "evidence": sum(int(item.get("evidence_count") or 0) for item in exports if item["status"] == "ready"),
    }


def _analysis_export_item(json_path: Path) -> dict[str, Any]:
    markdown_path = json_path.with_name("analysis.md")
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _malformed_item(json_path, markdown_path, str(exc))
    if not isinstance(payload, dict):
        return _malformed_item(json_path, markdown_path, "analysis.json must contain a JSON object.")
    contract_errors = _analysis_contract_errors(payload)
    if contract_errors:
        return _malformed_item(json_path, markdown_path, "; ".join(contract_errors))
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    role_analyses = payload.get("role_analyses") if isinstance(payload.get("role_analyses"), list) else []
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
    role_summaries = [_role_summary(item) for item in role_analyses if isinstance(item, dict)]
    evidence_summaries = [_evidence_summary(item) for item in evidence if isinstance(item, dict)]
    return {
        "status": "ready",
        "schema_version": int(payload.get("schema_version") or 1),
        "event_id": str(event.get("event_id") or json_path.parent.name),
        "title": str(event.get("title") or json_path.parent.name),
        "date": str(event.get("date") or ""),
        "symbols": _string_list(event.get("symbols")),
        "topics": _string_list(event.get("topics")),
        "role_count": len(role_summaries),
        "evidence_count": len([item for item in evidence if isinstance(item, dict)]),
        "consensus": _string_list(payload.get("consensus")),
        "disagreements": _string_list(payload.get("disagreements")),
        "uncertainty": _string_list(payload.get("uncertainty")),
        "synthesis_markdown": str(payload.get("synthesis_markdown") or ""),
        "role_summaries": role_summaries,
        "evidence_summaries": evidence_summaries,
        "analysis_json": str(json_path),
        "analysis_markdown": str(markdown_path),
        "error": "",
    }


def _role_summary(payload: dict[str, Any]) -> dict[str, Any]:
    supporting = payload.get("supporting_evidence") if isinstance(payload.get("supporting_evidence"), list) else []
    tension = payload.get("tension_evidence") if isinstance(payload.get("tension_evidence"), list) else []
    supporting_summaries = [_evidence_summary(item) for item in supporting if isinstance(item, dict)]
    tension_summaries = [_evidence_summary(item) for item in tension if isinstance(item, dict)]
    return {
        "role_id": str(payload.get("role_id") or ""),
        "display_name": str(payload.get("display_name") or payload.get("role_id") or ""),
        "profile_status": str(payload.get("profile_status") or ""),
        "stance": str(payload.get("stance") or "unclear"),
        "confidence": str(payload.get("confidence") or ""),
        "time_horizon": str(payload.get("time_horizon") or ""),
        "conclusion": str(payload.get("conclusion") or ""),
        "evidence_count": len(supporting_summaries),
        "tension_count": len(tension_summaries),
        "supporting_evidence": supporting_summaries,
        "tension_evidence": tension_summaries,
    }


def _analysis_contract_errors(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    schema_version = payload.get("schema_version", 1)
    if schema_version != 1:
        errors.append("schema_version must be 1 when present")

    event = payload.get("event")
    if not isinstance(event, dict):
        errors.append("event must be an object")
    else:
        _require_nonempty_string(event, "event.event_id", errors)
        _require_nonempty_string(event, "event.title", errors)
        _require_nonempty_string(event, "event.date", errors)

    role_analyses = payload.get("role_analyses")
    if not isinstance(role_analyses, list) or not role_analyses:
        errors.append("role_analyses must be a nonempty list")
    else:
        for index, item in enumerate(role_analyses):
            if not isinstance(item, dict):
                errors.append(f"role_analyses[{index}] must be an object")
                continue
            prefix = f"role_analyses[{index}]"
            for field in ["role_id", "display_name", "stance", "confidence", "time_horizon", "conclusion"]:
                _require_nonempty_string(item, f"{prefix}.{field}", errors)
            for field in ["supporting_evidence", "tension_evidence", "uncertainty"]:
                _require_list(item, f"{prefix}.{field}", errors)

    for field in ["evidence", "consensus", "disagreements", "minority_views", "uncertainty"]:
        _require_list(payload, field, errors)
    _require_nonempty_string(payload, "synthesis_markdown", errors)

    evidence = payload.get("evidence")
    if isinstance(evidence, list):
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                errors.append(f"evidence[{index}] must be an object")
                continue
            prefix = f"evidence[{index}]"
            for field in ["statement_id", "role_id", "source_url", "excerpt"]:
                _require_nonempty_string(item, f"{prefix}.{field}", errors)

    return errors


def _require_nonempty_string(payload: dict[str, Any], dotted_path: str, errors: list[str]) -> None:
    field = dotted_path.rsplit(".", 1)[-1]
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{dotted_path} must be a nonempty string")


def _require_list(payload: dict[str, Any], dotted_path: str, errors: list[str]) -> None:
    field = dotted_path.rsplit(".", 1)[-1]
    if not isinstance(payload.get(field), list):
        errors.append(f"{dotted_path} must be a list")


def _evidence_summary(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "statement_id": str(payload.get("statement_id") or ""),
        "role_id": str(payload.get("role_id") or ""),
        "source_type": str(payload.get("source_type") or ""),
        "source_platform": str(payload.get("source_platform") or ""),
        "source_user_id": str(payload.get("source_user_id") or ""),
        "source_author": str(payload.get("source_author") or ""),
        "source_url": str(payload.get("source_url") or ""),
        "published_at": str(payload.get("published_at") or ""),
        "captured_at": str(payload.get("captured_at") or ""),
        "title": str(payload.get("title") or ""),
        "excerpt": str(payload.get("excerpt") or ""),
        "stance": str(payload.get("stance") or ""),
    }


def _malformed_item(json_path: Path, markdown_path: Path, error: str) -> dict[str, Any]:
    return {
        "status": "malformed",
        "schema_version": 0,
        "event_id": json_path.parent.name,
        "title": json_path.parent.name,
        "date": "",
        "symbols": [],
        "topics": [],
        "role_count": 0,
        "evidence_count": 0,
        "consensus": [],
        "disagreements": [],
        "uncertainty": [],
        "synthesis_markdown": "",
        "role_summaries": [],
        "evidence_summaries": [],
        "analysis_json": str(json_path),
        "analysis_markdown": str(markdown_path),
        "error": error,
    }


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]
