from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .index import VoiceVaultIndex
from .kb import KnowledgeBase
from .models import Statement
from .roles import evaluate_role_coverage, list_role_summaries


ROLE_SKILL_SCHEMA_VERSION = 1
ROLE_SKILL_ARTIFACT_TYPE = "voicevault_role_skill"

_ENGLISH_STOPWORDS = {
    "about",
    "after",
    "also",
    "because",
    "been",
    "being",
    "from",
    "have",
    "into",
    "more",
    "only",
    "that",
    "their",
    "there",
    "this",
    "when",
    "with",
    "would",
}
_NOISE_SUBSTRINGS = (
    "http",
    "www.",
    "xqimg",
    "imedao",
    "assets.",
    "emoji",
    "jpeg",
    ".jpg",
    ".png",
    ".gif",
    ".webp",
    "网页链接",
    "图片",
)
_CHINESE_CONCEPT_PHRASES = (
    "产业思维",
    "底线思维",
    "宏观思维",
    "宏观流动性",
    "风险管理",
    "市场周期",
    "地缘格局",
    "政治经济学",
    "行为经济学",
    "长期主义",
    "安全与发展",
    "竞争格局",
    "结构性行情",
    "金融强国",
    "法治与规范",
    "创造性破坏",
    "工业革命",
    "交易体系",
    "防守为主",
    "均值回归",
    "流动性挤压",
    "泡沫状态",
    "资产价格",
    "产能过剩",
    "利润率",
    "现金流",
    "估值",
)
_CHINESE_CONCEPT_KEYWORDS = (
    "思维",
    "主义",
    "周期",
    "格局",
    "风险",
    "泡沫",
    "流动性",
    "产能",
    "利润率",
    "估值",
    "现金流",
    "工业革命",
    "均值回归",
    "交易体系",
    "安全",
    "发展",
    "竞争",
    "改革",
)


def distill_role_skill(kb: KnowledgeBase, role_id: str, *, limit: int = 12) -> dict[str, Any]:
    normalized_role_id = role_id.strip()
    if not normalized_role_id:
        raise ValueError("role_id is required.")
    statements = VoiceVaultIndex(kb).statements_for_role(normalized_role_id)
    role = _role_summary(kb, normalized_role_id)
    selected = statements[-max(1, min(int(limit or 12), 50)) :]
    topics = Counter(topic for statement in statements for topic in statement.topics)
    symbols = Counter(symbol for statement in statements for symbol in statement.symbols)
    stances = Counter(statement.stance for statement in statements if statement.stance)
    horizons = Counter(statement.time_horizon for statement in statements if statement.time_horizon)
    return {
        "schema_version": ROLE_SKILL_SCHEMA_VERSION,
        "artifact_type": ROLE_SKILL_ARTIFACT_TYPE,
        "role_id": normalized_role_id,
        "display_name": str(role.get("display_name") or normalized_role_id),
        "profile_status": str(role.get("profile_status") or ""),
        "source_scope": "public_statements_only",
        "generated_at": _now_utc(),
        "source_statement_count": len(statements),
        "knowledge_system": {
            "focus_areas": _counter_names(topics, fallback=["No recurring focus areas found."]),
            "symbols": _counter_names(symbols),
            "stance_distribution": _counter_map(stances),
            "time_horizons": _counter_map(horizons),
            "decision_frameworks": _decision_frameworks(statements, topics, symbols, horizons),
            "style_markers": _style_markers(statements),
            "role_concepts": _role_concepts(statements, topics),
            "common_terms": _common_terms(statements),
        },
        "answer_policy": {
            "language": "zh-CN",
            "required_sections": [
                "direct_answer",
                "evidence_backed_claims",
                "framework_projection",
                "uncertainty",
                "citations",
            ],
            "must_not": [
                "Do not claim to be the public role.",
                "Do not invent private beliefs, holdings, trades, or real-time statements.",
                "Do not copy-paste evidence as the whole answer.",
            ],
            "grounding_rules": [
                "Use local evidence for direct claims about the role's historical public statements.",
                "When the exact concept is absent, apply the distilled framework and label it as framework_projection.",
                "Separate evidence-backed claims from inferred role-framework reasoning.",
            ],
        },
        "prompt_contract": {
            "system": (
                "You are VoiceVault Role Agent. Do not claim to be the public role. "
                "Answer as an analytical model distilled from public statements, grounded in the provided Role Skill and evidence."
            ),
            "developer": (
                "Use the role's decision frameworks, recurring concepts, risk preferences, and style markers. "
                "Produce a useful answer even when exact keyword evidence is thin, but clearly mark framework projection."
            ),
            "output_schema": {
                "mode": "external_llm_role_agent",
                "answer": "string",
                "evidence_backed_claims": [{"text": "string", "refs": ["[1]"]}],
                "framework_inference": "string",
                "uncertainty": ["string"],
                "citations": [{"ref": "[1]", "statement_id": "string", "source_url": "string"}],
            },
        },
        "source_statements": [_statement_reference(index, statement) for index, statement in enumerate(selected, start=1)],
    }


def write_role_skill(kb: KnowledgeBase, skill: dict[str, Any]) -> dict[str, Path]:
    role_id = str(skill.get("role_id") or "").strip()
    if not role_id:
        raise ValueError("skill.role_id is required.")
    skill_dir = _role_skill_dir(kb, role_id)
    skill_dir.mkdir(parents=True, exist_ok=True)
    json_path = skill_dir / "role.skill.json"
    markdown_path = skill_dir / "role.skill.md"
    json_path.write_text(json.dumps(skill, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    markdown_path.write_text(_skill_markdown(skill), encoding="utf-8", newline="\n")
    return {"skill_json": json_path, "skill_markdown": markdown_path}


def load_role_skill(kb: KnowledgeBase, role_id: str) -> dict[str, Any]:
    path = _role_skill_json(kb, role_id)
    if not path.is_file():
        raise FileNotFoundError(f"Role skill not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Role skill JSON must contain an object.")
    return payload


def list_role_skills(kb: KnowledgeBase) -> dict[str, Any]:
    rows = [_skill_summary(kb, role) for role in list_role_summaries(kb)]
    return {
        "schema_version": 1,
        "summary": {
            "total": len(rows),
            "ready": len([row for row in rows if row["status"] == "ready"]),
            "missing": len([row for row in rows if row["status"] == "missing"]),
            "malformed": len([row for row in rows if row["status"] == "malformed"]),
        },
        "skills": rows,
    }


def audit_role_skill_coverage(kb: KnowledgeBase) -> dict[str, Any]:
    coverage = evaluate_role_coverage(kb)
    skills = list_role_skills(kb)
    skills_by_role = {item["role_id"]: item for item in skills["skills"]}
    ready_role_ids = [str(role_id) for role_id in coverage.get("ready_role_ids", [])]
    missing_roles = []
    ready_skills = 0
    for role_id in ready_role_ids:
        item = skills_by_role.get(role_id, {})
        if item.get("status") == "ready":
            ready_skills += 1
            continue
        missing_roles.append(
            {
                "role_id": role_id,
                "status": str(item.get("status") or "missing"),
                "skill_json": str(_role_skill_json(kb, role_id)),
                "remediation": f"voicevault role distill --kb {kb.root} --role {role_id} --json",
            }
        )
    ok = bool(ready_role_ids) and not missing_roles
    return {
        "schema_version": 1,
        "ok": ok,
        "summary": {
            "ready_roles": len(ready_role_ids),
            "ready": ready_skills,
            "missing": len(missing_roles),
            "total_skills": skills["summary"]["total"],
        },
        "ready_role_ids": ready_role_ids,
        "missing_roles": missing_roles,
        "skills": skills,
    }


def _role_skill_dir(kb: KnowledgeBase, role_id: str) -> Path:
    return kb.roles_dir / role_id / "skill"


def _role_skill_json(kb: KnowledgeBase, role_id: str) -> Path:
    return _role_skill_dir(kb, role_id) / "role.skill.json"


def _role_skill_markdown(kb: KnowledgeBase, role_id: str) -> Path:
    return _role_skill_dir(kb, role_id) / "role.skill.md"


def _skill_summary(kb: KnowledgeBase, role: dict[str, Any]) -> dict[str, Any]:
    role_id = str(role.get("role_id") or "")
    json_path = _role_skill_json(kb, role_id)
    markdown_path = _role_skill_markdown(kb, role_id)
    if not json_path.is_file():
        return {
            "role_id": role_id,
            "display_name": str(role.get("display_name") or role_id),
            "status": "missing",
            "skill_json": str(json_path),
            "skill_markdown": str(markdown_path),
            "source_statement_count": 0,
            "generated_at": "",
        }
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        errors = _skill_contract_errors(payload, role_id=role_id)
    except (OSError, json.JSONDecodeError) as exc:
        payload = {}
        errors = [str(exc)]
    return {
        "role_id": role_id,
        "display_name": str(payload.get("display_name") or role.get("display_name") or role_id),
        "status": "ready" if not errors and markdown_path.is_file() else "malformed",
        "skill_json": str(json_path),
        "skill_markdown": str(markdown_path),
        "source_statement_count": int(payload.get("source_statement_count") or 0),
        "generated_at": str(payload.get("generated_at") or ""),
        "contract_errors": errors,
    }


def _skill_contract_errors(payload: Any, *, role_id: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["role skill must be a JSON object"]
    if payload.get("schema_version") != ROLE_SKILL_SCHEMA_VERSION:
        errors.append("schema_version must be 1")
    if payload.get("artifact_type") != ROLE_SKILL_ARTIFACT_TYPE:
        errors.append("artifact_type must be voicevault_role_skill")
    if payload.get("role_id") != role_id:
        errors.append("role_id must match role directory")
    if int(payload.get("source_statement_count") or 0) <= 0:
        errors.append("source_statement_count must be positive")
    if not isinstance(payload.get("knowledge_system"), dict):
        errors.append("knowledge_system must be an object")
    if not isinstance(payload.get("answer_policy"), dict):
        errors.append("answer_policy must be an object")
    if not isinstance(payload.get("prompt_contract"), dict):
        errors.append("prompt_contract must be an object")
    return errors


def _role_summary(kb: KnowledgeBase, role_id: str) -> dict[str, Any]:
    for role in list_role_summaries(kb):
        if str(role.get("role_id") or "") == role_id:
            return role
    return {"role_id": role_id, "display_name": role_id, "profile_status": "missing"}


def _counter_names(counter: Counter[str], *, fallback: list[str] | None = None) -> list[str]:
    values = [name for name, _ in counter.most_common(8) if name]
    return values or (fallback or [])


def _counter_map(counter: Counter[str]) -> dict[str, int]:
    return {name: count for name, count in counter.most_common() if name}


def _decision_frameworks(
    statements: list[Statement],
    topics: Counter[str],
    symbols: Counter[str],
    horizons: Counter[str],
) -> list[str]:
    frameworks: list[str] = []
    if topics:
        frameworks.append("先按主题框架拆解问题：" + "、".join(name for name, _ in topics.most_common(5)) + "。")
    if symbols:
        frameworks.append("将具体标的放回长期反复讨论的公司/资产上下文：" + "、".join(name for name, _ in symbols.most_common(5)) + "。")
    if horizons:
        frameworks.append("回答时保留时间维度，优先区分：" + "、".join(name for name, _ in horizons.most_common(4)) + "。")
    if statements:
        frameworks.append("先给条件化判断，再说明需要哪些公开证据才能提高置信度。")
    return frameworks or ["当前公开材料不足以蒸馏稳定框架，只能作为待补齐角色。"]


def _style_markers(statements: list[Statement]) -> list[str]:
    if not statements:
        return ["No style markers inferred."]
    markers = ["回答应保持条件化，不把单条 statement 扩张为绝对结论。"]
    if any(statement.stance == "mixed" for statement in statements):
        markers.append("经常保留双向风险，避免单边化表达。")
    if any(statement.time_horizon == "long_term" for statement in statements):
        markers.append("倾向把短期波动放到长期框架中解释。")
    return markers


def _common_terms(statements: list[Statement]) -> list[str]:
    words: Counter[str] = Counter()
    for statement in statements:
        clean_body = _text_without_capture_noise(statement.body)
        words.update(_extract_chinese_concepts(clean_body))
        for token in _english_term_tokens(clean_body):
            words[token] += 1
    return [word for word, _ in words.most_common(12)]


def _role_concepts(statements: list[Statement], topics: Counter[str]) -> list[str]:
    concepts: Counter[str] = Counter()
    for topic, count in topics.items():
        concepts[topic] += count
    for statement in statements:
        concepts.update(_extract_chinese_concepts(_text_without_capture_noise(statement.body)))
    return [concept for concept, _ in concepts.most_common(12) if not _is_noise_term(concept)]


def _text_without_capture_noise(text: str) -> str:
    cleaned = re.sub(r"https?://\S+", " ", str(text))
    cleaned = re.sub(r"\S+\.(?:jpg|jpeg|png|gif|webp)\S*", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\S*(?:xqimg|imedao|emoji|assets)\S*", " ", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("网页链接", " ").replace("图片:", " ").replace("图片：", " ")
    return cleaned


def _extract_chinese_concepts(text: str) -> Counter[str]:
    concepts: Counter[str] = Counter()
    for phrase in _CHINESE_CONCEPT_PHRASES:
        count = text.count(phrase)
        if count:
            concepts[phrase] += count * 3
    for segment in re.split(r"[\s,.;:!?，。；：！？、（）()《》“”\"'`]+", text):
        token = segment.strip()
        if not (2 <= len(token) <= 12):
            continue
        if not re.search(r"[\u4e00-\u9fff]", token):
            continue
        if _is_noise_term(token):
            continue
        if any(keyword in token for keyword in _CHINESE_CONCEPT_KEYWORDS):
            concepts[token] += 1
    return concepts


def _english_term_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", text):
        token = raw.strip("-_").lower()
        if len(token) < 4 or len(token) > 32:
            continue
        if token in _ENGLISH_STOPWORDS:
            continue
        if _is_noise_term(token):
            continue
        tokens.append(token)
    return tokens


def _is_noise_term(value: str) -> bool:
    normalized = value.lower()
    return any(fragment in normalized for fragment in _NOISE_SUBSTRINGS)


def _statement_reference(index: int, statement: Statement) -> dict[str, Any]:
    return {
        "ref": f"[{index}]",
        "statement_id": statement.statement_id,
        "title": statement.title,
        "source_url": statement.source_url,
        "published_at": statement.published_at,
        "topics": statement.topics,
        "symbols": statement.symbols,
        "stance": statement.stance,
        "time_horizon": statement.time_horizon,
        "excerpt": _excerpt(statement.body, 360),
    }


def _skill_markdown(skill: dict[str, Any]) -> str:
    knowledge = skill.get("knowledge_system") if isinstance(skill.get("knowledge_system"), dict) else {}
    policy = skill.get("answer_policy") if isinstance(skill.get("answer_policy"), dict) else {}
    lines = [
        "# VoiceVault Role Skill",
        "",
        "## Role Skill",
        "",
        f"- Role: {skill.get('display_name') or skill.get('role_id')}",
        f"- Role ID: `{skill.get('role_id')}`",
        f"- Source statements: {skill.get('source_statement_count', 0)}",
        "",
        "## Focus Areas",
        "",
        *_bullets(knowledge.get("focus_areas") or []),
        "",
        "## Decision Frameworks",
        "",
        *_bullets(knowledge.get("decision_frameworks") or []),
        "",
        "## Style Markers",
        "",
        *_bullets(knowledge.get("style_markers") or []),
        "",
        "## Role Concepts",
        "",
        *_bullets(knowledge.get("role_concepts") or []),
        "",
        "## Answer Policy",
        "",
        *_bullets(policy.get("grounding_rules") or []),
        "",
        "## Source Statements",
        "",
    ]
    for item in skill.get("source_statements") or []:
        lines.append(f"- {item.get('ref')} `{item.get('statement_id')}` {item.get('title')}: {item.get('excerpt')}")
    return "\n".join(lines).strip() + "\n"


def _bullets(values: list[Any]) -> list[str]:
    return [f"- {value}" for value in values] or ["- None"]


def _excerpt(text: str, limit: int) -> str:
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
