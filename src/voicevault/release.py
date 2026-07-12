from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from . import __version__
from .analysis_exports import list_analysis_exports, summarize_analysis_exports
from .answer import is_deliverable_answer_export, list_answer_exports, summarize_answer_exports
from .answer_regression import audit_answer_regression_coverage
from .checksums import file_sha256, write_sha256_file
from .collections import list_reports
from .comparison import (
    is_adopted_comparison_export,
    is_deliverable_comparison_export,
    is_reviewed_comparison_export,
    list_comparison_exports,
    summarize_comparison_exports,
)
from .diagnostics import inspect_kb
from .events import list_events
from .kb import KnowledgeBase
from .roles import evaluate_role_coverage, list_role_summaries
from .role_agent import audit_role_agent_exports, audit_role_agent_readiness, list_role_agent_exports
from .role_skill import audit_role_skill_coverage, list_role_skills
from .samples import SAMPLE_EXPORT_DIRS
from .source_imports import read_source_import_status
from .source_jobs import read_source_job_status
from .sources import list_sources, read_source_status, validate_source_adapters
from .sync import read_capture_status, read_sync_status


RELEASE_SCHEMA_VERSION = 1


def check_release_readiness(kb: KnowledgeBase, *, require_live_role_agent: bool = False) -> dict[str, Any]:
    diagnostics = inspect_kb(kb)
    roles = list_role_summaries(kb)
    role_coverage = evaluate_role_coverage(kb)
    role_skills = list_role_skills(kb)
    role_skill_coverage = audit_role_skill_coverage(kb)
    role_agent_exports = list_role_agent_exports(kb)
    role_agent_audit = audit_role_agent_exports(kb)
    role_agent_readiness = audit_role_agent_readiness(kb, require_live=require_live_role_agent)
    events = list_events(kb)
    reports = list_reports(kb)
    answer_exports = list_answer_exports(kb)
    answer_export_summary = summarize_answer_exports(answer_exports)
    comparison_exports = list_comparison_exports(kb)
    comparison_export_summary = summarize_comparison_exports(comparison_exports)
    source_configs = list_sources(kb)
    source_adapter_validation = validate_source_adapters(kb)
    source_status = read_source_status(kb)
    source_import_status = read_source_import_status(kb)
    source_job_status = read_source_job_status(kb)
    evidence_answer_exports = [item for item in answer_exports if item["evidence_backed"]]
    deliverable_answer_exports = [item for item in answer_exports if is_deliverable_answer_export(item)]
    invalid_answer_exports = [item for item in answer_exports if not is_deliverable_answer_export(item)]
    deliverable_comparison_exports = [item for item in comparison_exports if is_deliverable_comparison_export(item)]
    reviewed_comparison_exports = [item for item in comparison_exports if is_reviewed_comparison_export(item)]
    adopted_comparison_exports = [item for item in comparison_exports if is_adopted_comparison_export(item)]
    draft_comparison_exports = [item for item in comparison_exports if item.get("review_status") == "draft"]
    invalid_open_comparison_exports = [
        item
        for item in comparison_exports
        if not is_deliverable_comparison_export(item) and item.get("review_status") != "rejected"
    ]
    sync_status = read_sync_status(kb)
    capture_status = read_capture_status(kb)
    analysis_exports = list_analysis_exports(kb)
    analysis_export_summary = summarize_analysis_exports(analysis_exports)
    ready_analysis_exports = [item for item in analysis_exports if item["status"] == "ready"]
    malformed_analysis_exports = [item for item in analysis_exports if item["status"] == "malformed"]
    answer_regression = audit_answer_regression_coverage(kb)
    dashboard_path = kb.exports_dir / "dashboard" / "index.html"
    ui_path = kb.exports_dir / "ui" / "index.html"
    ui_data_path = kb.exports_dir / "ui" / "data.json"
    sample_roles = [role["role_id"] for role in roles if role["role_id"] == "sample-investor"]
    sample_events = [event["event_id"] for event in events if event["event_id"] == "example-nvda-margin"]
    sample_exports = [name for name in sorted(SAMPLE_EXPORT_DIRS) if (kb.exports_dir / name).exists()]
    unreviewed_roles = [
        {"role_id": role["role_id"], "profile_status": role["profile_status"]}
        for role in roles
        if role["profile_status"] != "reviewed"
    ]

    checks: list[dict[str, Any]] = []
    _add_check(
        checks,
        "required_dirs",
        all(path.is_dir() for path in _required_dirs(kb)),
        "Required knowledge-base directories exist.",
    )
    _add_check(
        checks,
        "index",
        kb.index_path.is_file() and diagnostics["statement_count"] > 0,
        f"SQLite index has {diagnostics['statement_count']} statement(s).",
        {"index_path": diagnostics["index_path"]},
        f"Run: voicevault sync --kb {kb.root}  or  voicevault build --kb {kb.root}",
    )
    _add_check(
        checks,
        "roles",
        len(roles) > 0,
        f"{len(roles)} role(s) available.",
    )
    _add_check(
        checks,
        "profiles_reviewed",
        len(roles) > 0 and not unreviewed_roles,
        "All roles have reviewed profiles.",
        {"unreviewed_roles": unreviewed_roles},
        f"Run: voicevault profile generate --role <role_id> --kb {kb.root}, review the draft, then voicevault profile promote --role <role_id> --kb {kb.root}",
    )
    _add_check(
        checks,
        "role_coverage",
        bool(role_coverage["ok"]),
        (
            f"{role_coverage['reviewed_roles_with_statements']} reviewed role(s) with statements; "
            f"requires {role_coverage['min_reviewed_roles']}."
        ),
        role_coverage,
        (
            f"Run: voicevault roles coverage --kb {kb.root} --json, then add/promote public roles "
            "and sync statements until at least two reviewed roles have evidence."
        ),
    )
    _add_check(
        checks,
        "role_skills",
        bool(role_skill_coverage["ok"]),
        (
            f"{role_skill_coverage['summary']['ready']} ready Role Skill artifact(s); "
            f"{role_skill_coverage['summary']['missing']} ready role(s) missing Role Skill."
        ),
        role_skill_coverage,
        f"Run: voicevault role skills --kb {kb.root} --json, then voicevault role distill --kb {kb.root} --role <role_id> --json for each missing ready role.",
    )
    _add_check(
        checks,
        "role_agent_quality",
        bool(role_agent_exports) and bool(role_agent_audit["ok"]),
        (
            f"{role_agent_audit['summary']['deliverable']} deliverable Role Agent answer(s), "
            f"{role_agent_audit['summary']['prompt_only']} prompt-only, "
            f"{role_agent_audit['summary']['failed']} failed, "
            f"{role_agent_audit['summary']['invalid_completed']} invalid completed."
        ),
        role_agent_audit,
        f"Run: voicevault role ask --kb {kb.root} --role <role_id> --query <question> --dry-run --json, then voicevault role agents --kb {kb.root} --json.",
    )
    _add_check(
        checks,
        "role_agent_readiness",
        bool(role_agent_readiness["ok"]),
        (
            f"{role_agent_readiness['summary']['roles_live_ready']} live-ready role(s), "
            f"{role_agent_readiness['summary']['roles_prompt_ready']} prompt-ready role(s), "
            f"{role_agent_readiness['summary']['roles_missing_live']} missing live; "
            f"require_live={role_agent_readiness['require_live']}."
        ),
        role_agent_readiness,
        (
            "; ".join(role_agent_readiness["remediation"])
            or f"Run: voicevault role readiness --kb {kb.root} --require-live --json"
        ),
    )
    _add_check(
        checks,
        "events",
        any(event["event_id"] != "example-nvda-margin" for event in events),
        f"{len(events)} event file(s) available.",
        remediation=f"Run: voicevault event create --kb {kb.root} --event-id <id> --title <title> --date <YYYY-MM-DD>",
    )
    _add_check(
        checks,
        "sync_status",
        bool(sync_status.get("ok") and sync_status.get("last_result")),
        "Latest sync status is recorded and healthy.",
        {"status_path": sync_status.get("status_path")},
        f"Run: voicevault sync --kb {kb.root}",
    )
    _add_check(
        checks,
        "capture_status",
        bool(capture_status.get("ok") and capture_status.get("pending_count") == 0 and capture_status["summary"]["failed"] == 0),
        "Capture lifecycle status is healthy and inbox backlog is empty.",
        {
            "status_path": capture_status.get("status_path"),
            "pending_count": capture_status.get("pending_count"),
            "summary": capture_status.get("summary"),
        },
        f"Run: voicevault sync --kb {kb.root} --archive --json, then voicevault capture status --kb {kb.root} --json",
    )
    _add_check(
        checks,
        "sources",
        any(item.get("status") == "active" for item in source_configs),
        f"{len(source_configs)} capture source config(s) available.",
        {"paths": [item["config_path"] for item in source_configs[:6]]},
        f"Run: voicevault sources create --kb {kb.root} --source <source_id> --role <role_id> --platform <platform>",
    )
    _add_check(
        checks,
        "source_adapters",
        bool(source_adapter_validation["ok"]),
        (
            f"{source_adapter_validation['summary']['ready']} source adapter config(s) ready; "
            f"{source_adapter_validation['summary']['failed']} failed."
        ),
        {
            "summary": source_adapter_validation["summary"],
            "failed_sources": [
                item
                for item in source_adapter_validation["sources"]
                if item.get("status") == "failed"
            ][:6],
        },
        f"Run: voicevault sources validate --kb {kb.root} --json, then fix failed source adapter configs.",
    )
    _add_check(
        checks,
        "source_runs",
        bool(source_status["ok"]),
        (
            f"{source_status['summary']['total']} source run(s) recorded; "
            f"{source_status['summary']['active_without_runs']} active source(s) without runs; "
            f"{source_status['summary']['active_failed_latest']} active source(s) with failed latest runs."
        ),
        {
            "status_path": source_status["status_path"],
            "summary": source_status["summary"],
            "errors": source_status.get("errors", []),
        },
        f"Run: voicevault sources run --kb {kb.root} --source <source_id> --text <public_statement> --dry-run --json, then voicevault sources status --kb {kb.root} --json",
    )
    _add_check(
        checks,
        "source_jobs",
        bool(source_job_status["ok"] and source_job_status["summary"]["pending"] == 0),
        (
            f"{source_job_status['summary']['pending']} pending source job(s), "
            f"{source_job_status['summary']['failed']} failed source job(s)."
        ),
        {
            "status_path": source_job_status["status_path"],
            "summary": source_job_status["summary"],
            "errors": source_job_status.get("errors", []),
        },
        f"Run: voicevault sources jobs --kb {kb.root} --json. For pending jobs, run voicevault sources drain --kb {kb.root} --dry-run --json. For failed jobs, run voicevault sources retry --kb {kb.root} --job <job_id> --json.",
    )
    _add_check(
        checks,
        "analysis_exports",
        analysis_export_summary["ready"] > 0 and analysis_export_summary["malformed"] == 0,
        (
            f"{analysis_export_summary['ready']} ready analysis export(s), "
            f"{analysis_export_summary['malformed']} malformed."
        ),
        {
            "summary": analysis_export_summary,
            "paths": [item["analysis_json"] for item in analysis_exports[:6]],
            "malformed": [_analysis_export_problem(item) for item in malformed_analysis_exports[:6]],
        },
        f"Run: voicevault analyses list --kb {kb.root} --json, then rerun voicevault analyze --kb {kb.root} --event <event.md> --roles all for malformed or missing exports.",
    )
    _add_check(
        checks,
        "answer_exports",
        len(deliverable_answer_exports) > 0 and not invalid_answer_exports,
        (
            f"{len(deliverable_answer_exports)} deliverable zh-CN v1 evidence answer export(s) available; "
            f"{len(invalid_answer_exports)} invalid."
        ),
        {
            "summary": answer_export_summary,
            "paths": [item["answer_json"] for item in deliverable_answer_exports[:6]],
            "total_exports": len(answer_exports),
            "evidence_backed_exports": len(evidence_answer_exports),
            "deliverable_answer_exports": len(deliverable_answer_exports),
            "invalid_paths": [item["answer_json"] for item in invalid_answer_exports[:6]],
            "invalid_exports": [_answer_export_problem(item) for item in invalid_answer_exports[:6]],
        },
        f"Run: voicevault answers list --kb {kb.root} --status invalid --json, then regenerate or prune invalid answer exports.",
    )
    _add_check(
        checks,
        "answer_regression",
        bool(answer_regression["ok"]),
        (
            f"{answer_regression['summary']['passed']} / {answer_regression['summary']['total']} fixed answer regression "
            f"question(s) passed; requires {answer_regression['summary']['min_questions']} with provenance."
        ),
        {
            "summary": answer_regression["summary"],
            "checks": answer_regression["checks"],
            "failed_items": [_answer_regression_problem(item) for item in answer_regression["items"] if item.get("status") != "pass"][:6],
            "missing_provenance": [
                _answer_regression_problem(item)
                for item in answer_regression["items"]
                if not item.get("source_url") or not item.get("rationale") or not item.get("updated_by")
            ][:6],
        },
        (
            f"Run: voicevault evaluations answers --kb {kb.root} --json. "
            f"Use voicevault evaluations export --kb {kb.root} --out <answer-regression-suite.json> --json and "
            f"voicevault evaluations import --kb {kb.root} --input <answer-regression-suite.json> --yes --json to maintain the fixed suite."
        ),
    )
    _add_check(
        checks,
        "comparison_exports",
        (
            len(comparison_exports) == 0
            or (
                len(adopted_comparison_exports) > 0
                and not draft_comparison_exports
                and not invalid_open_comparison_exports
            )
        ),
        (
            f"{len(adopted_comparison_exports)} adopted comparison export(s), "
            f"{len(draft_comparison_exports)} draft, "
            f"{len(invalid_open_comparison_exports)} invalid open."
        ),
        {
            "summary": comparison_export_summary,
            "total_exports": len(comparison_exports),
            "deliverable_comparison_exports": len(deliverable_comparison_exports),
            "reviewed_comparison_exports": len(reviewed_comparison_exports),
            "adopted_comparison_exports": len(adopted_comparison_exports),
            "draft_comparison_exports": len(draft_comparison_exports),
            "invalid_open_comparison_exports": len(invalid_open_comparison_exports),
            "draft_paths": [item["comparison_json"] for item in draft_comparison_exports[:6]],
            "invalid_open_exports": [_comparison_export_problem(item) for item in invalid_open_comparison_exports[:6]],
        },
        f"Run: voicevault comparisons list --kb {kb.root} --review-status draft --json, then voicevault comparisons review --kb {kb.root} --query <query> --status adopted --reviewer <name> --notes <notes> --json.",
    )
    _add_check(
        checks,
        "reports",
        len(reports) > 0,
        f"{len(reports)} report(s) available.",
        remediation=f"Run: voicevault collect --kb {kb.root} --title <title> --query <query>",
    )
    _add_check(
        checks,
        "dashboard",
        dashboard_path.is_file(),
        f"Dashboard HTML exists at {dashboard_path}.",
        {"path": str(dashboard_path)},
        f"Run: voicevault dashboard --kb {kb.root}",
    )
    _add_check(
        checks,
        "ui",
        ui_path.is_file() and ui_data_path.is_file(),
        f"Local UI exists at {ui_path}.",
        {"path": str(ui_path), "data": str(ui_data_path)},
        f"Run: voicevault ui --kb {kb.root}",
    )
    _add_check(
        checks,
        "sample_content",
        not sample_roles and not sample_events and not sample_exports,
        "Initialization sample content has been removed.",
        {"roles": sample_roles, "events": sample_events, "exports": sample_exports},
        f"Run: voicevault sample remove --kb {kb.root} --dry-run --json, then voicevault sample remove --kb {kb.root}",
    )

    return {
        "schema_version": RELEASE_SCHEMA_VERSION,
        "ok": all(check["ok"] for check in checks),
        "root": str(kb.root),
        "summary": {
            "roles": len(roles),
            "reviewed_roles": role_coverage["reviewed_roles"],
            "roles_with_statements": role_coverage["roles_with_statements"],
            "reviewed_roles_with_statements": role_coverage["reviewed_roles_with_statements"],
            "min_reviewed_roles": role_coverage["min_reviewed_roles"],
            "role_skills": role_skills["summary"]["total"],
            "role_skills_ready": role_skill_coverage["summary"]["ready"],
            "role_skills_missing": role_skill_coverage["summary"]["missing"],
            "role_agent_exports": role_agent_audit["summary"]["total"],
            "role_agent_completed": role_agent_audit["summary"]["completed"],
            "role_agent_prompt_only": role_agent_audit["summary"]["prompt_only"],
            "role_agent_deliverable": role_agent_audit["summary"]["deliverable"],
            "role_agent_failed": role_agent_audit["summary"]["failed"],
            "role_agent_invalid_completed": role_agent_audit["summary"]["invalid_completed"],
            "role_agent_roles_prompt_ready": role_agent_readiness["summary"]["roles_prompt_ready"],
            "role_agent_roles_live_ready": role_agent_readiness["summary"]["roles_live_ready"],
            "role_agent_roles_missing_prompt": role_agent_readiness["summary"]["roles_missing_prompt"],
            "role_agent_roles_missing_live": role_agent_readiness["summary"]["roles_missing_live"],
            "role_agent_roles_blocked_runtime": role_agent_readiness["summary"]["roles_blocked_runtime"],
            "events": len(events),
            "statements": diagnostics["statement_count"],
            "reports": len(reports),
            "source_configs": len(source_configs),
            "source_adapter_ready": source_adapter_validation["summary"]["ready"],
            "source_adapter_failed": source_adapter_validation["summary"]["failed"],
            "source_runs": source_status["summary"]["total"],
            "source_run_failed": source_status["summary"]["failed"],
            "source_run_active_without_runs": source_status["summary"]["active_without_runs"],
            "source_imports": source_import_status["summary"]["total"],
            "source_import_ready": source_import_status["summary"]["ready"],
            "source_import_failed": source_import_status["summary"]["failed"],
            "source_jobs": source_job_status["summary"]["total"],
            "source_jobs_pending": source_job_status["summary"]["pending"],
            "source_jobs_completed": source_job_status["summary"]["completed"],
            "source_jobs_failed": source_job_status["summary"]["failed"],
            "answer_exports": len(answer_exports),
            "evidence_answer_exports": len(evidence_answer_exports),
            "deliverable_answer_exports": len(deliverable_answer_exports),
            "answer_regression_questions": answer_regression["summary"]["total"],
            "answer_regression_min_questions": answer_regression["summary"]["min_questions"],
            "answer_regression_passed": answer_regression["summary"]["passed"],
            "answer_regression_review": answer_regression["summary"]["review"],
            "answer_regression_failed": answer_regression["summary"]["failed"],
            "answer_regression_missing_provenance": answer_regression["summary"]["missing_provenance"],
            "comparison_exports": len(comparison_exports),
            "deliverable_comparison_exports": len(deliverable_comparison_exports),
            "reviewed_comparison_exports": len(reviewed_comparison_exports),
            "adopted_comparison_exports": len(adopted_comparison_exports),
            "draft_comparison_exports": len(draft_comparison_exports),
            "analysis_exports": analysis_export_summary["total"],
            "analysis_export_ready": analysis_export_summary["ready"],
            "analysis_export_malformed": analysis_export_summary["malformed"],
            "analysis_export_roles": analysis_export_summary["roles"],
            "analysis_export_evidence": analysis_export_summary["evidence"],
            "unreviewed_roles": len(unreviewed_roles),
            "capture_pending": capture_status.get("pending_count", 0),
            "capture_failed": capture_status.get("summary", {}).get("failed", 0),
            "capture_duplicates_skipped": capture_status.get("summary", {}).get("duplicates_skipped", 0),
        },
        "checks": checks,
    }


def write_release_manifest(kb: KnowledgeBase, out_dir: Path | None = None) -> Path:
    target_dir = out_dir or kb.exports_dir / "release"
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = target_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(build_release_manifest(kb), ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    return manifest_path


def write_release_bundle(kb: KnowledgeBase, out_dir: Path | None = None, repo_root: Path | None = None) -> dict[str, Any]:
    root = repo_root.resolve() if repo_root else Path.cwd().resolve()
    target_dir = out_dir or kb.exports_dir / "release" / f"voicevault-v{__version__}"
    target_dir.mkdir(parents=True, exist_ok=True)
    readiness = check_release_readiness(kb)
    manifest = build_release_manifest(kb, repo_root=root)
    manifest["readiness"] = readiness

    readiness_path = target_dir / "readiness.json"
    manifest_path = target_dir / "manifest.json"
    summary_path = target_dir / "release-summary.md"
    plan_path = target_dir / "release-plan.md"
    readiness_path.write_text(json.dumps(readiness, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    summary_path.write_text(_release_summary_markdown(manifest), encoding="utf-8", newline="\n")
    plan_path.write_text(_release_plan_markdown(manifest), encoding="utf-8", newline="\n")

    zip_path = target_dir.parent / f"{target_dir.name}.zip"
    _write_release_zip(target_dir, zip_path)
    zip_sha256 = file_sha256(zip_path)
    zip_sha256_path = write_sha256_file(zip_path, zip_sha256)
    return {
        "ok": readiness["ok"],
        "root": str(kb.root),
        "bundle_dir": str(target_dir),
        "bundle_zip": str(zip_path),
        "bundle_zip_sha256": zip_sha256,
        "bundle_zip_sha256_path": str(zip_sha256_path),
        "files": {
            "readiness_json": str(readiness_path),
            "manifest_json": str(manifest_path),
            "release_summary": str(summary_path),
            "release_plan": str(plan_path),
            "bundle_zip_sha256": str(zip_sha256_path),
        },
    }


def build_release_manifest(kb: KnowledgeBase, repo_root: Path | None = None) -> dict[str, Any]:
    root = repo_root.resolve() if repo_root else Path.cwd().resolve()
    readiness = check_release_readiness(kb)
    reports = list_reports(kb)
    role_skills = list_role_skills(kb)
    role_skill_coverage = audit_role_skill_coverage(kb)
    role_agent_exports = list_role_agent_exports(kb)
    role_agent_audit = audit_role_agent_exports(kb)
    role_agent_readiness = audit_role_agent_readiness(kb)
    answer_exports = list_answer_exports(kb)
    answer_regression = audit_answer_regression_coverage(kb)
    comparison_exports = list_comparison_exports(kb)
    source_configs = list_sources(kb)
    source_status = read_source_status(kb)
    source_import_status = read_source_import_status(kb)
    source_job_status = read_source_job_status(kb)
    evidence_answer_exports = [item for item in answer_exports if item["evidence_backed"]]
    deliverable_answer_exports = [item for item in answer_exports if is_deliverable_answer_export(item)]
    deliverable_comparison_exports = [item for item in comparison_exports if is_deliverable_comparison_export(item)]
    adopted_comparison_exports = [item for item in comparison_exports if is_adopted_comparison_export(item)]
    analysis_exports = list_analysis_exports(kb)
    analysis_export_summary = summarize_analysis_exports(analysis_exports)
    malformed_analysis_exports = [item for item in analysis_exports if item["status"] == "malformed"]
    dashboard_path = kb.exports_dir / "dashboard" / "index.html"
    ui_path = kb.exports_dir / "ui" / "index.html"
    ui_data_path = kb.exports_dir / "ui" / "data.json"
    return {
        "schema_version": RELEASE_SCHEMA_VERSION,
        "product": {
            "chinese_name": "声迹",
            "english_name": "VoiceVault",
            "repository": "public-voice-archive",
            "version": __version__,
        },
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "repo_root": str(root),
        "knowledge_base": str(kb.root),
        "readiness": readiness,
        "artifacts": {
            "dashboard": str(dashboard_path),
            "ui": str(ui_path),
            "ui_data": str(ui_data_path),
            "role_skills": [item["skill_json"] for item in role_skills["skills"] if item.get("status") == "ready"],
            "role_skill_coverage": role_skill_coverage,
            "role_agent_exports": [item["role_agent_json"] for item in role_agent_exports],
            "role_agent_quality": role_agent_audit,
            "role_agent_readiness": role_agent_readiness,
            "answer_exports": [item["answer_json"] for item in answer_exports],
            "comparison_exports": [item["comparison_json"] for item in comparison_exports],
            "source_configs": [item["config_path"] for item in source_configs],
            "source_adapter_validation": str(kb.sources_dir),
            "source_status": source_status["status_path"],
            "source_import_status": source_import_status["status_path"],
            "source_jobs": source_job_status["status_path"],
            "evidence_answer_exports": [item["answer_json"] for item in evidence_answer_exports],
            "deliverable_answer_exports": [item["answer_json"] for item in deliverable_answer_exports],
            "answer_regression_suite": answer_regression["suite_path"],
            "answer_regression_changelog": answer_regression["changelog_path"],
            "deliverable_comparison_exports": [item["comparison_json"] for item in deliverable_comparison_exports],
            "adopted_comparison_exports": [item["comparison_json"] for item in adopted_comparison_exports],
            "analysis_exports": [item["analysis_json"] for item in analysis_exports],
            "analysis_export_status": {
                "summary": analysis_export_summary,
                "malformed": [_analysis_export_problem(item) for item in malformed_analysis_exports],
            },
            "reports": [report["path"] for report in reports],
            "index": str(kb.index_path),
        },
        "next_release_actions": [
            "Archive this manifest with the release notes.",
            "Keep real knowledge-base data outside the repository.",
            "Run release check again after adding new roles, captures, events, or reports.",
        ],
    }


def _required_dirs(kb: KnowledgeBase) -> list:
    return [
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


def _release_summary_markdown(manifest: dict[str, Any]) -> str:
    product = manifest["product"]
    readiness = manifest["readiness"]
    summary = readiness["summary"]
    checks = readiness["checks"]
    artifacts = manifest["artifacts"]
    status = "通过" if readiness["ok"] else "未通过"
    lines = [
        "# 声迹 VoiceVault 发布交付包",
        "",
        f"- 产品：{product['chinese_name']} / {product['english_name']}",
        f"- 版本：{product['version']}",
        f"- 仓库：{product['repository']}",
        f"- 知识库：{manifest['knowledge_base']}",
        f"- 发布验收：{status}",
        "",
        "## 验收摘要",
        "",
        f"- 角色：{summary['roles']}",
        f"- Role coverage：{summary['reviewed_roles_with_statements']} / {summary['min_reviewed_roles']} reviewed roles with statements",
        f"- Role Skills：ready {summary['role_skills_ready']}，missing {summary['role_skills_missing']}",
        f"- Role Agent：live-ready roles {summary['role_agent_roles_live_ready']}，prompt-ready roles {summary['role_agent_roles_prompt_ready']}，missing live {summary['role_agent_roles_missing_live']}；deliverable {summary['role_agent_deliverable']} / {summary['role_agent_exports']}，failed {summary['role_agent_failed']}，invalid completed {summary['role_agent_invalid_completed']}",
        f"- Statements：{summary['statements']}",
        f"- 事件：{summary['events']}",
        f"- 报告：{summary['reports']}",
        f"- 采集源：{summary['source_configs']}",
        f"- Source adapters：ready {summary['source_adapter_ready']}，失败 {summary['source_adapter_failed']}",
        f"- Source runs：{summary['source_runs']}（失败 {summary['source_run_failed']}）",
        f"- Source import status：{summary['source_imports']}（ready {summary['source_import_ready']}，失败 {summary['source_import_failed']}）",
        f"- Source jobs：{summary['source_jobs']}（待处理 {summary['source_jobs_pending']}，失败 {summary['source_jobs_failed']}）",
        f"- Analysis exports：ready {summary['analysis_export_ready']} / {summary['analysis_exports']}，malformed {summary['analysis_export_malformed']}，证据 {summary['analysis_export_evidence']}",
        f"- 可交付答案：{summary['deliverable_answer_exports']} / {summary['answer_exports']}",
        f"- Answer regression：{summary['answer_regression_passed']} / {summary['answer_regression_questions']} passed，missing provenance {summary['answer_regression_missing_provenance']}",
        f"- 已采纳角色对比：{summary['adopted_comparison_exports']} / {summary['comparison_exports']}，draft {summary['draft_comparison_exports']}",
        f"- Capture backlog：{summary['capture_pending']}",
        "",
        "## 检查项",
        "",
    ]
    for check in checks:
        marker = "OK" if check["ok"] else "FAIL"
        lines.append(f"- {marker} `{check['id']}`：{check['message']}")
    lines.extend(
        [
            "",
            "## 关键产物",
            "",
            f"- UI：{artifacts['ui']}",
            f"- UI data：{artifacts['ui_data']}",
            f"- Dashboard：{artifacts['dashboard']}",
            f"- Role Agent exports：{len(artifacts['role_agent_exports'])}",
            f"- Source import status：{artifacts['source_import_status']}",
            f"- Analysis exports：{len(artifacts['analysis_exports'])}",
            f"- Release manifest：{manifest['knowledge_base']}\\exports\\release\\manifest.json",
            f"- Reports：{len(artifacts['reports'])}",
            f"- Deliverable answers：{len(artifacts['deliverable_answer_exports'])}",
            f"- Adopted comparisons：{len(artifacts['adopted_comparison_exports'])}",
            "",
            "## 数据边界",
            "",
            "- 发布包只包含 release metadata、验收结果和交付说明。",
            "- 真实知识库、私有采集文件、密钥、cookie、音频样本和平台缓存不进入仓库或发布包。",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def _release_plan_markdown(manifest: dict[str, Any]) -> str:
    version = manifest["product"]["version"]
    kb_path = manifest["knowledge_base"]
    repo_root = str(manifest["repo_root"])
    readiness_summary = manifest.get("readiness", {}).get("summary", {})
    regression_min_questions = readiness_summary.get("answer_regression_min_questions", 4)
    cli_package = f"dist\\voicevault-cli-v{version}.zip"
    cli_install_guide = f"dist\\voicevault-cli-v{version}-INSTALL.md"
    cli_manifest = f"dist\\voicevault-cli-v{version}-manifest.json"
    ship_manifest = f"{repo_root}\\dist\\voicevault-v{version}-ship-manifest.json"
    ship_summary = f"dist\\voicevault-v{version}-ship-summary.md"
    verification_report = f"dist\\voicevault-v{version}-verification-report.json"
    return (
        "# 发布上线计划\n\n"
        f"版本：{version}\n\n"
        "## 发布前\n\n"
        "- 运行 `python -m unittest`。\n"
        f"- 运行 `voicevault release prepare --kb {kb_path} --root {repo_root} --json`，生成 dashboard、本地 UI、quickstart、release bundle 和 prepare report。\n"
        f"- 运行 `voicevault release check --kb {kb_path} --json`。\n"
        f"- 运行 `voicevault roles coverage --kb {kb_path} --json`，确认至少两个已审阅且有 statement 的公开声音可用于多角色问答和对比。\n"
        f"- 运行 `voicevault role skills --kb {kb_path} --json`，确认每个 ready role 都已蒸馏为 Role Skill；缺失时运行 `voicevault role distill --kb {kb_path} --role <role_id> --json`。\n"
        f"- 运行 `voicevault role readiness --kb {kb_path} --json`，确认每个 ready role 至少有 evidence-backed Role Agent prompt；若要发布 live Role Agent 能力，运行 `voicevault release check --kb {kb_path} --require-live-role-agent --json`。\n"
        f"- 运行 `voicevault sources list --kb {kb_path} --json`，确认至少一个 active source config。\n"
        f"- 如有新的 CSV/JSON/JSONL 公开导出文件，运行 `voicevault sources import --kb {kb_path} --source <source_id> --input <export.csv> --json`，完成 normalize、adapter validation 和 dry-run 预检。\n"
        f"- 运行 `voicevault sources imports --kb {kb_path} --json`，确认 Source import status 已记录最新导入。\n"
        f"- 如需只更新本地 adapter 输入，运行 `voicevault sources normalize --kb {kb_path} --source <source_id> --input <export.csv> --update-source --json`。\n"
        f"- 运行 `voicevault sources validate --kb {kb_path} --json`，确认 source adapter 配置可运行。\n"
        f"- 运行 `voicevault sources status --kb {kb_path} --json`，确认 Source run status 健康。\n"
        f"- 运行 `voicevault sources jobs --kb {kb_path} --json`，确认 Source job queue 没有 pending 或 failed 任务；如有 pending，先运行 `voicevault sources drain --kb {kb_path} --dry-run --json`；如有 failed，运行 `voicevault sources retry --kb {kb_path} --job <job_id> --json`。\n"
        f"- 运行 `voicevault analyses list --kb {kb_path} --json`，确认 analysis exports 至少一个 ready 且 malformed 为 0。\n"
        f"- 运行 `voicevault answers list --kb {kb_path} --status invalid --json`，确认 invalid 为 0。\n"
        f"- 运行 `voicevault evaluations answers --kb {kb_path} --json`，确认固定问答回归至少 {regression_min_questions} 条、全部通过且具备 source URL / rationale / owner / timestamps。\n"
        f"- 批量维护固定问答时，先 `voicevault evaluations export --kb {kb_path} --out <answer-regression-suite.json> --json`，编辑后运行 `voicevault evaluations import --kb {kb_path} --input <answer-regression-suite.json> --yes --updated-by <owner> --json`。\n"
        f"- 运行 `voicevault comparisons list --kb {kb_path} --json`，确认已有 comparison exports 均已 review，发布用对比至少一个 adopted。\n"
        f"- 对 draft 对比运行 `voicevault comparisons review --kb {kb_path} --query <query> --status adopted --reviewer <name> --notes <notes> --json`。\n"
        f"- 运行 `voicevault release package --root {repo_root} --json`，生成 CLI 分发包。\n"
        f"- 运行 `voicevault release ship --root {repo_root} --kb {kb_path} --json`，生成最终 ship manifest 和 ship summary。\n"
        f"- 运行 `voicevault release verify --manifest {ship_manifest} --json`，确认最终交付合同全部通过。\n"
        "- 人工审阅 `release-summary.md` 和 `manifest.json`。\n\n"
        "## 发布\n\n"
        "- 归档本目录和同名 zip。\n"
        f"- 归档 `{cli_package}`、`{cli_install_guide}` 和 `{cli_manifest}`。\n"
        f"- 归档 `{ship_manifest}`、`{ship_summary}` 和 `{verification_report}`。\n"
        "- 在仓库中保留代码、测试、文档和 release notes，不提交真实知识库内容。\n"
        "- 将本地 UI、dashboard、reports、answers 的路径作为交付清单交给使用者。\n\n"
        "## 数据边界\n\n"
        "- 不提交真实知识库内容。\n"
        "- 不复制密钥、cookie、音频样本或平台缓存到仓库、dist 或 KB release zip。\n"
        "- KB release bundle 只包含 release metadata、验收结果和交付说明。\n\n"
        "## 发布后\n\n"
        "- 新增角色、采集批次、事件或答案后重新运行 release prepare。\n"
        "- 如果 release check 出现 invalid answer export，先用 `answers prune --dry-run` 核对，再决定是否 `--yes` 清理。\n"
    )


def _write_release_zip(source_dir: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with ZipFile(zip_path, "w", ZIP_DEFLATED) as archive:
        for path in sorted(source_dir.iterdir()):
            if path.is_file():
                archive.write(path, arcname=path.name)


def _add_check(
    checks: list[dict[str, Any]],
    check_id: str,
    ok: bool,
    message: str,
    details: dict[str, Any] | None = None,
    remediation: str | None = None,
) -> None:
    check = {"id": check_id, "ok": ok, "message": message}
    if details:
        check["details"] = details
    if remediation and not ok:
        check["remediation"] = remediation
    checks.append(check)


def _analysis_export_problem(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": item.get("event_id", ""),
        "analysis_json": item.get("analysis_json", ""),
        "error": item.get("error", ""),
    }


def _answer_export_problem(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "query": item.get("query", ""),
        "answer_json": item.get("answer_json", ""),
        "status": item.get("status", ""),
        "schema_version": item.get("schema_version", 0),
        "contract_errors": item.get("contract_errors", []),
        "error": item.get("error", ""),
    }


def _answer_regression_problem(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id", ""),
        "query": item.get("query", ""),
        "status": item.get("status", ""),
        "failed_checks": item.get("failed_checks", []),
        "source_url": item.get("source_url", ""),
        "rationale": item.get("rationale", ""),
        "updated_by": item.get("updated_by", ""),
        "answer_json": item.get("answer_json", ""),
    }


def _comparison_export_problem(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "query": item.get("query", ""),
        "comparison_json": item.get("comparison_json", ""),
        "status": item.get("status", ""),
        "review_status": item.get("review_status", ""),
        "schema_version": item.get("schema_version", 0),
        "contract_errors": item.get("contract_errors", []),
        "error": item.get("error", ""),
    }
