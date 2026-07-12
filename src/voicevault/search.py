from __future__ import annotations

import re
from typing import Any

from .index import VoiceVaultIndex
from .kb import KnowledgeBase
from .models import Statement


_TERM_ALIASES = {
    "nvidia": ["nvda"],
    "英伟达": ["nvda"],
    "apple": ["aapl"],
    "苹果": ["aapl"],
    "microsoft": ["msft"],
    "微软": ["msft"],
    "tesla": ["tsla"],
    "特斯拉": ["tsla"],
    "amazon": ["amzn"],
    "亚马逊": ["amzn"],
    "alphabet": ["goog", "googl"],
    "google": ["goog", "googl"],
    "谷歌": ["goog", "googl"],
    "meta": ["meta", "fb"],
    "facebook": ["meta", "fb"],
    "intel": ["intc"],
    "英特尔": ["intc"],
    "oracle": ["orcl"],
    "甲骨文": ["orcl"],
    "tsmc": ["tsm"],
    "台积电": ["tsm"],
    "利润率": ["margin", "margins"],
    "毛利率": ["gross margin", "gross margins", "margin", "margins"],
    "估值": ["valuation"],
    "现金流": ["cash-flow", "cashflow", "cash flow"],
    "市值": ["market-cap", "market cap"],
}
_EXCERPT_NOISE_PATTERNS = (
    r"https?://\S+",
    r"\S+\.(?:jpg|jpeg|png|gif|webp)\S*",
    r"\S*(?:xqimg|imedao|emoji|assets)\S*",
)


def search_statements(
    kb: KnowledgeBase,
    query: str,
    *,
    role_id: str = "",
    symbol: str = "",
    topic: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    raw_terms = _terms(query)
    terms = _expand_terms(raw_terms)
    statements = VoiceVaultIndex(kb).all_statements()
    results: list[dict[str, Any]] = []
    for statement in statements:
        if role_id and statement.role_id != role_id:
            continue
        if symbol and symbol.upper() not in {value.upper() for value in statement.symbols}:
            continue
        if topic and topic.lower() not in {value.lower() for value in statement.topics}:
            continue
        score = _score(statement, terms)
        if score <= 0:
            continue
        if not _is_relevant(statement, raw_terms):
            continue
        results.append(_result(statement, score, terms))
    results.sort(key=lambda item: (-item["score"], item["published_at"], item["statement_id"]))
    limited = results[: max(limit, 0)]
    return {
        "query": query,
        "terms": raw_terms,
        "expanded_terms": terms,
        "count": len(limited),
        "total_matches": len(results),
        "results": limited,
    }


def _score(statement: Statement, terms: list[str]) -> int:
    if not terms:
        return 0
    title = statement.title.lower()
    body = statement.body.lower()
    symbols = {value.lower() for value in statement.symbols}
    topics = {value.lower() for value in statement.topics}
    score = 0
    for term in terms:
        if term in symbols:
            score += 8
        if term in topics:
            score += 6
        if term in title:
            score += 4
        if term in body:
            score += 2
    return score


def _is_relevant(statement: Statement, raw_terms: list[str]) -> bool:
    if len(raw_terms) <= 1:
        return True
    title = statement.title.lower()
    body = statement.body.lower()
    symbols = {value.lower() for value in statement.symbols}
    topics = {value.lower() for value in statement.topics}
    matched_terms = 0
    text_hit = False
    for term in raw_terms:
        aliases = _TERM_ALIASES.get(term, [])
        symbol_hit = term in symbols or any(alias in symbols for alias in aliases)
        topic_hit = term in topics or any(alias in topics for alias in aliases)
        text_terms = [term, *aliases]
        term_text_hit = any(value in title or value in body for value in text_terms)
        if term_text_hit or symbol_hit or topic_hit:
            matched_terms += 1
        text_hit = text_hit or term_text_hit
    return matched_terms >= 2 or text_hit


def _result(statement: Statement, score: int, terms: list[str]) -> dict[str, Any]:
    return {
        "statement_id": statement.statement_id,
        "role_id": statement.role_id,
        "score": score,
        "source_type": statement.source_type,
        "source_platform": statement.source_platform,
        "source_user_id": statement.source_user_id,
        "source_author": statement.source_author,
        "source_url": statement.source_url,
        "published_at": statement.published_at,
        "captured_at": statement.captured_at,
        "title": statement.title,
        "excerpt": _excerpt(statement.body, terms),
        "symbols": statement.symbols,
        "topics": statement.topics,
        "stance": statement.stance,
        "time_horizon": statement.time_horizon,
        "confidence": statement.confidence,
    }


def _excerpt(body: str, terms: list[str]) -> str:
    normalized = re.sub(r"\s+", " ", _clean_excerpt_text(body)).strip()
    if len(normalized) <= 240:
        return normalized
    lowered = normalized.lower()
    starts = [lowered.find(term) for term in terms if term and lowered.find(term) >= 0]
    start = min(starts) if starts else 0
    start = max(start - 60, 0)
    excerpt = normalized[start : start + 240].strip()
    return ("..." if start > 0 else "") + excerpt


def _clean_excerpt_text(body: str) -> str:
    cleaned = str(body)
    for pattern in _EXCERPT_NOISE_PATTERNS:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    return cleaned.replace("网页链接", " ").replace("图片:", " ").replace("图片：", " ")


def _terms(query: str) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    normalized_query = query.lower()

    def add(term: str) -> None:
        if term and term not in seen:
            seen.add(term)
            terms.append(term)

    for raw in re.findall(r"[A-Za-z0-9_\-\u4e00-\u9fff]+", query.lower()):
        add(raw)
    for phrase in sorted(_TERM_ALIASES, key=len, reverse=True):
        if phrase in normalized_query:
            add(phrase)
    return terms


def _expand_terms(terms: list[str]) -> list[str]:
    seen: set[str] = set()
    expanded: list[str] = []
    for term in terms:
        if term not in seen:
            seen.add(term)
            expanded.append(term)
        for alias in _TERM_ALIASES.get(term, []):
            if alias not in seen:
                seen.add(alias)
                expanded.append(alias)
    return expanded
