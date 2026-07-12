from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .kb import KnowledgeBase
from .markdown import first_heading, read_markdown
from .search import search_statements


def create_evidence_pack(
    kb: KnowledgeBase,
    *,
    title: str,
    query: str,
    role_id: str = "",
    symbol: str = "",
    topic: str = "",
    limit: int = 20,
) -> Path:
    kb.reports_dir.mkdir(parents=True, exist_ok=True)
    result = search_statements(kb, query, role_id=role_id, symbol=symbol, topic=topic, limit=limit)
    path = kb.reports_dir / f"{_slug(title)}.md"
    path.write_text(_markdown(title, query, role_id, symbol, topic, result), encoding="utf-8", newline="\n")
    return path


def list_reports(kb: KnowledgeBase) -> list[dict]:
    if not kb.reports_dir.exists():
        return []

    reports = []
    for path in sorted(kb.reports_dir.glob("*.md")):
        metadata, body = read_markdown(path)
        tags = _as_list(metadata.get("tags"))
        reports.append(
            {
                "title": str(metadata.get("title") or first_heading(body, path.stem)),
                "path": str(path),
                "query": str(metadata.get("query") or ""),
                "generated_at": str(metadata.get("generated_at") or ""),
                "role_id": str(metadata.get("role_id") or ""),
                "symbols": _as_list(metadata.get("symbols")),
                "topics": _as_list(metadata.get("topics")),
                "tags": tags,
                "kind": "evidence_pack" if "voicevault/evidence-pack" in tags else "markdown_report",
                "matches": _extract_match_count(body),
            }
        )

    return sorted(reports, key=lambda item: (item["generated_at"], item["title"]), reverse=True)


def _markdown(title: str, query: str, role_id: str, symbol: str, topic: str, result: dict) -> str:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    lines = [
        "---",
        f"title: {title}",
        f"query: {query}",
        f"generated_at: {generated_at}",
        f"role_id: {role_id}",
        "symbols:",
        *([f"  - {symbol}"] if symbol else []),
        "topics:",
        *([f"  - {topic}"] if topic else []),
        "tags:",
        "  - voicevault/evidence-pack",
        "---",
        "",
        f"# {title}",
        "",
        "## Query",
        "",
        f"- Query: {query}",
        f"- Role: {role_id or 'all'}",
        f"- Symbol: {symbol or 'all'}",
        f"- Topic: {topic or 'all'}",
        f"- Matches: {result['total_matches']}",
        "",
        "## Evidence",
        "",
    ]
    if not result["results"]:
        lines.append("- No matching evidence found.")
    for index, item in enumerate(result["results"], start=1):
        lines.extend(
            [
                f"### [{index}] {item['title']}",
                "",
                f"- Role: {item['role_id']}",
                f"- Published: {item['published_at']}",
                f"- Platform: {item['source_platform'] or 'unknown'}",
                f"- Source: {item['source_url'] or 'unknown'}",
                f"- Stance: {item['stance']}",
                f"- Score: {item['score']}",
                "",
                item["excerpt"],
                "",
            ]
        )
    lines.extend(
        [
            "## Uncertainty",
            "",
            "- This evidence pack is a deterministic keyword search result, not an AI conclusion.",
            "- Review original sources before using evidence in a report.",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "-", value.lower()).strip("-")
    return slug or "evidence-pack"


def _as_list(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [str(value)]


def _extract_match_count(body: str) -> int:
    match = re.search(r"^- Matches:\s*(\d+)\s*$", body, flags=re.MULTILINE)
    return int(match.group(1)) if match else 0
