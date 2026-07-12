from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .kb import KnowledgeBase
from .next_actions import build_research_next_actions
from .profile import generate_profile, promote_generated_profile
from .roles import create_role, evaluate_role_coverage
from .sources import create_source, run_source
from .sync import sync_once


def create_public_role_source(
    kb: KnowledgeBase,
    *,
    role_id: str,
    source_id: str,
    platform: str,
    display_name: str = "",
    source_url: str = "",
    symbols: list[str] | None = None,
    topics: list[str] | None = None,
    tags: list[str] | None = None,
    notes: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    role = create_role(
        kb,
        role_id=role_id,
        display_name=display_name,
        platform=platform,
        source_url=source_url,
        tags=tags,
        notes=notes,
        overwrite=overwrite,
    )
    source = create_source(
        kb,
        source_id=source_id,
        role_id=role["role_id"],
        platform=platform,
        source_url=source_url,
        display_name=display_name or source_id,
        adapter="manual",
        symbols=symbols,
        topics=topics,
        tags=tags,
        notes=notes,
        overwrite=overwrite,
    )
    return {
        "role": role,
        "source": source,
        "role_coverage": evaluate_role_coverage(kb),
        "next_actions": build_research_next_actions(kb),
    }


def ingest_public_statement(
    kb: KnowledgeBase,
    *,
    source_id: str,
    text: str,
    title: str = "",
    source_url: str = "",
    published_at: str = "",
    captured_at: str = "",
    symbols: list[str] | None = None,
    topics: list[str] | None = None,
    stance: str = "unclear",
    time_horizon: str = "unknown",
    confidence: str = "low",
    notes: str = "",
    sync: bool = True,
    archive: bool = True,
    generate: bool = True,
    promote: bool = False,
    overwrite_profile: bool = True,
    reviewer: str = "local-ui",
    review_note: str = "",
) -> dict[str, Any]:
    source_run = run_source(
        kb,
        source_id,
        text=text,
        title=title,
        source_url=source_url,
        published_at=published_at,
        captured_at=captured_at,
        symbols=symbols,
        topics=topics,
        stance=stance,
        time_horizon=time_horizon,
        confidence=confidence,
        notes=notes,
    )
    role_id = str(source_run["record"].get("role_id") or "")
    sync_payload: dict[str, Any] | None = None
    if sync:
        sync_payload = asdict(sync_once(kb, archive_processed=archive))

    generated_profile_path = ""
    profile_path = ""
    if generate or promote:
        generated_profile_path = str(generate_profile(kb, role_id))
    if promote:
        profile_path = str(
            promote_generated_profile(
                kb,
                role_id,
                overwrite=overwrite_profile,
                reviewer=reviewer,
                review_note=review_note,
            )
        )

    return {
        "source_run": source_run,
        "sync": sync_payload,
        "generated_profile_path": generated_profile_path,
        "profile_path": profile_path,
        "role_coverage": evaluate_role_coverage(kb),
        "next_actions": build_research_next_actions(kb, latest_record=source_run["record"]),
    }
