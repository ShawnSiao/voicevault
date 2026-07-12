from __future__ import annotations

from collections import Counter
from typing import Any

from .index import VoiceVaultIndex
from .kb import KnowledgeBase
from .models import Event, Statement

KNOWN_STANCES = {"bullish", "bearish", "neutral", "mixed"}


def analyze_event(kb: KnowledgeBase, event: Event, roles: str | list[str] = "all") -> dict[str, Any]:
    index = VoiceVaultIndex(kb)
    selected_roles = index.list_roles() if roles == "all" else list(roles)
    relevant = index.query_relevant(event)
    role_analyses = [_analyze_role(kb, event, role_id, relevant.get(role_id, [])) for role_id in selected_roles]
    top_evidence = [evidence for analysis in role_analyses for evidence in analysis["supporting_evidence"]]
    return {
        "schema_version": 1,
        "event": event.to_dict(),
        "role_analyses": role_analyses,
        "consensus": _consensus(role_analyses),
        "disagreements": _disagreements(role_analyses),
        "minority_views": [],
        "synthesis_markdown": _synthesis(role_analyses),
        "evidence": top_evidence,
        "uncertainty": _uncertainty(role_analyses),
    }


def _analyze_role(kb: KnowledgeBase, event: Event, role_id: str, evidence: list[Statement]) -> dict[str, Any]:
    if not evidence:
        return {
            "role_id": role_id,
            "display_name": role_id.replace("-", " ").title(),
            "profile_status": _profile_status(kb, role_id),
            "stance": "unclear",
            "confidence": "low",
            "time_horizon": "unknown",
            "conclusion": "No relevant public evidence was found for this event.",
            "watch_variables": event.symbols + event.topics,
            "supporting_evidence": [],
            "tension_evidence": [],
            "historical_consistency": "unknown",
            "uncertainty": ["No relevant evidence found for this role and event."],
        }

    stance = _derive_stance(evidence)
    horizon = Counter(statement.time_horizon for statement in evidence if statement.time_horizon).most_common(1)
    conclusion = _conclusion(role_id, stance, event)
    return {
        "role_id": role_id,
        "display_name": role_id.replace("-", " ").title(),
        "profile_status": _profile_status(kb, role_id),
        "stance": stance,
        "confidence": "medium" if len(evidence) <= 2 else "high",
        "time_horizon": horizon[0][0] if horizon else "unknown",
        "conclusion": conclusion,
        "watch_variables": _watch_variables(event, evidence),
        "supporting_evidence": [_evidence_dict(statement) for statement in evidence[:5]],
        "tension_evidence": _tension_evidence(evidence, stance),
        "historical_consistency": "consistent_with_retrieved_evidence",
        "uncertainty": ["This is a deterministic local analysis, not a statement from the real person."],
    }


def _derive_stance(evidence: list[Statement]) -> str:
    stances = [statement.stance for statement in evidence if statement.stance in KNOWN_STANCES]
    if not stances:
        return "unclear"
    unique = set(stances)
    if "mixed" in unique or ("bullish" in unique and "bearish" in unique):
        return "mixed"
    return Counter(stances).most_common(1)[0][0]


def _conclusion(role_id: str, stance: str, event: Event) -> str:
    if stance == "mixed":
        return f"Based on retrieved public evidence, {role_id} would likely treat {event.title} as a mixed signal."
    if stance == "bullish":
        return f"Based on retrieved public evidence, {role_id} would likely focus on supportive long-term variables in {event.title}."
    if stance == "bearish":
        return f"Based on retrieved public evidence, {role_id} would likely focus on downside or risk variables in {event.title}."
    if stance == "neutral":
        return f"Based on retrieved public evidence, {role_id} would likely avoid a strong directional read on {event.title}."
    return f"Retrieved public evidence is insufficient to infer a view on {event.title}."


def _watch_variables(event: Event, evidence: list[Statement]) -> list[str]:
    variables: list[str] = []
    for value in event.symbols + event.topics:
        if value not in variables:
            variables.append(value)
    for statement in evidence:
        for value in statement.symbols + statement.topics:
            if value not in variables:
                variables.append(value)
    return variables


def _evidence_dict(statement: Statement) -> dict[str, Any]:
    return {
        "statement_id": statement.statement_id,
        "role_id": statement.role_id,
        "source_type": statement.source_type,
        "source_url": statement.source_url,
        "published_at": statement.published_at,
        "captured_at": statement.captured_at,
        "title": statement.title,
        "excerpt": statement.body[:240],
        "stance": statement.stance,
        "source_platform": statement.source_platform,
        "source_user_id": statement.source_user_id,
        "source_author": statement.source_author,
    }


def _tension_evidence(evidence: list[Statement], stance: str) -> list[dict[str, Any]]:
    if stance not in {"bullish", "bearish"}:
        return []
    opposite = "bearish" if stance == "bullish" else "bullish"
    return [_evidence_dict(statement) for statement in evidence if statement.stance == opposite]


def _profile_status(kb: KnowledgeBase, role_id: str) -> str:
    role_dir = kb.roles_dir / role_id
    if (role_dir / "profile.md").exists():
        return "reviewed"
    if (role_dir / "profile.generated.md").exists():
        return "generated_unreviewed"
    return "missing"


def _consensus(role_analyses: list[dict[str, Any]]) -> list[str]:
    non_unclear = [analysis["stance"] for analysis in role_analyses if analysis["stance"] != "unclear"]
    if not non_unclear:
        return []
    stance, count = Counter(non_unclear).most_common(1)[0]
    return [f"{count} role(s) share a {stance} stance."] if count > 0 else []


def _disagreements(role_analyses: list[dict[str, Any]]) -> list[str]:
    stances = {analysis["stance"] for analysis in role_analyses if analysis["stance"] != "unclear"}
    if len(stances) <= 1:
        return []
    return ["Roles disagree on stance, likely due to different time horizons or evidence emphasis."]


def _synthesis(role_analyses: list[dict[str, Any]]) -> str:
    if not role_analyses:
        return "No roles were analyzed."
    clear = [analysis for analysis in role_analyses if analysis["stance"] != "unclear"]
    if not clear:
        return "No role had enough relevant evidence for a directional event analysis."
    return " ".join(analysis["conclusion"] for analysis in clear)


def _uncertainty(role_analyses: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for analysis in role_analyses:
        values.extend(analysis.get("uncertainty", []))
    return values
