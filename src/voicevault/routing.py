from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .kb import KnowledgeBase
from .roles import list_role_summaries
from .search import search_statements


ROLE_ROUTING_SCHEMA_VERSION = 1
MAX_ROUTE_LIMIT = 20
ROUTE_EVIDENCE_LIMIT = 3


def suggest_roles(
    kb: KnowledgeBase,
    query: str,
    *,
    symbol: str = "",
    topic: str = "",
    limit: int = 5,
) -> dict[str, Any]:
    route_limit = _safe_limit(limit)
    routes: list[dict[str, Any]] = []
    for role in list_role_summaries(kb):
        role_id = str(role.get("role_id") or "")
        if not role_id:
            continue
        search = search_statements(
            kb,
            query,
            role_id=role_id,
            symbol=symbol,
            topic=topic,
            limit=ROUTE_EVIDENCE_LIMIT,
        )
        if int(search.get("total_matches") or 0) <= 0:
            continue
        routes.append(_route_item(role, search, symbol=symbol, topic=topic))

    routes.sort(key=lambda item: (-item["score"], -item["evidence_count"], item["role_id"]))
    limited_routes = routes[:route_limit]
    suggested_role_id = str(limited_routes[0]["role_id"]) if limited_routes else ""
    return {
        "schema_version": ROLE_ROUTING_SCHEMA_VERSION,
        "query": query,
        "filters": {"symbol": symbol, "topic": topic, "limit": route_limit},
        "generated_at": _now_utc(),
        "suggested_role_id": suggested_role_id,
        "confidence": str(limited_routes[0]["confidence"]) if limited_routes else "none",
        "route_count": len(limited_routes),
        "routes": limited_routes,
        "no_match_reason": "" if limited_routes else "No indexed role evidence matched the question and filters.",
    }


def _route_item(role: dict[str, Any], search: dict[str, Any], *, symbol: str, topic: str) -> dict[str, Any]:
    results = search.get("results") if isinstance(search.get("results"), list) else []
    evidence_count = len(results)
    total_matches = int(search.get("total_matches") or 0)
    search_score = sum(int(item.get("score") or 0) for item in results if isinstance(item, dict))
    profile_status = str(role.get("profile_status") or "")
    score = search_score + min(total_matches, 20) * 3 + (2 if profile_status == "reviewed" else 0)
    return {
        "role_id": str(role.get("role_id") or ""),
        "display_name": str(role.get("display_name") or role.get("role_id") or ""),
        "profile_status": profile_status,
        "statement_count": int(role.get("statement_count") or 0),
        "score": score,
        "confidence": _confidence(evidence_count, score),
        "evidence_count": evidence_count,
        "total_matches": total_matches,
        "reason": _reason(evidence_count, total_matches, symbol=symbol, topic=topic),
        "evidence": [_evidence_item(item) for item in results[:ROUTE_EVIDENCE_LIMIT] if isinstance(item, dict)],
    }


def _evidence_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "statement_id": str(item.get("statement_id") or ""),
        "title": str(item.get("title") or ""),
        "score": int(item.get("score") or 0),
        "source_url": str(item.get("source_url") or ""),
        "published_at": str(item.get("published_at") or ""),
        "excerpt": str(item.get("excerpt") or ""),
    }


def _reason(evidence_count: int, total_matches: int, *, symbol: str, topic: str) -> str:
    filters = []
    if symbol:
        filters.append(f"symbol {symbol}")
    if topic:
        filters.append(f"topic {topic}")
    suffix = f" with {' and '.join(filters)}" if filters else ""
    return f"{evidence_count} top evidence item(s), {total_matches} total indexed match(es){suffix}."


def _confidence(evidence_count: int, score: int) -> str:
    if evidence_count >= 3 and score >= 25:
        return "high"
    if evidence_count >= 2 or score >= 12:
        return "medium"
    return "low"


def _safe_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 5
    return max(1, min(parsed, MAX_ROUTE_LIMIT))


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
