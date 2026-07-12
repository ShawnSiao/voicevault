from __future__ import annotations

from typing import Any

from .index import VoiceVaultIndex
from .kb import KnowledgeBase


def inspect_kb(kb: KnowledgeBase) -> dict[str, Any]:
    required_dirs = _required_dirs(kb)
    missing_dirs = [str(path) for path in required_dirs if not path.is_dir()]
    warnings: list[str] = []
    if missing_dirs:
        warnings.append("Knowledge base is missing required directories.")

    index_exists = kb.index_path.is_file()
    role_count = 0
    statement_count = 0
    if index_exists:
        index = VoiceVaultIndex(kb)
        role_count = len(index.list_roles())
        statement_count = index.count_statements()
        if statement_count == 0:
            warnings.append("Index contains no statements.")
    else:
        warnings.append("Index has not been built.")

    if kb.roles_dir.exists() and not any(path.is_dir() for path in kb.roles_dir.iterdir()):
        warnings.append("No role directories found.")
    if kb.events_dir.exists() and not any(path.suffix == ".md" for path in kb.events_dir.iterdir()):
        warnings.append("No event Markdown files found.")

    return {
        "ok": not warnings,
        "root": str(kb.root),
        "index_path": str(kb.index_path),
        "index_exists": index_exists,
        "role_count": role_count,
        "statement_count": statement_count,
        "missing_dirs": missing_dirs,
        "warnings": warnings,
    }


def repair_kb(kb: KnowledgeBase) -> dict[str, Any]:
    created_dirs: list[str] = []
    for path in _required_dirs(kb):
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            created_dirs.append(str(path))
    report = inspect_kb(kb)
    report["created_dirs"] = created_dirs
    return report


def _required_dirs(kb: KnowledgeBase) -> list[Any]:
    return [
        kb.content_dir,
        kb.roles_dir,
        kb.events_dir,
        kb.topics_dir,
        kb.reports_dir,
        kb.sources_dir,
        kb.inbox_dir,
        kb.inbox_captures_dir,
        kb.inbox_archive_dir,
        kb.exports_dir,
        kb.state_dir,
    ]
