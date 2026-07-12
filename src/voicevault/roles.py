from __future__ import annotations

import re
from datetime import date
from typing import Any

from .index import VoiceVaultIndex
from .kb import KnowledgeBase
from .markdown import read_markdown


ROLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
ROLE_COVERAGE_MIN_REVIEWED_ROLES = 2
ROLE_COVERAGE_MIN_STATEMENTS_PER_ROLE = 1


def create_role(
    kb: KnowledgeBase,
    *,
    role_id: str,
    display_name: str = "",
    platform: str = "",
    source_url: str = "",
    tags: list[str] | None = None,
    notes: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    normalized_role_id = role_id.strip()
    if not ROLE_ID_PATTERN.match(normalized_role_id):
        raise ValueError("Role ID must start with a letter or number and contain only letters, numbers, dots, underscores, or hyphens.")
    role_dir = kb.roles_dir / normalized_role_id
    generated_path = role_dir / "profile.generated.md"
    reviewed_path = role_dir / "profile.md"
    if not overwrite and (generated_path.exists() or reviewed_path.exists()):
        raise FileExistsError(f"Role profile already exists: {role_dir}")
    role_dir.mkdir(parents=True, exist_ok=True)
    statements_dir = role_dir / "statements"
    statements_dir.mkdir(exist_ok=True)
    if platform:
        (statements_dir / platform).mkdir(exist_ok=True)
    generated_path.write_text(
        _role_profile_draft(
            role_id=normalized_role_id,
            display_name=display_name.strip() or normalized_role_id,
            platform=platform.strip(),
            source_url=source_url.strip(),
            tags=tags or [],
            notes=notes.strip(),
        ),
        encoding="utf-8",
        newline="\n",
    )
    return {
        "role_id": normalized_role_id,
        "display_name": display_name.strip() or normalized_role_id,
        "profile_status": "generated_unreviewed",
        "role_dir": str(role_dir),
        "profile_path": str(reviewed_path),
        "generated_profile_path": str(generated_path),
        "statements_dir": str(statements_dir),
    }


def list_role_summaries(kb: KnowledgeBase) -> list[dict[str, Any]]:
    index = VoiceVaultIndex(kb)
    role_ids = _role_ids(kb, index)
    summaries: list[dict[str, Any]] = []
    for role_id in role_ids:
        role_dir = kb.roles_dir / role_id
        statements = index.statements_for_role(role_id) if kb.index_path.exists() else []
        summaries.append(
            {
                "role_id": role_id,
                "display_name": _role_display_name(kb, role_id),
                "profile_status": profile_status(kb, role_id),
                "statement_count": len(statements),
                "profile_path": str(role_dir / "profile.md"),
                "generated_profile_path": str(role_dir / "profile.generated.md"),
            }
        )
    return summaries


def evaluate_role_coverage(
    kb: KnowledgeBase,
    *,
    min_reviewed_roles: int = ROLE_COVERAGE_MIN_REVIEWED_ROLES,
    min_statements_per_role: int = ROLE_COVERAGE_MIN_STATEMENTS_PER_ROLE,
) -> dict[str, Any]:
    roles = list_role_summaries(kb)
    rows: list[dict[str, Any]] = []
    ready_role_ids: list[str] = []
    gaps: list[dict[str, Any]] = []
    for role in roles:
        profile = str(role["profile_status"])
        statement_count = int(role["statement_count"])
        ready = profile == "reviewed" and statement_count >= min_statements_per_role
        if profile != "reviewed":
            status = "unreviewed"
        elif statement_count < min_statements_per_role:
            status = "insufficient_statements"
        else:
            status = "ready"
        row = {
            "role_id": role["role_id"],
            "display_name": role["display_name"],
            "profile_status": profile,
            "statement_count": statement_count,
            "coverage_status": status,
            "ready": ready,
        }
        rows.append(row)
        if ready:
            ready_role_ids.append(str(role["role_id"]))
        else:
            gaps.append(
                {
                    "role_id": role["role_id"],
                    "gap": status,
                    "profile_status": profile,
                    "statement_count": statement_count,
                }
            )
    reviewed_roles = [role for role in rows if role["profile_status"] == "reviewed"]
    roles_with_statements = [role for role in rows if role["statement_count"] >= min_statements_per_role]
    missing_ready_roles = max(0, min_reviewed_roles - len(ready_role_ids))
    if missing_ready_roles:
        gaps.insert(
            0,
            {
                "gap": f"needs_{missing_ready_roles}_more_ready_role",
                "required": min_reviewed_roles,
                "actual": len(ready_role_ids),
            },
        )
    ok = missing_ready_roles == 0
    return {
        "schema_version": 1,
        "ok": ok,
        "min_reviewed_roles": min_reviewed_roles,
        "min_statements_per_role": min_statements_per_role,
        "total_roles": len(rows),
        "reviewed_roles": len(reviewed_roles),
        "roles_with_statements": len(roles_with_statements),
        "reviewed_roles_with_statements": len(ready_role_ids),
        "ready_role_ids": ready_role_ids,
        "roles": rows,
        "gaps": gaps,
        "remediation": _role_coverage_remediation(kb),
    }


def profile_status(kb: KnowledgeBase, role_id: str) -> str:
    role_dir = kb.roles_dir / role_id
    if (role_dir / "profile.md").is_file():
        return "reviewed"
    if (role_dir / "profile.generated.md").is_file():
        return "generated_unreviewed"
    return "missing"


def _role_coverage_remediation(kb: KnowledgeBase) -> list[str]:
    return [
        f"voicevault roles create --kb {kb.root} --role <role_id> --display-name <name> --platform <platform> --source-url <public_url> --json",
        f"voicevault sources create --kb {kb.root} --source <source_id> --role <role_id> --platform <platform> --source-url <public_url> --json",
        f"voicevault sources import --kb {kb.root} --source <source_id> --input <public_export.csv> --json",
        f"voicevault sync --kb {kb.root}",
        f"voicevault profile promote --kb {kb.root} --role <role_id> --reviewer <name> --note <note> --json",
    ]


def _role_ids(kb: KnowledgeBase, index: VoiceVaultIndex) -> list[str]:
    from_index = index.list_roles() if kb.index_path.exists() else []
    from_dirs = [path.name for path in kb.roles_dir.iterdir() if path.is_dir()] if kb.roles_dir.exists() else []
    return sorted(set(from_index) | set(from_dirs))


def _role_display_name(kb: KnowledgeBase, role_id: str) -> str:
    role_dir = kb.roles_dir / role_id
    for path in (role_dir / "profile.md", role_dir / "profile.generated.md"):
        if not path.is_file():
            continue
        metadata, _ = read_markdown(path)
        display_name = str(metadata.get("display_name") or "").strip()
        if display_name:
            return display_name
    source_display_name = _source_display_name(kb, role_id)
    if source_display_name:
        return source_display_name
    return role_id


def _source_display_name(kb: KnowledgeBase, role_id: str) -> str:
    from .sources import list_sources

    for source in list_sources(kb):
        if str(source.get("role_id") or "").strip() != role_id:
            continue
        display_name = str(source.get("display_name") or "").strip()
        source_id = str(source.get("source_id") or "").strip()
        if display_name and display_name != source_id:
            return display_name
    return ""


def _role_profile_draft(
    *,
    role_id: str,
    display_name: str,
    platform: str,
    source_url: str,
    tags: list[str],
    notes: str,
) -> str:
    lines = [
        "---",
        f"role_id: {role_id}",
        f"display_name: {display_name}",
        "profile_status: generated_unreviewed",
        f"updated_at: {date.today().isoformat()}",
        "source_scope: public_statements_only",
    ]
    if platform:
        lines.append(f"source_platform: {platform}")
    if source_url:
        lines.append(f"source_url: {source_url}")
    if tags:
        lines.append("tags:")
        lines.extend(f"  - {tag}" for tag in tags)
    lines.extend(
        [
            "---",
            "",
            "# Role Profile",
            "",
            "## Onboarding Notes",
            "",
            notes or "Add public-source context before promoting this profile.",
            "",
            "## Focus Areas",
            "",
            "- Review after public statements are captured.",
            "",
            "## Evidence Index",
            "",
            "- No statements captured yet.",
            "",
        ]
    )
    return "\n".join(lines)
