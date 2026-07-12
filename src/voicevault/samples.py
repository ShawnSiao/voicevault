from __future__ import annotations

import shutil
from typing import Any

from .importers import load_statements_from_kb
from .index import VoiceVaultIndex
from .kb import KnowledgeBase

SAMPLE_ROLE_IDS = {"sample-investor"}
SAMPLE_EVENT_FILES = {"example-event.md"}
SAMPLE_EXPORT_DIRS = {"example-event", "example-nvda-margin"}


def preview_sample_removal(kb: KnowledgeBase) -> dict[str, Any]:
    return {
        "removed_roles": [
            role_id for role_id in sorted(SAMPLE_ROLE_IDS) if (kb.roles_dir / role_id).exists()
        ],
        "removed_events": [
            event_name for event_name in sorted(SAMPLE_EVENT_FILES) if (kb.events_dir / event_name).exists()
        ],
        "removed_exports": [
            export_name for export_name in sorted(SAMPLE_EXPORT_DIRS) if (kb.exports_dir / export_name).exists()
        ],
        "statements_indexed": None,
        "dry_run": True,
    }


def remove_sample_content(kb: KnowledgeBase) -> dict[str, Any]:
    preview = preview_sample_removal(kb)
    for role_id in sorted(SAMPLE_ROLE_IDS):
        role_dir = kb.roles_dir / role_id
        if role_dir.exists():
            shutil.rmtree(role_dir)
    for event_name in sorted(SAMPLE_EVENT_FILES):
        event_path = kb.events_dir / event_name
        if event_path.exists():
            event_path.unlink()
    for export_name in sorted(SAMPLE_EXPORT_DIRS):
        export_dir = kb.exports_dir / export_name
        if export_dir.exists():
            shutil.rmtree(export_dir)
    indexed = VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
    return {
        "removed_roles": preview["removed_roles"],
        "removed_events": preview["removed_events"],
        "removed_exports": preview["removed_exports"],
        "statements_indexed": indexed,
        "dry_run": False,
    }
