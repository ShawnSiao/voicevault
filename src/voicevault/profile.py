from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path

from .index import VoiceVaultIndex
from .kb import KnowledgeBase
from .markdown import read_markdown
from .models import Statement


def generate_profile(kb: KnowledgeBase, role_id: str) -> Path:
    role_dir = kb.roles_dir / role_id
    role_dir.mkdir(parents=True, exist_ok=True)
    statements = VoiceVaultIndex(kb).statements_for_role(role_id)
    markdown = build_generated_profile(role_id, statements, metadata=_existing_profile_metadata(role_dir))
    output_path = role_dir / "profile.generated.md"
    output_path.write_text(markdown, encoding="utf-8", newline="\n")
    return output_path


def promote_generated_profile(
    kb: KnowledgeBase,
    role_id: str,
    *,
    overwrite: bool = False,
    reviewer: str = "manual",
    review_note: str = "",
) -> Path:
    role_dir = kb.roles_dir / role_id
    generated_path = role_dir / "profile.generated.md"
    profile_path = role_dir / "profile.md"
    if not generated_path.is_file():
        raise FileNotFoundError(f"Generated profile not found: {generated_path}")
    if profile_path.exists() and not overwrite:
        raise FileExistsError(f"Reviewed profile already exists: {profile_path}")
    text = generated_path.read_text(encoding="utf-8")
    promoted = text.replace("profile_status: generated_unreviewed", "profile_status: reviewed", 1)
    review_lines = [
        f"reviewed_at: {date.today().isoformat()}",
        f"reviewed_by: {reviewer}",
    ]
    if review_note:
        review_lines.append(f"review_note: {review_note}")
    promoted = promoted.replace("---\n\n# Role Profile", "\n".join(review_lines) + "\n---\n\n# Role Profile", 1)
    profile_path.write_text(promoted, encoding="utf-8", newline="\n")
    return profile_path


def build_generated_profile(role_id: str, statements: list[Statement], metadata: dict[str, object] | None = None) -> str:
    topics = Counter(topic for statement in statements for topic in statement.topics)
    symbols = Counter(symbol for statement in statements for symbol in statement.symbols)
    stances = Counter(statement.stance for statement in statements if statement.stance)
    horizons = Counter(statement.time_horizon for statement in statements if statement.time_horizon)
    representative = statements[:5]
    metadata = metadata or {}
    display_name = str(metadata.get("display_name") or "").strip()
    source_platform = str(metadata.get("source_platform") or "").strip()
    source_url = str(metadata.get("source_url") or "").strip()

    lines = [
        "---",
        f"role_id: {role_id}",
    ]
    if display_name:
        lines.append(f"display_name: {display_name}")
    lines.extend(
        [
            "profile_status: generated_unreviewed",
            f"updated_at: {date.today().isoformat()}",
            "source_scope: public_statements_only",
        ]
    )
    if source_platform:
        lines.append(f"source_platform: {source_platform}")
    if source_url:
        lines.append(f"source_url: {source_url}")
    lines.extend(
        [
            "---",
            "",
            "# Role Profile",
            "",
            "## Focus Areas",
            "",
            _bullets([name for name, _ in topics.most_common(8)] or ["No recurring topics found."]),
            "",
            "## Decision Frameworks",
            "",
            _bullets(_frameworks(symbols, topics)),
            "",
            "## Investment Style",
            "",
            _bullets(_investment_style(stances, horizons)),
            "",
            "## Risk Preferences",
            "",
            _bullets(["Review source evidence before using this generated profile in reports."]),
            "",
            "## Common Stances",
            "",
            _bullets([f"{name}: {count}" for name, count in stances.most_common()] or ["No stance data found."]),
            "",
            "## Representative Views",
            "",
            _bullets([f"{statement.title}: {statement.body[:180]}" for statement in representative] or ["No statements found."]),
            "",
            "## Easy Misreadings",
            "",
            _bullets(["Generated summaries can flatten conditional views. Confirm time horizon and evidence before using."]),
            "",
            "## Evidence Index",
            "",
            _bullets([statement.statement_id for statement in representative] or ["No evidence indexed."]),
            "",
        ]
    )
    return "\n".join(lines)


def _existing_profile_metadata(role_dir: Path) -> dict[str, object]:
    for path in (role_dir / "profile.md", role_dir / "profile.generated.md"):
        if not path.is_file():
            continue
        metadata, _ = read_markdown(path)
        return metadata
    return {}


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _frameworks(symbols: Counter[str], topics: Counter[str]) -> list[str]:
    result: list[str] = []
    if symbols:
        result.append("Frequently references symbols: " + ", ".join(name for name, _ in symbols.most_common(6)) + ".")
    if topics:
        result.append("Often frames events through topics: " + ", ".join(name for name, _ in topics.most_common(6)) + ".")
    return result or ["No repeatable framework inferred from current statements."]


def _investment_style(stances: Counter[str], horizons: Counter[str]) -> list[str]:
    result: list[str] = []
    if stances:
        result.append("Observed stance distribution: " + ", ".join(f"{name}={count}" for name, count in stances.most_common()) + ".")
    if horizons:
        result.append("Observed time horizons: " + ", ".join(f"{name}={count}" for name, count in horizons.most_common()) + ".")
    return result or ["No investment style inferred from current statements."]
