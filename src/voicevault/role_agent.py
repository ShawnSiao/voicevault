from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .kb import KnowledgeBase
from .role_skill import audit_role_skill_coverage, distill_role_skill, load_role_skill, write_role_skill
from .search import search_statements


ROLE_AGENT_SCHEMA_VERSION = 1
ROLE_AGENT_EXPORT_STATUS_CHOICES = ("all", "prompt_only", "completed", "failed", "malformed")
ROLE_AGENT_QUALITY_STATUS_CHOICES = ("prompt_only", "deliverable", "invalid_completed", "failed", "malformed")


def build_role_agent_prompt(
    kb: KnowledgeBase,
    role_id: str,
    query: str,
    *,
    symbol: str = "",
    topic: str = "",
    limit: int = 5,
) -> dict[str, Any]:
    normalized_role_id = role_id.strip()
    normalized_query = query.strip()
    if not normalized_role_id:
        raise ValueError("role_id is required.")
    if not normalized_query:
        raise ValueError("query is required.")
    try:
        skill = load_role_skill(kb, normalized_role_id)
        skill_ready = True
        skill_path = str(kb.roles_dir / normalized_role_id / "skill" / "role.skill.json")
    except FileNotFoundError:
        skill = distill_role_skill(kb, normalized_role_id)
        output = write_role_skill(kb, skill)
        skill_ready = True
        skill_path = str(output["skill_json"])
    search = search_statements(
        kb,
        normalized_query,
        role_id=normalized_role_id,
        symbol=symbol,
        topic=topic,
        limit=limit,
    )
    evidence = [_evidence_item(index, item) for index, item in enumerate(search["results"], start=1)]
    payload = {
        "query": normalized_query,
        "role_skill": _compact_skill(skill),
        "related_evidence": evidence,
        "answer_contract": skill.get("prompt_contract", {}).get("output_schema", {}),
        "instructions": [
            "请用中文回答。",
            "不是复制粘贴证据；要先运用 role_skill 中的知识体系和判断框架。",
            "把 evidence_backed_claims 与 framework_projection 分开。",
            "没有直接证据时仍可给出框架化推演，但必须说明证据缺口和不确定性。",
            "不要声称自己就是该公开角色，也不要伪造实时观点、持仓、交易或私下信息。",
        ],
    }
    system = str(skill.get("prompt_contract", {}).get("system") or "")
    developer = str(skill.get("prompt_contract", {}).get("developer") or "")
    return {
        "schema_version": ROLE_AGENT_SCHEMA_VERSION,
        "answer_type": "role_agent_prompt",
        "role_id": normalized_role_id,
        "query": normalized_query,
        "filters": {"symbol": symbol, "topic": topic, "limit": limit},
        "generated_at": _now_utc(),
        "coverage": {
            "skill_ready": skill_ready,
            "skill_path": skill_path,
            "evidence_count": len(evidence),
            "total_matches": int(search.get("total_matches") or 0),
        },
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": developer + "\n\n" + json.dumps(payload, ensure_ascii=False, indent=2),
            },
        ],
        "role_skill": {
            "role_id": normalized_role_id,
            "display_name": skill.get("display_name", normalized_role_id),
            "skill_json": skill_path,
        },
        "evidence": evidence,
    }


def ask_role_agent(
    kb: KnowledgeBase,
    role_id: str,
    query: str,
    *,
    symbol: str = "",
    topic: str = "",
    limit: int = 5,
    dry_run: bool = True,
    llm_client: Any | None = None,
    model: str = "",
    temperature: float = 0.2,
) -> dict[str, Any]:
    prompt_bundle = build_role_agent_prompt(kb, role_id, query, symbol=symbol, topic=topic, limit=limit)
    result: dict[str, Any] = {
        "schema_version": ROLE_AGENT_SCHEMA_VERSION,
        "ok": True,
        "answer_type": "role_agent_answer",
        "role_id": role_id.strip(),
        "query": query.strip(),
        "generated_at": _now_utc(),
        "prompt_bundle": prompt_bundle,
        "llm": {"status": "not_called" if dry_run else "pending", "model": model},
        "answer": None,
    }
    if not dry_run:
        try:
            client = llm_client or EnvRoleAgentClient.from_env()
            answer = client.complete(
                messages=prompt_bundle["messages"],
                model=model or EnvRoleAgentClient.default_model(),
                temperature=temperature,
            )
        except Exception as exc:
            result["ok"] = False
            result["llm"] = {
                "status": "failed",
                "model": model or EnvRoleAgentClient.default_model(),
                "temperature": temperature,
                "error": str(exc),
            }
        else:
            result["llm"] = {"status": "completed", "model": model or EnvRoleAgentClient.default_model(), "temperature": temperature}
            result["answer"] = _normalize_answer(answer)
    output = write_role_agent_output(default_role_agent_dir(kb, role_id, query), result)
    result["role_agent_json"] = str(output["role_agent_json"])
    result["role_agent_markdown"] = str(output["role_agent_markdown"])
    return result


def write_role_agent_output(out_dir: Path, result: dict[str, Any]) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "role-agent.json"
    markdown_path = out_dir / "role-agent.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    markdown_path.write_text(_role_agent_markdown(result), encoding="utf-8", newline="\n")
    return {"role_agent_json": json_path, "role_agent_markdown": markdown_path}


def list_role_agent_exports(kb: KnowledgeBase, *, status: str = "all") -> list[dict[str, Any]]:
    _validate_status(status)
    root = kb.exports_dir / "role-agent"
    if not root.exists():
        return []
    rows = [inspect_role_agent_export(path) for path in sorted(root.glob("*/*/role-agent.json"))]
    rows.sort(key=lambda item: (item.get("generated_at", ""), item.get("query", "")), reverse=True)
    if status == "all":
        return rows
    return [row for row in rows if row.get("status") == status]


def summarize_role_agent_exports(exports: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(exports),
        "prompt_only": len([item for item in exports if item.get("status") == "prompt_only"]),
        "completed": len([item for item in exports if item.get("status") == "completed"]),
        "failed": len([item for item in exports if item.get("status") == "failed"]),
        "malformed": len([item for item in exports if item.get("status") == "malformed"]),
    }


def audit_role_agent_exports(kb: KnowledgeBase) -> dict[str, Any]:
    root = kb.exports_dir / "role-agent"
    paths = sorted(root.glob("*/*/role-agent.json")) if root.exists() else []
    items = [_audit_role_agent_export(path) for path in paths]
    items.sort(key=lambda item: (item.get("generated_at", ""), item.get("query", "")), reverse=True)
    summary = {
        "total": len(items),
        "prompt_only": len([item for item in items if item.get("quality_status") == "prompt_only"]),
        "completed": len([item for item in items if item.get("status") == "completed"]),
        "deliverable": len([item for item in items if item.get("quality_status") == "deliverable"]),
        "failed": len([item for item in items if item.get("quality_status") == "failed"]),
        "malformed": len([item for item in items if item.get("quality_status") == "malformed"]),
        "invalid_completed": len([item for item in items if item.get("quality_status") == "invalid_completed"]),
        "evidence_backed_completed": len(
            [item for item in items if item.get("status") == "completed" and int(item.get("evidence_count") or 0) > 0]
        ),
    }
    ok = summary["failed"] == 0 and summary["malformed"] == 0 and summary["invalid_completed"] == 0
    remediation: list[str] = []
    if summary["failed"]:
        remediation.append("Fix Role Agent runtime configuration, then rerun failed role questions with --call-llm.")
    if summary["invalid_completed"]:
        remediation.append("Regenerate invalid completed Role Agent answers with a JSON response following the Role Skill contract.")
    if summary["malformed"]:
        remediation.append("Remove or regenerate malformed role-agent.json artifacts.")
    return {
        "schema_version": ROLE_AGENT_SCHEMA_VERSION,
        "ok": ok,
        "root": str(root),
        "runtime": inspect_role_agent_runtime(),
        "summary": summary,
        "items": items,
        "remediation": remediation,
    }


def audit_role_agent_readiness(
    kb: KnowledgeBase,
    *,
    require_live: bool = False,
    min_deliverable_per_role: int = 1,
) -> dict[str, Any]:
    role_skill_coverage = audit_role_skill_coverage(kb)
    quality = audit_role_agent_exports(kb)
    runtime = quality["runtime"]
    ready_role_ids = [str(role_id) for role_id in role_skill_coverage.get("ready_role_ids", [])]
    skill_rows = {
        str(item.get("role_id") or ""): item
        for item in (role_skill_coverage.get("skills", {}).get("skills", []) if isinstance(role_skill_coverage.get("skills"), dict) else [])
    }
    items_by_role: dict[str, list[dict[str, Any]]] = {}
    for item in quality["items"]:
        role_id = str(item.get("role_id") or "")
        if role_id:
            items_by_role.setdefault(role_id, []).append(item)

    roles = [
        _role_agent_readiness_row(
            kb,
            role_id,
            skill_rows.get(role_id, {}),
            items_by_role.get(role_id, []),
            require_live=require_live,
            runtime_configured=bool(runtime.get("configured")),
            min_deliverable_per_role=min_deliverable_per_role,
        )
        for role_id in ready_role_ids
    ]
    roles.sort(key=lambda item: (item.get("status", ""), item.get("role_id", "")))
    live_ok = bool(ready_role_ids) and all(item["status"] == "live_ready" for item in roles)
    acceptable = {"live_ready"} if require_live else {"live_ready", "prompt_ready"}
    ok = (
        bool(ready_role_ids)
        and bool(role_skill_coverage["ok"])
        and bool(quality["ok"])
        and all(item["status"] in acceptable for item in roles)
    )
    remediation = _unique_strings(
        [
            command
            for command in list(role_skill_coverage.get("remediation", [])) + list(quality.get("remediation", []))
            if isinstance(command, str)
        ]
        + [command for role in roles for command in role.get("remediation", [])]
    )
    summary = {
        "ready_roles": len(ready_role_ids),
        "role_skills_missing": int(role_skill_coverage["summary"]["missing"]),
        "roles_live_ready": len([item for item in roles if item["status"] == "live_ready"]),
        "roles_prompt_ready": len([item for item in roles if item["status"] == "prompt_ready"]),
        "roles_missing_prompt": len([item for item in roles if item["status"] == "missing_prompt"]),
        "roles_missing_live": len([item for item in roles if item["status"] != "live_ready"]),
        "roles_blocked_runtime": len([item for item in roles if item["status"] == "blocked_runtime"]),
        "roles_blocked_quality": len([item for item in roles if item["status"] == "blocked_quality"]),
        "deliverable": quality["summary"]["deliverable"],
        "prompt_only": quality["summary"]["prompt_only"],
        "failed": quality["summary"]["failed"],
        "malformed": quality["summary"]["malformed"],
        "invalid_completed": quality["summary"]["invalid_completed"],
    }
    return {
        "schema_version": ROLE_AGENT_SCHEMA_VERSION,
        "ok": ok,
        "live_ok": live_ok,
        "require_live": require_live,
        "root": str(kb.exports_dir / "role-agent"),
        "runtime": runtime,
        "summary": summary,
        "roles": roles,
        "quality": quality,
        "role_skill_coverage": role_skill_coverage,
        "remediation": remediation,
    }


def inspect_role_agent_runtime() -> dict[str, Any]:
    endpoint = os.environ.get("VOICEVAULT_LLM_ENDPOINT", "").strip()
    base_url = os.environ.get("VOICEVAULT_LLM_BASE_URL", "").strip().rstrip("/")
    endpoint_source = "VOICEVAULT_LLM_ENDPOINT" if endpoint else ("VOICEVAULT_LLM_BASE_URL" if base_url else "")
    endpoint_configured = bool(endpoint or base_url)
    api_key_configured = bool(os.environ.get("VOICEVAULT_LLM_API_KEY", "").strip())
    remediation: list[str] = []
    if not endpoint_configured:
        remediation.append("Set VOICEVAULT_LLM_ENDPOINT or VOICEVAULT_LLM_BASE_URL before calling an external LLM.")
    return {
        "schema_version": ROLE_AGENT_SCHEMA_VERSION,
        "configured": endpoint_configured,
        "endpoint_configured": endpoint_configured,
        "endpoint_source": endpoint_source,
        "api_key_configured": api_key_configured,
        "model": EnvRoleAgentClient.default_model(),
        "remediation": remediation,
    }


def inspect_role_agent_export(path: Path) -> dict[str, Any]:
    markdown_path = path.with_name("role-agent.md")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("role-agent.json must contain an object.")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {
            "schema_version": 0,
            "status": "malformed",
            "role_id": "",
            "query": "",
            "generated_at": "",
            "llm_status": "",
            "evidence_count": 0,
            "role_agent_json": str(path),
            "role_agent_markdown": str(markdown_path),
            "error": str(exc),
        }
    llm = payload.get("llm") if isinstance(payload.get("llm"), dict) else {}
    prompt = payload.get("prompt_bundle") if isinstance(payload.get("prompt_bundle"), dict) else {}
    coverage = prompt.get("coverage") if isinstance(prompt.get("coverage"), dict) else {}
    status = _export_status(payload)
    return {
        "schema_version": int(payload.get("schema_version") or 0),
        "status": status,
        "role_id": str(payload.get("role_id") or ""),
        "query": str(payload.get("query") or ""),
        "generated_at": str(payload.get("generated_at") or ""),
        "llm_status": str(llm.get("status") or ""),
        "model": str(llm.get("model") or ""),
        "evidence_count": int(coverage.get("evidence_count") or 0),
        "role_agent_json": str(path),
        "role_agent_markdown": str(markdown_path),
        "error": str(llm.get("error") or ""),
    }


def default_role_agent_dir(kb: KnowledgeBase, role_id: str, query: str) -> Path:
    return kb.exports_dir / "role-agent" / (_slug(role_id) or "role") / (_slug(query) or "question")


class EnvRoleAgentClient:
    def __init__(self, endpoint: str, api_key: str = "") -> None:
        self.endpoint = endpoint
        self.api_key = api_key

    @classmethod
    def from_env(cls) -> "EnvRoleAgentClient":
        endpoint = os.environ.get("VOICEVAULT_LLM_ENDPOINT", "").strip()
        if not endpoint:
            base_url = os.environ.get("VOICEVAULT_LLM_BASE_URL", "").strip().rstrip("/")
            if base_url:
                endpoint = f"{base_url}/chat/completions"
        if not endpoint:
            raise RuntimeError("Set VOICEVAULT_LLM_ENDPOINT or VOICEVAULT_LLM_BASE_URL before calling an external LLM.")
        return cls(endpoint, api_key=os.environ.get("VOICEVAULT_LLM_API_KEY", "").strip())

    @staticmethod
    def default_model() -> str:
        return os.environ.get("VOICEVAULT_LLM_MODEL", "").strip() or "voicevault-external-model"

    def complete(self, *, messages: list[dict[str, str]], model: str, temperature: float) -> dict[str, Any]:
        body = json.dumps(
            {"model": model, "messages": messages, "temperature": temperature, "response_format": {"type": "json_object"}},
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"external LLM request failed: HTTP {exc.code} {detail}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"external LLM request failed: {exc}") from exc
        content = _choice_content(payload)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = {"answer": content}
        if not isinstance(parsed, dict):
            return {"answer": str(parsed)}
        return parsed


def _compact_skill(skill: dict[str, Any]) -> dict[str, Any]:
    return {
        "role_id": skill.get("role_id"),
        "display_name": skill.get("display_name"),
        "source_statement_count": skill.get("source_statement_count"),
        "knowledge_system": skill.get("knowledge_system", {}),
        "answer_policy": skill.get("answer_policy", {}),
    }


def _evidence_item(index: int, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": f"[{index}]",
        "statement_id": str(item.get("statement_id") or ""),
        "role_id": str(item.get("role_id") or ""),
        "title": str(item.get("title") or ""),
        "source_url": str(item.get("source_url") or ""),
        "published_at": str(item.get("published_at") or ""),
        "excerpt": str(item.get("excerpt") or ""),
    }


def _normalize_answer(answer: Any) -> dict[str, Any]:
    if isinstance(answer, dict):
        payload = dict(answer)
    else:
        payload = {"answer": str(answer)}
    payload.setdefault("mode", "external_llm_role_agent")
    payload.setdefault("evidence_backed_claims", [])
    payload.setdefault("framework_inference", "")
    payload.setdefault("uncertainty", [])
    payload.setdefault("citations", [])
    return payload


def _role_agent_markdown(result: dict[str, Any]) -> str:
    prompt = result.get("prompt_bundle") if isinstance(result.get("prompt_bundle"), dict) else {}
    answer = result.get("answer") if isinstance(result.get("answer"), dict) else {}
    lines = [
        "# VoiceVault Role Agent",
        "",
        f"- Role: `{result.get('role_id', '')}`",
        f"- Query: {result.get('query', '')}",
        f"- LLM status: {(result.get('llm') or {}).get('status', '') if isinstance(result.get('llm'), dict) else ''}",
        f"- Evidence: {(prompt.get('coverage') or {}).get('evidence_count', 0) if isinstance(prompt.get('coverage'), dict) else 0}",
        "",
    ]
    if answer:
        lines.extend(["## Answer", "", str(answer.get("answer") or "")])
        if answer.get("framework_inference"):
            lines.extend(["", "## Framework Inference", "", str(answer["framework_inference"])])
    else:
        lines.extend(["## Prompt Preview", "", "External LLM was not called."])
    return "\n".join(lines).strip() + "\n"


def _choice_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
    if not choices:
        return json.dumps(payload, ensure_ascii=False)
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    return str(message.get("content") or first.get("text") or "")


def _export_status(payload: dict[str, Any]) -> str:
    if payload.get("schema_version") != ROLE_AGENT_SCHEMA_VERSION:
        return "malformed"
    llm = payload.get("llm") if isinstance(payload.get("llm"), dict) else {}
    llm_status = str(llm.get("status") or "")
    if llm_status == "completed":
        return "completed"
    if llm_status == "failed":
        return "failed"
    return "prompt_only"


def _audit_role_agent_export(path: Path) -> dict[str, Any]:
    row = inspect_role_agent_export(path)
    if row["status"] == "malformed":
        row["quality_status"] = "malformed"
        row["failed_checks"] = ["valid_json"]
        return row
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("role-agent.json must contain an object.")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        row["quality_status"] = "malformed"
        row["failed_checks"] = [str(exc)]
        return row
    status = str(row.get("status") or "")
    if status in ("prompt_only", "failed"):
        row["quality_status"] = status
        row["failed_checks"] = []
        return row
    failed_checks = _completed_answer_failed_checks(payload)
    row["quality_status"] = "deliverable" if not failed_checks else "invalid_completed"
    row["failed_checks"] = failed_checks
    answer = payload.get("answer") if isinstance(payload.get("answer"), dict) else {}
    row["answer_excerpt"] = str(answer.get("answer") or "")[:320]
    return row


def _role_agent_readiness_row(
    kb: KnowledgeBase,
    role_id: str,
    skill: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    require_live: bool,
    runtime_configured: bool,
    min_deliverable_per_role: int,
) -> dict[str, Any]:
    display_name = str(skill.get("display_name") or role_id)
    deliverable = [item for item in items if item.get("quality_status") == "deliverable"]
    evidence_prompt = [
        item
        for item in items
        if item.get("quality_status") == "prompt_only" and int(item.get("evidence_count") or 0) > 0
    ]
    failed = [item for item in items if item.get("quality_status") == "failed"]
    malformed = [item for item in items if item.get("quality_status") == "malformed"]
    invalid_completed = [item for item in items if item.get("quality_status") == "invalid_completed"]
    query = _role_agent_suggested_query(role_id, display_name, items)
    remediation: list[str] = []
    if skill.get("status") != "ready":
        status = "missing_skill"
        remediation.append(f"voicevault role distill --kb {kb.root} --role {role_id} --json")
    elif failed or malformed or invalid_completed:
        status = "blocked_quality"
        remediation.append(f"voicevault role agents --kb {kb.root} --status failed --json")
    elif len(deliverable) >= max(1, min_deliverable_per_role):
        status = "live_ready"
    elif not evidence_prompt:
        status = "missing_prompt"
        remediation.append(
            f'voicevault role ask --kb {kb.root} --role {role_id} --query "{query}" --dry-run --json'
        )
    elif require_live and not runtime_configured:
        status = "blocked_runtime"
        remediation.append(
            "Set VOICEVAULT_LLM_ENDPOINT or VOICEVAULT_LLM_BASE_URL, then "
            f'voicevault role ask --kb {kb.root} --role {role_id} --query "{query}" --call-llm --json'
        )
    elif require_live:
        status = "needs_live_answer"
        remediation.append(
            f'voicevault role ask --kb {kb.root} --role {role_id} --query "{query}" --call-llm --json'
        )
    else:
        status = "prompt_ready"
        remediation.append(
            f'voicevault role ask --kb {kb.root} --role {role_id} --query "{query}" --call-llm --json'
        )
    return {
        "role_id": role_id,
        "display_name": display_name,
        "status": status,
        "require_live": require_live,
        "counts": {
            "exports": len(items),
            "prompt_ready": len(evidence_prompt),
            "deliverable": len(deliverable),
            "failed": len(failed),
            "malformed": len(malformed),
            "invalid_completed": len(invalid_completed),
        },
        "latest_prompt_json": _first_path(evidence_prompt),
        "latest_deliverable_json": _first_path(deliverable),
        "suggested_query": query,
        "remediation": remediation,
    }


def _role_agent_suggested_query(role_id: str, display_name: str, items: list[dict[str, Any]]) -> str:
    for item in items:
        query = str(item.get("query") or "").strip()
        if query:
            return query
    label = display_name or role_id
    return f"{label} 的核心判断框架是什么？"


def _first_path(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    return str(items[0].get("role_agent_json") or "")


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _completed_answer_failed_checks(payload: dict[str, Any]) -> list[str]:
    checks: list[str] = []
    if payload.get("schema_version") != ROLE_AGENT_SCHEMA_VERSION:
        checks.append("schema_version")
    llm = payload.get("llm") if isinstance(payload.get("llm"), dict) else {}
    if llm.get("status") != "completed":
        checks.append("llm_completed")
    answer = payload.get("answer") if isinstance(payload.get("answer"), dict) else {}
    if answer.get("mode") != "external_llm_role_agent":
        checks.append("answer_mode")
    if not str(answer.get("answer") or "").strip():
        checks.append("answer_text")
    claims = answer.get("evidence_backed_claims")
    if not isinstance(claims, list) or not claims:
        checks.append("evidence_backed_claims")
    if not str(answer.get("framework_inference") or "").strip():
        checks.append("framework_inference")
    prompt = payload.get("prompt_bundle") if isinstance(payload.get("prompt_bundle"), dict) else {}
    coverage = prompt.get("coverage") if isinstance(prompt.get("coverage"), dict) else {}
    if int(coverage.get("evidence_count") or 0) <= 0:
        checks.append("evidence_count")
    if _answer_impersonates_role(payload, answer):
        checks.append("role_impersonation")
    return checks


def _answer_impersonates_role(payload: dict[str, Any], answer: dict[str, Any]) -> bool:
    text = json.dumps(answer, ensure_ascii=False).lower()
    prompt = payload.get("prompt_bundle") if isinstance(payload.get("prompt_bundle"), dict) else {}
    role_skill = prompt.get("role_skill") if isinstance(prompt.get("role_skill"), dict) else {}
    names = [
        str(payload.get("role_id") or "").strip(),
        str(role_skill.get("display_name") or "").strip(),
    ]
    for name in [item for item in names if item]:
        name_lower = name.lower()
        if f"我是{name_lower}" in text or f"我是 {name_lower}" in text or f"i am {name_lower}" in text:
            return True
    return False


def _validate_status(status: str) -> None:
    if status not in ROLE_AGENT_EXPORT_STATUS_CHOICES:
        raise ValueError(f"Unknown role agent export status: {status}")


def _slug(value: str) -> str:
    return re.sub(r"[^\w-]+", "-", value.lower(), flags=re.UNICODE).strip("-_")[:80]


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
