from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .kb import KnowledgeBase
from .release import check_release_readiness


QUICKSTART_SCHEMA_VERSION = 1


def build_quickstart_guide(kb: KnowledgeBase, repo_root: Path | None = None) -> dict[str, Any]:
    root = repo_root.resolve() if repo_root else Path.cwd().resolve()
    readiness = check_release_readiness(kb)
    phases = _quickstart_phases(kb, root)
    next_actions = _next_actions(readiness, phases)
    return {
        "schema_version": QUICKSTART_SCHEMA_VERSION,
        "product": {
            "chinese_name": "声迹",
            "english_name": "VoiceVault",
            "repository": "public-voice-archive",
            "version": __version__,
        },
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "knowledge_base": str(kb.root),
        "repo_root": str(root),
        "release_ready": bool(readiness["ok"]),
        "readiness_summary": readiness["summary"],
        "phases": phases,
        "next_actions": next_actions,
        "data_boundary": [
            "Do not commit real knowledge-base content to the repository.",
            "Keep generated secrets, credentials, cookies, private captures, voice samples, and platform caches outside version control.",
            "Quickstart guide exports contain workflow metadata only; they do not copy source captures or private content.",
        ],
    }


def write_quickstart_guide(
    kb: KnowledgeBase,
    repo_root: Path | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    target_dir = out_dir or kb.exports_dir / "guide"
    target_dir.mkdir(parents=True, exist_ok=True)
    guide = build_quickstart_guide(kb, repo_root=repo_root)
    guide_json = target_dir / "quickstart.json"
    guide_markdown = target_dir / "quickstart.md"
    guide_json.write_text(json.dumps(guide, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    guide_markdown.write_text(_quickstart_markdown(guide), encoding="utf-8", newline="\n")
    return {
        "ok": True,
        "guide_json": str(guide_json),
        "guide_markdown": str(guide_markdown),
        "guide": guide,
    }


def _quickstart_phases(kb: KnowledgeBase, repo_root: Path) -> list[dict[str, Any]]:
    kb_path = str(kb.root)
    repo_path = str(repo_root)
    return [
        {
            "id": "setup",
            "title": "Repair and inspect the local knowledge base",
            "goal": "Confirm the KB structure is healthy before adding real public voices.",
            "commands": [
                f"voicevault doctor --kb {kb_path} --repair",
                f"voicevault sample remove --kb {kb_path} --dry-run --json",
                f"voicevault doctor --kb {kb_path} --json",
                f"voicevault roles list --kb {kb_path} --json",
            ],
        },
        {
            "id": "capture",
            "title": "Append and validate public capture input",
            "goal": "Move public statements into the capture inbox and sync them into reviewed local notes.",
            "commands": [
                f'voicevault capture append --kb {kb_path} --role <role_id> --platform <platform> --url "<public_url>" --text "<public statement>" --topics <topic> --json',
                f"voicevault capture validate --path {kb.inbox_captures_dir} --json",
                f"voicevault sync --kb {kb_path} --archive --json",
                f"voicevault capture status --kb {kb_path} --json",
            ],
        },
        {
            "id": "source_jobs",
            "title": "Configure and drain source jobs",
            "goal": "Turn recurring public sources into auditable jobs before release.",
            "commands": [
                f'voicevault sources create --kb {kb_path} --source <source_id> --role <role_id> --platform <platform> --source-url "<public_url>" --json',
                f"voicevault sources template --kb {kb_path} --source <source_id> --format csv --json",
                f"voicevault sources import --kb {kb_path} --source <source_id> --input <public_export.csv> --json",
                f"voicevault sources imports --kb {kb_path} --json",
                f"voicevault sources normalize --kb {kb_path} --source <source_id> --input <public_export.csv> --update-source --json",
                f"voicevault sources list --kb {kb_path} --json",
                f"voicevault sources validate --kb {kb_path} --json",
                f"voicevault sources enqueue --kb {kb_path} --json",
                f"voicevault sources drain --kb {kb_path} --dry-run --json",
                f"voicevault sources jobs --kb {kb_path} --json",
            ],
        },
        {
            "id": "research_outputs",
            "title": "Generate research outputs from evidence",
            "goal": "Produce reviewed profiles, event analysis, cited answers, and evidence packs from indexed statements.",
            "commands": [
                f"voicevault profile generate --role <role_id> --kb {kb_path}",
                f"voicevault profile promote --role <role_id> --kb {kb_path} --json",
                f"voicevault event list --kb {kb_path} --json",
                f"voicevault analyze --kb {kb_path} --event {kb_path}\\content\\events\\<event_id>.md --roles all --json",
                f"voicevault analyses list --kb {kb_path} --json",
                f"voicevault search --kb {kb_path} --query NVIDIA --json",
                f"voicevault answer --kb {kb_path} --query AI --json",
                f'voicevault collect --kb {kb_path} --title "<report title>" --query "<query>" --json',
                f"voicevault reports list --kb {kb_path} --json",
                f"voicevault ui --kb {kb_path} --root {repo_path} --json",
            ],
        },
        {
            "id": "release_handoff",
            "title": "Create final release handoff artifacts",
            "goal": "Package the KB handoff and CLI distribution into one release manifest.",
            "commands": [
                f"voicevault release prepare --kb {kb_path} --root {repo_path} --json",
                f"voicevault release package --root {repo_path} --json",
                f"voicevault release ship --root {repo_path} --kb {kb_path} --json",
            ],
        },
        {
            "id": "post_handoff_verify",
            "title": "Verify the final ship manifest",
            "goal": "Recheck the archived release artifacts and their data boundary before handoff.",
            "commands": [
                f"voicevault release verify --manifest {repo_path}\\dist\\voicevault-v{__version__}-ship-manifest.json --json",
            ],
        },
    ]


def _next_actions(readiness: dict[str, Any], phases: list[dict[str, Any]]) -> list[dict[str, str]]:
    failed = [check for check in readiness["checks"] if not check["ok"]]
    if not failed:
        return [
            {
                "phase": "release_handoff",
                "action": "Run release ship to regenerate the final handoff after any content or code change.",
                "command": phases[-2]["commands"][-1],
            },
            {
                "phase": "post_handoff_verify",
                "action": "Run release verify against the generated ship manifest before handing off artifacts.",
                "command": phases[-1]["commands"][0],
            },
        ]
    actions: list[dict[str, str]] = []
    for check in failed[:6]:
        actions.append(
            {
                "phase": _phase_for_check(check["id"]),
                "action": check["message"],
                "command": check.get("remediation", phases[0]["commands"][0]),
            }
        )
    return actions


def _phase_for_check(check_id: str) -> str:
    if check_id in {"required_dirs", "index", "roles", "profiles_reviewed", "sample_content"}:
        return "setup"
    if check_id in {"capture_status", "sync_status"}:
        return "capture"
    if check_id in {"sources", "source_adapters", "source_runs", "source_jobs"}:
        return "source_jobs"
    if check_id in {"analysis_exports", "answer_exports", "reports", "dashboard", "ui", "events"}:
        return "research_outputs"
    return "setup"


def _quickstart_markdown(guide: dict[str, Any]) -> str:
    product = guide["product"]
    lines = [
        "# VoiceVault Quickstart Guide",
        "",
        f"- 产品：{product['chinese_name']} / {product['english_name']}",
        f"- 版本：{product['version']}",
        f"- 知识库：{guide['knowledge_base']}",
        f"- 仓库：{guide['repo_root']}",
        f"- 发布状态：{'ready' if guide['release_ready'] else 'needs attention'}",
        "",
        "## Next Actions",
        "",
    ]
    for item in guide["next_actions"]:
        lines.append(f"- `{item['phase']}`：{item['action']}")
        lines.append(f"  - `{item['command']}`")
    for phase in guide["phases"]:
        lines.extend(["", f"## {phase['title']}", "", phase["goal"], ""])
        for command in phase["commands"]:
            lines.append(f"```powershell\n{command}\n```")
    lines.extend(["", "## Data Boundary", ""])
    for item in guide["data_boundary"]:
        lines.append(f"- {item}")
    return "\n".join(lines).strip() + "\n"
