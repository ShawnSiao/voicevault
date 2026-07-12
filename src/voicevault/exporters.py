from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import AnalysisOutput


def write_analysis_outputs(out_dir: Path, result: dict[str, Any]) -> AnalysisOutput:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "analysis.json"
    markdown_path = out_dir / "analysis.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    markdown_path.write_text(_analysis_markdown(result), encoding="utf-8", newline="\n")
    return AnalysisOutput(json_path=json_path, markdown_path=markdown_path)


def _analysis_markdown(result: dict[str, Any]) -> str:
    event = result["event"]
    lines = [
        "# VoiceVault Role Analysis",
        "",
        "## Event",
        "",
        f"- ID: {event.get('event_id', '')}",
        f"- Title: {event.get('title', '')}",
        f"- Date: {event.get('date', '')}",
        "",
        "## Executive Synthesis",
        "",
        result.get("synthesis_markdown") or "No synthesis available.",
        "",
        "## Consensus",
        "",
        *_bullets(result.get("consensus", [])),
        "",
        "## Disagreements",
        "",
        *_bullets(result.get("disagreements", [])),
        "",
        "## Minority Views",
        "",
        *_bullets(result.get("minority_views", [])),
        "",
        "## Role Analyses",
        "",
    ]
    for analysis in result.get("role_analyses", []):
        lines.extend(
            [
                f"### {analysis['display_name']}",
                "",
                f"- Stance: {analysis['stance']}",
                f"- Confidence: {analysis['confidence']}",
                f"- Time horizon: {analysis['time_horizon']}",
                f"- Conclusion: {analysis['conclusion']}",
                f"- Profile status: {analysis['profile_status']}",
                "- Uncertainty:",
                *_bullets(analysis.get("uncertainty", [])),
                "",
            ]
        )
    lines.extend(["## Evidence", ""])
    for evidence in result.get("evidence", []):
        lines.extend(
            [
                f"### {evidence['statement_id']}",
                "",
                f"- Role: {evidence['role_id']}",
                f"- Published: {evidence['published_at']}",
                f"- Source: {evidence['source_url']}",
                f"- Excerpt: {evidence['excerpt']}",
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _bullets(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items] if items else ["- None"]
