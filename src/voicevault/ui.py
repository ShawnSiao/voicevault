from __future__ import annotations

import json
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from . import __version__
from .action_runs import read_action_runs
from .accounts import list_accounts, read_account_status
from .analysis_exports import list_analysis_exports
from .answer import is_deliverable_answer_export, list_answer_exports
from .answer_quality import audit_answer_quality
from .answer_regression import audit_answer_regression_coverage
from .collections import list_reports
from .comparison import (
    is_adopted_comparison_export,
    is_deliverable_comparison_export,
    is_reviewed_comparison_export,
    list_comparison_exports,
)
from .diagnostics import inspect_kb
from .events import list_events
from .importers import load_statements_from_kb
from .kb import KnowledgeBase
from .next_actions import build_research_action_audit, build_research_next_actions
from .remediation import build_remediation_queue
from .release import check_release_readiness
from .role_agent import (
    audit_role_agent_exports,
    audit_role_agent_readiness,
    inspect_role_agent_runtime,
    list_role_agent_exports,
    summarize_role_agent_exports,
)
from .role_skill import audit_role_skill_coverage, list_role_skills
from .roles import evaluate_role_coverage, list_role_summaries
from .source_imports import read_source_import_status
from .source_jobs import read_source_job_status
from .sources import list_sources, read_source_status, validate_source_adapters
from .sync import read_capture_status, read_sync_status


def build_ui_data(
    kb: KnowledgeBase,
    statement_limit: int = 250,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    resolved_repo_root = repo_root.resolve() if repo_root else None
    diagnostics = inspect_kb(kb)
    roles = list_role_summaries(kb)
    role_coverage = evaluate_role_coverage(kb)
    role_skills = list_role_skills(kb)
    role_skill_coverage = audit_role_skill_coverage(kb)
    role_agent_exports = list_role_agent_exports(kb)
    role_agent_summary = summarize_role_agent_exports(role_agent_exports)
    role_agent_audit = audit_role_agent_exports(kb)
    role_agent_readiness = audit_role_agent_readiness(kb)
    role_agent_runtime = inspect_role_agent_runtime()
    events = list_events(kb)
    reports = list_reports(kb)
    analysis_exports = list_analysis_exports(kb)
    answer_exports = list_answer_exports(kb)
    answer_quality = audit_answer_quality(kb)
    answer_regression = audit_answer_regression_coverage(kb)
    comparison_exports = list_comparison_exports(kb)
    source_configs = list_sources(kb)
    source_adapter_validation = validate_source_adapters(kb)
    source_status = read_source_status(kb)
    source_import_status = read_source_import_status(kb)
    source_job_status = read_source_job_status(kb)
    account_archives = list_accounts(kb)
    account_status = read_account_status(kb)
    action_runs = read_action_runs(kb)
    evidence_answer_exports = [item for item in answer_exports if item["evidence_backed"]]
    deliverable_answer_exports = [item for item in answer_exports if is_deliverable_answer_export(item)]
    evidence_comparison_exports = [item for item in comparison_exports if item["evidence_backed"]]
    deliverable_comparison_exports = [item for item in comparison_exports if is_deliverable_comparison_export(item)]
    reviewed_comparison_exports = [item for item in comparison_exports if is_reviewed_comparison_export(item)]
    adopted_comparison_exports = [item for item in comparison_exports if is_adopted_comparison_export(item)]
    draft_comparison_exports = [item for item in comparison_exports if item.get("review_status") == "draft"]
    statements = sorted(load_statements_from_kb(kb), key=_statement_sort_key, reverse=True)
    capture_status = read_capture_status(kb)
    sync_status = read_sync_status(kb)
    release_readiness = check_release_readiness(kb)
    release_blockers = [check for check in release_readiness["checks"] if not check["ok"]]
    release_actions = _release_actions(kb, release_readiness, repo_root=resolved_repo_root)
    next_actions = build_research_next_actions(kb)
    next_action_audit = build_research_action_audit(kb)
    remediation_queue = build_remediation_queue(kb)
    return {
        "schema_version": 1,
        "product": {
            "chinese_name": "声迹",
            "english_name": "VoiceVault",
            "repository": "public-voice-archive",
            "version": __version__,
        },
        "generated_at": _now_utc(),
        "knowledge_base": str(kb.root),
        "repo_root": str(resolved_repo_root) if resolved_repo_root else "",
        "summary": {
            "roles": len(roles),
            "reviewed_roles": role_coverage["reviewed_roles"],
            "roles_with_statements": role_coverage["roles_with_statements"],
            "reviewed_roles_with_statements": role_coverage["reviewed_roles_with_statements"],
            "min_reviewed_roles": role_coverage["min_reviewed_roles"],
            "statements": diagnostics["statement_count"],
            "events": len(events),
            "reports": len(reports),
            "analysis_exports": len(analysis_exports),
            "source_configs": len(source_configs),
            "account_archives": len(account_archives),
            "account_archives_blocked": account_status["summary"]["blocked"],
            "source_adapter_failed": source_adapter_validation["summary"]["failed"],
            "source_runs": source_status["summary"]["total"],
            "source_run_failed": source_status["summary"]["failed"],
            "source_imports": source_import_status["summary"]["total"],
            "source_import_ready": source_import_status["summary"]["ready"],
            "source_import_failed": source_import_status["summary"]["failed"],
            "source_jobs": source_job_status["summary"]["total"],
            "source_jobs_pending": source_job_status["summary"]["pending"],
            "source_jobs_failed": source_job_status["summary"]["failed"],
            "action_runs": action_runs["summary"]["total"],
            "action_run_failed": action_runs["summary"]["failed"],
            "action_run_retryable_failed": action_runs["summary"]["retryable_failed"],
            "remediation_items": remediation_queue["summary"]["total"],
            "remediation_ready": remediation_queue["summary"]["ready"],
            "remediation_blocked": remediation_queue["summary"]["blocked"],
            "answer_exports": len(answer_exports),
            "answer_quality_passed": answer_quality["summary"]["passed"],
            "answer_quality_review": answer_quality["summary"]["review"],
            "answer_quality_failed": answer_quality["summary"]["failed"],
            "answer_regression_passed": answer_regression["summary"]["passed"],
            "answer_regression_review": answer_regression["summary"]["review"],
            "answer_regression_failed": answer_regression["summary"]["failed"],
            "answer_regression_questions": answer_regression["summary"]["total"],
            "answer_regression_min_questions": answer_regression["summary"]["min_questions"],
            "answer_regression_missing_provenance": answer_regression["summary"]["missing_provenance"],
            "role_skills": role_skills["summary"]["total"],
            "role_skills_ready": role_skill_coverage["summary"]["ready"],
            "role_skills_missing": role_skill_coverage["summary"]["missing"],
            "role_agent_exports": role_agent_summary["total"],
            "role_agent_completed": role_agent_summary["completed"],
            "role_agent_prompt_only": role_agent_summary["prompt_only"],
            "role_agent_deliverable": role_agent_audit["summary"]["deliverable"],
            "role_agent_failed": role_agent_audit["summary"]["failed"],
            "role_agent_invalid_completed": role_agent_audit["summary"]["invalid_completed"],
            "role_agent_runtime_configured": role_agent_runtime["configured"],
            "role_agent_roles_prompt_ready": role_agent_readiness["summary"]["roles_prompt_ready"],
            "role_agent_roles_live_ready": role_agent_readiness["summary"]["roles_live_ready"],
            "role_agent_roles_missing_prompt": role_agent_readiness["summary"]["roles_missing_prompt"],
            "role_agent_roles_missing_live": role_agent_readiness["summary"]["roles_missing_live"],
            "role_agent_roles_blocked_runtime": role_agent_readiness["summary"]["roles_blocked_runtime"],
            "evidence_answer_exports": len(evidence_answer_exports),
            "deliverable_answer_exports": len(deliverable_answer_exports),
            "comparison_exports": len(comparison_exports),
            "evidence_comparison_exports": len(evidence_comparison_exports),
            "deliverable_comparison_exports": len(deliverable_comparison_exports),
            "reviewed_comparison_exports": len(reviewed_comparison_exports),
            "adopted_comparison_exports": len(adopted_comparison_exports),
            "draft_comparison_exports": len(draft_comparison_exports),
            "release_ready": release_readiness["ok"],
            "release_blockers": len(release_blockers),
            "next_actions": len(next_actions),
            "completed_next_actions": len(next_action_audit["completed_actions"]),
            "capture_pending": capture_status.get("pending_count", 0),
            "capture_failed": capture_status.get("summary", {}).get("failed", 0),
            "capture_duplicates_skipped": capture_status.get("summary", {}).get("duplicates_skipped", 0),
        },
        "roles": roles,
        "role_coverage": role_coverage,
        "role_skills": role_skills,
        "role_skill_coverage": role_skill_coverage,
        "role_agent_exports": role_agent_exports,
        "role_agent_audit": role_agent_audit,
        "role_agent_readiness": role_agent_readiness,
        "role_agent_runtime": role_agent_runtime,
        "events": events,
        "reports": reports,
        "analysis_exports": analysis_exports,
        "source_configs": source_configs,
        "account_archives": account_archives,
        "account_status": account_status,
        "source_adapter_validation": source_adapter_validation,
        "source_status": source_status,
        "source_import_status": source_import_status,
        "source_job_status": source_job_status,
        "action_runs": action_runs,
        "remediation_queue": remediation_queue,
        "answer_exports": answer_exports,
        "answer_quality": answer_quality,
        "answer_regression": answer_regression,
        "comparison_exports": comparison_exports,
        "statements": [_statement_payload(statement) for statement in statements[:statement_limit]],
        "capture_status": capture_status,
        "sync_status": sync_status,
        "release_readiness": release_readiness,
        "release_actions": release_actions,
        "next_actions": next_actions,
        "next_action_audit": next_action_audit,
    }


def write_local_ui(
    kb: KnowledgeBase,
    out_dir: Path | None = None,
    repo_root: Path | None = None,
) -> Path:
    target_dir = out_dir or kb.exports_dir / "ui"
    target_dir.mkdir(parents=True, exist_ok=True)
    data_path = target_dir / "data.json"
    index_path = target_dir / "index.html"

    # Release readiness includes the UI artifacts, so reserve them before building data.
    data_path.touch(exist_ok=True)
    index_path.touch(exist_ok=True)
    data = build_ui_data(kb, repo_root=repo_root)
    data_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    index_path.write_text(_html(data), encoding="utf-8", newline="\n")
    return index_path


def _statement_payload(statement: Any) -> dict[str, Any]:
    payload = statement.to_dict()
    payload["display_time"] = statement.published_at or statement.captured_at or "unknown"
    payload["excerpt"] = _excerpt(statement.body, 220)
    return payload


def _statement_sort_key(statement: Any) -> str:
    return statement.published_at or statement.captured_at or ""


def _excerpt(text: str, limit: int) -> str:
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _release_actions(
    kb: KnowledgeBase,
    readiness: dict[str, Any],
    repo_root: Path | None = None,
) -> list[dict[str, str]]:
    failed = [check for check in readiness["checks"] if not check["ok"]]
    root = str(repo_root) if repo_root else "<repo_root>"
    if not failed:
        return [
            {
                "status": "ready",
                "phase": "release_handoff",
                "check_id": "release_ship",
                "action": "Run release ship to regenerate final handoff artifacts.",
                "command": f"voicevault release ship --root {root} --kb {kb.root} --json",
            },
            {
                "status": "ready",
                "phase": "post_handoff_verify",
                "check_id": "release_verify",
                "action": "Run release verify against the generated ship manifest.",
                "command": f"voicevault release verify --manifest {root}\\dist\\voicevault-v{__version__}-ship-manifest.json --json",
            },
        ]

    actions: list[dict[str, str]] = []
    for check in failed:
        actions.append(
            {
                "status": "blocked",
                "phase": _phase_for_release_check(check["id"]),
                "check_id": check["id"],
                "action": check["message"],
                "command": check.get("remediation", f"Run: voicevault release check --kb {kb.root} --json"),
            }
        )
    return actions


def _phase_for_release_check(check_id: str) -> str:
    if check_id in {"required_dirs", "index", "roles", "profiles_reviewed", "role_coverage", "role_skills", "sample_content"}:
        return "setup"
    if check_id in {"capture_status", "sync_status"}:
        return "capture"
    if check_id in {"sources", "source_adapters", "source_runs", "source_jobs"}:
        return "source_jobs"
    if check_id in {"analysis_exports", "answer_exports", "reports", "dashboard", "ui", "events"}:
        return "research_outputs"
    return "release_handoff"


def _html(data: dict[str, Any]) -> str:
    embedded = escape(json.dumps(data, ensure_ascii=False), quote=False)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>声迹本地工作台</title>
  <style>
    :root {{
      --bg: #f6f3ed;
      --bg-rail: #ece7dc;
      --surface: #fffdf8;
      --surface-alt: #f4efe5;
      --surface-muted: #ebe5d9;
      --border: #d8d0c2;
      --border-strong: #bdb1a1;
      --text: #202326;
      --text-strong: #101315;
      --muted: #6f6a62;
      --subtle: #91887b;
      --accent: #0f766e;
      --accent-strong: #0a4f4b;
      --accent-soft: #dbeee9;
      --blue: #315f9f;
      --blue-soft: #e0e9f5;
      --warn: #9b5a13;
      --warn-soft: #f4e2c8;
      --bad: #a33e4e;
      --bad-soft: #f1d9dd;
      --ok: #1f7a55;
      --ok-soft: #dbeade;
      --shadow: 0 14px 34px rgba(32, 35, 38, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    html {{ min-height: 100%; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, "SF Pro Text", "Segoe UI", "PingFang SC", "Microsoft YaHei", "Noto Sans SC", sans-serif;
      font-size: 14px;
      line-height: 1.68;
      text-wrap: pretty;
    }}
    a {{ color: var(--accent-strong); text-decoration-thickness: 1px; text-underline-offset: 3px; }}
    .app-shell {{
      display: grid;
      grid-template-columns: minmax(232px, 276px) minmax(0, 1fr);
      min-height: 100vh;
    }}
    .sidebar {{
      position: sticky;
      top: 0;
      align-self: start;
      display: grid;
      grid-template-rows: auto 1fr auto;
      gap: 18px;
      min-height: 100vh;
      padding: 22px 16px;
      border-right: 1px solid var(--border);
      background:
        linear-gradient(180deg, rgba(255, 253, 248, 0.72), rgba(236, 231, 220, 0.92)),
        var(--bg-rail);
    }}
    .brand-panel {{
      display: grid;
      gap: 12px;
      padding: 14px;
      border: 1px solid rgba(189, 177, 161, 0.72);
      border-radius: 8px;
      background: rgba(255, 253, 248, 0.72);
      box-shadow: 0 8px 22px rgba(32, 35, 38, 0.05);
    }}
    .brand-mark {{
      display: inline-grid;
      place-items: center;
      width: 34px;
      height: 34px;
      border-radius: 8px;
      background: var(--text-strong);
      color: #fffdf8;
      font-weight: 800;
      letter-spacing: 0;
    }}
    .brand-title {{
      display: grid;
      gap: 2px;
    }}
    .brand-title h1 {{
      margin: 0;
      font-size: 20px;
      line-height: 1.15;
      color: var(--text-strong);
      letter-spacing: 0;
    }}
    .brand-title p {{
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }}
    .workspace {{
      width: 100%;
      max-width: 1480px;
      margin: 0 auto;
      padding: 24px;
    }}
    .topbar {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 20px;
      align-items: center;
      min-height: 76px;
      padding: 0 0 20px;
      border-bottom: 1px solid var(--border);
    }}
    .eyebrow {{
      margin: 0 0 4px;
      color: var(--subtle);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .page-title {{
      margin: 0;
      font-size: 26px;
      line-height: 1.16;
      color: var(--text-strong);
      letter-spacing: 0;
    }}
    .page-subtitle {{
      margin: 7px 0 0;
      max-width: 880px;
      color: var(--muted);
      overflow-wrap: anywhere;
    }}
    h1 {{ margin: 0; font-size: 26px; line-height: 1.15; }}
    h2 {{ margin: 0; font-size: 16px; line-height: 1.25; color: var(--text-strong); }}
    h3 {{ margin: 0; font-size: 14px; line-height: 1.3; color: var(--text-strong); }}
    p {{ margin: 0; color: var(--muted); }}
    button, input, select, textarea {{
      font: inherit;
      border: 1px solid var(--border);
      border-radius: 7px;
      background: var(--surface);
      color: var(--text);
    }}
    button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
      min-height: 36px;
      padding: 0 12px;
      cursor: pointer;
      font-weight: 700;
      transition: border-color 140ms ease, background 140ms ease, color 140ms ease, transform 140ms ease;
    }}
    button:hover {{ border-color: var(--border-strong); background: var(--surface-alt); }}
    button:active {{ transform: translateY(1px); }}
    button.active {{ border-color: var(--accent); background: var(--accent-soft); color: var(--accent-strong); }}
    button:disabled {{ cursor: not-allowed; opacity: 0.58; }}
    button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible {{
      outline: 2px solid rgba(15, 118, 110, 0.35);
      outline-offset: 2px;
    }}
    input, select, textarea {{ min-height: 38px; padding: 0 10px; }}
    textarea {{ min-height: 112px; padding: 10px; resize: vertical; }}
    .topline {{ display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 34px;
      padding: 0 12px;
      border-radius: 7px;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--muted);
      font-weight: 800;
      white-space: nowrap;
    }}
    .badge.ok {{ color: var(--ok); background: var(--ok-soft); border-color: #bad6c4; }}
    .badge.warn {{ color: var(--warn); background: var(--warn-soft); border-color: #e6c89d; }}
    .nav {{
      display: grid;
      gap: 6px;
      align-content: start;
      overflow-y: auto;
      padding: 2px;
    }}
    .nav button {{
      justify-content: flex-start;
      min-height: 42px;
      width: 100%;
      padding: 0 12px;
      border-color: transparent;
      background: transparent;
      color: var(--muted);
    }}
    .nav button:hover {{ background: rgba(255, 253, 248, 0.65); }}
    .nav button.active {{
      border-color: rgba(15, 118, 110, 0.24);
      background: var(--surface);
      color: var(--text-strong);
      box-shadow: 0 8px 18px rgba(32, 35, 38, 0.06);
    }}
    .nav-index {{
      display: inline-grid;
      place-items: center;
      width: 22px;
      height: 22px;
      border-radius: 7px;
      background: var(--surface-muted);
      color: var(--subtle);
      font-size: 11px;
      font-weight: 900;
    }}
    .nav button.active .nav-index {{ background: var(--accent); color: #fffdf8; }}
    .sidebar-footer {{
      display: grid;
      gap: 8px;
      color: var(--subtle);
      font-size: 12px;
      line-height: 1.45;
    }}
    .path-chip {{
      display: block;
      max-width: 100%;
      overflow-wrap: anywhere;
      padding: 10px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: rgba(255, 253, 248, 0.62);
      color: var(--muted);
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(156px, 1fr));
      gap: 10px;
      margin: 18px 0;
    }}
    .metric, .panel {{
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
    }}
    .metric {{
      position: relative;
      min-height: 92px;
      padding: 14px;
      overflow: hidden;
    }}
    .metric::before {{
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 4px;
      background: var(--border);
    }}
    .metric[data-tone="ok"]::before {{ background: var(--ok); }}
    .metric[data-tone="warn"]::before {{ background: var(--warn); }}
    .metric[data-tone="bad"]::before {{ background: var(--bad); }}
    .metric[data-tone="accent"]::before {{ background: var(--accent); }}
    .metric span {{ display: block; color: var(--muted); font-size: 11px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.05em; }}
    .metric strong {{ display: block; margin-top: 9px; font-size: 25px; line-height: 1; color: var(--text-strong); }}
    .grid {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 14px; }}
    .grid > .full {{ grid-column: 1 / -1; }}
    .view {{
      animation: enter 180ms ease;
    }}
    .view.panel, .panel {{
      padding: 16px;
      min-width: 0;
      box-shadow: 0 1px 0 rgba(255, 255, 255, 0.62) inset;
    }}
    .view-header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 14px;
    }}
    .view-header p {{ margin-top: 4px; }}
    .section-stack {{ display: grid; gap: 14px; }}
    .panel-stack {{ display: grid; gap: 12px; }}
    .toolbar {{ display: grid; grid-template-columns: minmax(180px, 1fr) 180px 160px; gap: 10px; margin-bottom: 12px; align-items: center; }}
    .ask-form {{
      display: grid;
      gap: 10px;
      margin-bottom: 14px;
      padding: 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface-alt);
    }}
    .ask-grid {{ display: grid; grid-template-columns: minmax(220px, 2fr) 180px 120px 140px 88px auto; gap: 10px; }}
    .regression-grid {{ display: grid; grid-template-columns: minmax(220px, 2fr) 140px 150px 120px 140px 150px 88px auto auto; gap: 10px; align-items: center; }}
    .onboarding-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; align-items: start; }}
    .onboarding-grid .wide {{ grid-column: span 2; }}
    .onboarding-grid .full {{ grid-column: 1 / -1; }}
    .check {{ display: inline-flex; align-items: center; gap: 8px; min-height: 38px; color: var(--muted); }}
    .check input {{ min-height: auto; }}
    .answer-status {{ min-height: 22px; color: var(--muted); font-size: 13px; }}
    .answer-status.ok {{ color: var(--ok); }}
    .answer-status.error {{ color: var(--bad); }}
    .route-hints {{ margin: 10px 0; padding: 10px; border-radius: 8px; background: var(--surface-alt); border: 1px solid rgba(216, 208, 194, 0.75); }}
    #liveAnswer {{ margin-bottom: 12px; }}
    table {{ width: 100%; border-collapse: separate; border-spacing: 0; }}
    .action-table-scroll {{
      width: 100%;
      overflow-x: auto;
      margin-bottom: 16px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
    }}
    .action-table-scroll table {{ min-width: 760px; }}
    th, td {{ padding: 10px 9px; border-top: 1px solid var(--border); text-align: left; vertical-align: top; font-size: 13px; }}
    thead tr:first-child th {{ border-top: 0; }}
    th {{ color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; background: var(--surface-alt); }}
    tbody tr:hover td {{ background: rgba(244, 239, 229, 0.45); }}
    code {{ color: var(--accent); font-family: "Cascadia Mono", Consolas, monospace; font-size: 12px; }}
    code.command {{ display: block; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--text); }}
    .list {{ display: grid; gap: 10px; }}
    .statement {{
      display: grid;
      gap: 8px;
      padding: 13px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
    }}
    .statement:first-child {{ border-top: 1px solid var(--border); padding-top: 13px; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0; color: var(--muted); font-size: 12px; }}
    .pill {{ display: inline-flex; align-items: center; min-height: 24px; padding: 0 8px; border-radius: 999px; background: var(--surface-alt); color: var(--muted); border: 1px solid rgba(216, 208, 194, 0.78); }}
    .status-pill {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 0 8px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: var(--surface-alt);
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      white-space: nowrap;
    }}
    .status-pill.ok {{ color: var(--ok); background: var(--ok-soft); border-color: #bad6c4; }}
    .status-pill.warn {{ color: var(--warn); background: var(--warn-soft); border-color: #e6c89d; }}
    .status-pill.bad {{ color: var(--bad); background: var(--bad-soft); border-color: #e5bcc4; }}
    .status-pill.accent {{ color: var(--accent-strong); background: var(--accent-soft); border-color: #bad6d1; }}
    .key-points {{ margin: 10px 0 0; padding-left: 18px; color: var(--text); }}
    .key-points li {{ margin: 6px 0; }}
    .hidden {{ display: none; }}
    .empty {{ padding: 16px; border: 1px dashed var(--border-strong); border-radius: 8px; color: var(--muted); background: rgba(255, 253, 248, 0.62); }}
    .split-rail {{ display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(320px, 0.7fr); gap: 14px; }}
    .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }}
    .summary-tile {{ padding: 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-alt); }}
    .summary-tile span {{ display: block; color: var(--muted); font-size: 11px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.05em; }}
    .summary-tile strong {{ display: block; margin-top: 6px; font-size: 21px; line-height: 1; color: var(--text-strong); }}
    @keyframes enter {{
      from {{ opacity: 0; transform: translateY(4px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    @media (max-width: 980px) {{
      .app-shell {{ grid-template-columns: 1fr; }}
      .sidebar {{ position: static; min-height: auto; border-right: 0; border-bottom: 1px solid var(--border); }}
      .nav {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .workspace {{ padding: 18px; }}
      .topbar, .grid, .toolbar, .ask-grid, .regression-grid, .onboarding-grid, .metrics, .split-rail {{ grid-template-columns: 1fr; }}
      .onboarding-grid .wide {{ grid-column: 1 / -1; }}
      .topline {{ justify-content: flex-start; }}
    }}
    @media (max-width: 620px) {{
      .workspace {{ padding: 14px; }}
      .sidebar {{ padding: 14px; }}
      .nav {{ grid-template-columns: 1fr; }}
      .page-title {{ font-size: 22px; }}
      th, td {{ font-size: 12px; }}
    }}
  </style>
</head>
<body>
  <div class="app-shell" data-screen-label="voicevault-workbench">
    <aside class="sidebar">
      <section class="brand-panel" aria-label="产品">
        <span class="brand-mark">V</span>
        <div class="brand-title">
          <h1>声迹 VoiceVault</h1>
          <p>本地公开观点库</p>
        </div>
      </section>
      <nav class="nav" aria-label="视图">
        <button type="button" data-view="overview" class="active"><span class="nav-index">01</span><span>总览</span></button>
        <button type="button" data-view="actions"><span class="nav-index">02</span><span>行动</span></button>
        <button type="button" data-view="analysis"><span class="nav-index">03</span><span>分析</span></button>
        <button type="button" data-view="answers"><span class="nav-index">04</span><span>问答</span></button>
        <button type="button" data-view="statements"><span class="nav-index">05</span><span>语料</span></button>
        <button type="button" data-view="events"><span class="nav-index">06</span><span>事件</span></button>
        <button type="button" data-view="capture"><span class="nav-index">07</span><span>采集</span></button>
      </nav>
      <section class="sidebar-footer">
        <span>知识库</span>
        <code class="path-chip">{escape(str(data["knowledge_base"]))}</code>
      </section>
    </aside>
    <main class="workspace">
      <header class="topbar">
        <div>
          <p class="eyebrow">声迹工作台</p>
          <h1 class="page-title">公开观点归档与角色问答</h1>
          <p class="page-subtitle">{escape(str(data["knowledge_base"]))}</p>
        </div>
        <div class="topline">
          <span id="readyBadge" class="badge">发布检查</span>
          <span class="badge">v{escape(str(data["product"]["version"]))}</span>
          <a class="badge" href="data.json">data.json</a>
        </div>
      </header>
      <section class="metrics" id="metrics" aria-label="工作台指标"></section>
      <section id="overview" class="view grid" data-screen-label="overview"></section>
      <section id="actions" class="view panel hidden" data-screen-label="actions">
        <div class="view-header">
          <div>
            <h2>发布行动</h2>
            <p>查看研究队列、修复项、行动历史和发布阻塞项。</p>
          </div>
        </div>
        <div id="actionList" class="section-stack"></div>
      </section>
      <section id="analysis" class="view panel hidden" data-screen-label="analysis">
        <div class="view-header">
          <div>
            <h2>事件分析</h2>
            <p>按事件查看角色结论和证据摘要。</p>
          </div>
        </div>
        <div id="analysisList" class="list"></div>
      </section>
      <section id="statements" class="view panel hidden" data-screen-label="statements">
        <div class="view-header">
          <div>
            <h2>公开语料</h2>
            <p>搜索本地索引中的公开发言。</p>
          </div>
        </div>
      <div class="toolbar">
        <input id="query" type="search" placeholder="搜索公开发言">
        <select id="roleFilter" aria-label="角色筛选"></select>
        <select id="platformFilter" aria-label="平台筛选"></select>
      </div>
      <div id="statementList" class="list"></div>
    </section>
      <section id="answers" class="view panel hidden" data-screen-label="answers">
        <div class="view-header">
          <div>
            <h2>证据问答</h2>
            <p>基于本地证据回答、比较角色，或生成角色代理提示词。</p>
          </div>
        </div>
      <form id="answerForm" class="ask-form">
        <div class="ask-grid">
          <input id="answerQuery" type="search" placeholder="问题" required>
          <select id="answerRole" aria-label="回答角色"></select>
          <input id="answerSymbol" type="text" placeholder="标的代码">
          <input id="answerTopic" type="text" placeholder="主题">
          <input id="answerLimit" type="number" min="1" max="20" value="5" aria-label="证据数量">
          <button id="answerSubmit" type="submit">生成回答</button>
          <button id="answerCompare" type="button">比较角色</button>
        </div>
        <div id="answerError" class="answer-status" aria-live="polite"></div>
      </form>
      <section class="panel">
        <h2>角色代理</h2>
        <form id="roleAgentForm" class="ask-form">
          <div class="ask-grid">
            <input id="roleAgentQuery" type="search" placeholder="角色代理问题" required>
            <select id="roleAgentRole" aria-label="角色代理身份"></select>
            <input id="roleAgentSymbol" type="text" placeholder="标的代码">
            <input id="roleAgentTopic" type="text" placeholder="主题">
            <input id="roleAgentLimit" type="number" min="1" max="20" value="5" aria-label="证据数量">
            <label class="check"><input id="roleAgentCallLlm" type="checkbox"> 调用 LLM</label>
            <input id="roleAgentModel" type="text" placeholder="模型">
            <input id="roleAgentTemperature" type="number" min="0" max="2" step="0.1" value="0.2" aria-label="温度">
            <button id="roleAgentDistill" type="button">蒸馏技能</button>
            <button id="roleAgentAsk" type="submit">生成代理</button>
          </div>
        </form>
        <div id="roleAgentResult" class="list"></div>
      </section>
      <div id="liveAnswer" class="list"></div>
      <div id="answerList" class="list"></div>
      <div id="comparisonList" class="list"></div>
    </section>
      <section id="events" class="view grid hidden" data-screen-label="events"></section>
      <section id="capture" class="view grid hidden" data-screen-label="capture"></section>
    </main>
  </div>
  <script id="voicevault-data" type="application/json">{embedded}</script>
  <script>
    const data = JSON.parse(document.getElementById('voicevault-data').textContent);
    const state = {{ query: '', role: 'all', platform: 'all', actionRunStatus: 'all', answerLoading: false, onboardingLoading: false }};
    const byId = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
    const metric = (label, value, tone = '') => `<article class="metric" data-tone="${{esc(tone)}}"><span>${{esc(label)}}</span><strong>${{esc(value)}}</strong></article>`;
    const pills = (items) => (items || []).map((item) => `<span class="pill">${{esc(item)}}</span>`).join('');
    const sourceLink = (value) => value ? `<a href="${{esc(value)}}">来源</a>` : '';
    const uiText = (value) => {{
      const raw = String(value ?? '');
      const normalized = raw.toLowerCase();
      const labels = {{
        ok: '正常',
        ready: '就绪',
        active: '启用',
        completed: '已完成',
        complete: '已完成',
        pass: '通过',
        passed: '通过',
        deliverable: '可交付',
        adopted: '已采纳',
        reviewed: '已审阅',
        failed: '失败',
        fail: '失败',
        error: '错误',
        invalid: '无效',
        blocked: '已阻塞',
        rejected: '已拒绝',
        warn: '警告',
        warning: '警告',
        review: '待审阅',
        missing: '缺少',
        draft: '草稿',
        pending: '待处理',
        no_evidence: '无证据',
        all: '全部',
        unknown: '未知',
        unclear: '不明确',
        bullish: '看多',
        bearish: '看空',
        mixed: '分歧',
        low: '低',
        medium: '中',
        high: '高',
        short_term: '短期',
        medium_term: '中期',
        long_term: '长期',
        prompt_only: '仅提示词',
        local_api: '本地 API',
        dry_run: '试运行',
        rss: 'RSS',
        'local-export': '本地导出',
        'custom-api': '自定义 API',
        xueqiu: '雪球',
        weibo: '微博',
        wechat: '微信公众号',
        auto: '自动',
        evidence_backed: '有证据',
        single_role: '单角色',
        manual: '手动',
        'local-jsonl': '本地 JSONL',
        local_jsonl: '本地 JSONL',
        disabled: '已停用',
        malformed: '配置异常',
        blog: '博客',
        snowball: '雪球',
        prompt_ready: '提示词就绪',
        live_ready: '实时回答就绪',
        missing_prompt: '缺少提示词',
        missing_live: '缺少实时回答',
        blocked_runtime: '运行时阻塞',
        needs_attention: '需关注',
        research: '研究',
        capture: '采集',
        release: '发布',
        setup: '配置',
        content: '内容',
        publish: '发布',
        answer: '回答',
        compare: '比较',
        review_comparison: '审阅比较',
        comparison_review: '比较审阅',
        role_agent: '角色代理',
        fix: '修复',
        retry_action_run: '重试行动运行',
        rerun_answer: '重跑回答',
        inspect_answer: '检查回答',
        improve_answer: '改进回答',
        fix_answer_regression: '修复回答回归',
        inspect_comparison: '检查比较',
        retry_source_job: '重试来源任务',
        not_called: '未调用',
      }};
      return labels[normalized] || raw;
    }};
    const messageText = (value) => {{
      const raw = String(value ?? '');
      const normalized = raw.trim();
      const exact = {{
        'Adapter config is ready.': '适配器配置已就绪。',
        'Source is disabled; adapter config was not checked.': '来源已停用，未检查适配器配置。',
        'Source config is malformed.': '来源配置格式异常。',
        'Manual adapter is ready; provide --text when running the source.': '手动适配器已就绪；运行来源时请提供 --text。',
        'Source adapter_config must be a JSON object.': '来源 adapter_config 必须是 JSON 对象。',
        'local-jsonl adapter requires adapter_config.input_path.': '本地 JSONL 适配器需要 adapter_config.input_path。',
        'Ask evidence answer': '生成证据回答',
        'Compare roles': '比较角色',
        'Run source': '运行来源',
        'Compare relevant roles on the new evidence question.': '围绕新证据问题比较相关角色观点。',
        'Review comparison': '审阅角色比较',
        'Adopt comparison': '采纳角色比较',
        'Fix role profile': '修复角色档案',
        'Index role statements': '索引角色发言',
        'A public statement was captured; verify the new evidence can answer a concrete question.': '已采集公开发言，需要验证新证据能回答一个具体问题。',
        'Role coverage is sufficient; compare viewpoints before adopting the result for release handoff.': '角色覆盖已满足要求，发布交接前需要比较不同观点。',
        'A deliverable answer export exists for this query, so Ask evidence answer is complete.': '该问题已有可交付回答导出，证据回答行动已完成。',
        'A deliverable comparison export exists for this query, so Compare roles is complete.': '该问题已有可交付角色比较导出，角色比较行动已完成。',
        'Draft comparisons are not release-ready until reviewed.': '草稿状态的角色比较需要审阅后才能用于发布交接。',
        'This role has evidence but is not reviewed, so it cannot satisfy role coverage.': '该角色已有证据但尚未审阅，暂不能满足角色覆盖要求。',
        'Reviewed roles need indexed statements before they can participate in release coverage.': '已审阅角色需要有索引发言，才能参与发布覆盖检查。',
        'Active sources should have a recorded run before release.': '活跃来源在发布前需要有运行记录。',
        'Latest source run failed.': '最近一次来源运行失败。',
        'Answer regression suite is valid JSON with schema_version 1.': '回答回归套件是有效的 schema_version 1 JSON。',
        'Answer is deliverable, but missing structured role_answer for role-specific UI use.': '回答可交付，但缺少用于角色化 UI 的结构化 role_answer。',
        'Fixed answer regression question is failing and should be repaired before handoff.': '固定回答回归问题未通过，交接前需要修复。',
        'Comparison export is not adopted and may block release quality.': '角色比较导出尚未采纳，可能影响发布质量。',
        'Source job failed.': '来源任务失败。',
      }};
      if (exact[normalized]) return exact[normalized];
      let match = normalized.match(/^local-jsonl adapter is ready with (\d+) record\(s\)\.$/);
      if (match) return '本地 JSONL 适配器已就绪，包含 ' + match[1] + ' 条记录。';
      match = normalized.match(/^(\d+) fixed answer regression question\(s\); requires (\d+)\.$/);
      if (match) return '固定回答回归问题 ' + match[1] + ' 个，需要 ' + match[2] + ' 个。';
      match = normalized.match(/^(\d+) passed, (\d+) review, (\d+) failed\.$/);
      if (match) return match[1] + ' 个通过，' + match[2] + ' 个待审阅，' + match[3] + ' 个失败。';
      match = normalized.match(/^(\d+) fixed question\(s\) missing source URL, rationale, owner, or timestamps\.$/);
      if (match) return match[1] + ' 个固定问题缺少来源 URL、依据、负责人或时间戳。';
      match = normalized.match(/^Run source (.+) and sync captured evidence\.$/);
      if (match) return '运行来源 ' + match[1] + ' 并同步已采集证据。';
      match = normalized.match(/^Ask an evidence-backed question for (.+)\.$/);
      if (match) return '为 ' + match[1] + ' 生成一个有证据支撑的问题回答。';
      match = normalized.match(/^Retry failed (.+) run\.$/);
      if (match) return '重试失败的 ' + uiText(match[1]) + ' 运行。';
      match = normalized.match(/^Answer export status is (.+), so it is not release quality\.$/);
      if (match) return '回答导出状态为' + uiText(match[1]) + '，尚未达到发布质量。';
      match = normalized.match(/^Answer export status is (.+) and query is missing\.$/);
      if (match) return '回答导出状态为' + uiText(match[1]) + '，且缺少问题。';
      match = normalized.match(/^(\d+) top evidence item\(s\), (\d+) total indexed match\(es\)(.*)\.$/);
      if (match) {{
        const suffix = match[3]
          .replace(/^ with /, '，筛选条件：')
          .replace(/symbol /g, '标的 ')
          .replace(/topic /g, '主题 ')
          .replace(/ and /g, ' 和 ');
        return match[1] + ' 条最相关证据，' + match[2] + ' 条索引命中' + suffix + '。';
      }}
      match = normalized.match(/^Unsupported source adapter: (.+)$/);
      if (match) return '不支持的来源适配器：' + match[1];
      match = normalized.match(/^Adapter input has no records: (.+)$/);
      if (match) return '适配器输入没有记录：' + match[1];
      match = normalized.match(/^Adapter input not found: (.+)$/);
      if (match) return '找不到适配器输入：' + match[1];
      match = normalized.match(/^Invalid adapter JSONL record at (.+)$/);
      if (match) return '适配器 JSONL 记录无效：' + match[1];
      match = normalized.match(/^Adapter JSONL record must be an object at (.+)$/);
      if (match) return '适配器 JSONL 记录必须是对象：' + match[1];
      match = normalized.match(/^Adapter JSON list must contain objects: (.+)$/);
      if (match) return '适配器 JSON 列表必须包含对象：' + match[1];
      match = normalized.match(/^Adapter JSON records must contain objects: (.+)$/);
      if (match) return '适配器 JSON records 必须包含对象：' + match[1];
      match = normalized.match(/^Adapter JSON items must contain objects: (.+)$/);
      if (match) return '适配器 JSON items 必须包含对象：' + match[1];
      match = normalized.match(/^Adapter JSON must be an object, object list, records object, or items object: (.+)$/);
      if (match) return '适配器 JSON 必须是对象、对象列表、records 对象或 items 对象：' + match[1];
      return raw;
    }};
    const statusPill = (label, tone = '') => `<span class="status-pill ${{esc(tone)}}">${{esc(uiText(label))}}</span>`;
    const boolTone = (ok) => ok ? 'ok' : 'warn';
    const healthTone = (value) => {{
      const normalized = String(value || '').toLowerCase();
      if (['ok', 'ready', 'active', 'completed', 'complete', 'pass', 'passed', 'deliverable', 'adopted', 'reviewed'].includes(normalized)) return 'ok';
      if (['failed', 'fail', 'error', 'invalid', 'blocked', 'rejected'].includes(normalized)) return 'bad';
      if (['warn', 'warning', 'review', 'missing', 'draft', 'pending', 'no_evidence'].includes(normalized)) return 'warn';
      return 'accent';
    }};

    function init() {{
      byId('readyBadge').className = `badge ${{data.summary.release_ready ? 'ok' : 'warn'}}`;
      byId('readyBadge').textContent = data.summary.release_ready ? '发布就绪' : '发布检查';
      renderMetrics();
      renderOverview();
      renderActions();
      renderAnalysis();
      renderAnswers();
      renderEvents();
      renderCapture();
      hydrateFilters();
      renderStatements();
      document.querySelectorAll('[data-view]').forEach((button) => {{
        button.addEventListener('click', () => showView(button.dataset.view));
      }});
      byId('query').addEventListener('input', (event) => {{ state.query = event.target.value.toLowerCase(); renderStatements(); }});
      byId('roleFilter').addEventListener('change', (event) => {{ state.role = event.target.value; renderStatements(); }});
      byId('platformFilter').addEventListener('change', (event) => {{ state.platform = event.target.value; renderStatements(); }});
      byId('answerForm').addEventListener('submit', submitAnswer);
      byId('answerCompare').addEventListener('click', submitComparison);
      byId('roleAgentForm').addEventListener('submit', submitRoleAgent);
      byId('roleAgentDistill').addEventListener('click', distillSelectedRoleSkill);
      document.addEventListener('click', handleComparisonReviewClick);
      document.addEventListener('click', handleNextActionClick);
      document.addEventListener('click', handleRemediationClick);
      document.addEventListener('click', handleActionRunRetryClick);
      document.addEventListener('click', handleAnswerRegressionClick);
      document.addEventListener('click', handleAnswerRegressionDeleteClick);
      document.addEventListener('click', handleAnswerRegressionBatchClick);
      document.addEventListener('submit', handleAnswerRegressionSubmit);
      document.addEventListener('change', handleActionRunFilterChange);
    }}

    function showView(view) {{
      document.querySelectorAll('[data-view]').forEach((button) => button.classList.toggle('active', button.dataset.view === view));
      document.querySelectorAll('.view').forEach((section) => section.classList.toggle('hidden', section.id !== view));
      const titles = {{
        overview: ['公开观点归档与角色问答', '查看角色覆盖、证据状态和近期结果。'],
        actions: ['行动队列与发布状态', '查看研究队列、修复项、行动历史和发布阻塞项。'],
        analysis: ['事件分析', '按事件查看角色结论和证据摘要。'],
        answers: ['证据问答与角色代理', '基于本地证据回答、比较角色，或生成角色代理提示词。'],
        statements: ['公开语料库', '搜索本地索引中的公开发言。'],
        events: ['事件与发布检查', '查看事件文件和发布门禁。'],
        capture: ['采集与账户归档', '配置公开账户归档、来源接入和采集状态。'],
      }};
      const selected = titles[view] || titles.overview;
      const title = document.querySelector('.page-title');
      const subtitle = document.querySelector('.page-subtitle');
      if (title) title.textContent = selected[0];
      if (subtitle) subtitle.textContent = selected[1];
    }}

    function renderMetrics() {{
      byId('metrics').innerHTML = [
        metric('角色', data.summary.roles, 'accent'),
        metric('角色覆盖', `${{data.summary.reviewed_roles_with_statements || 0}}/${{data.summary.min_reviewed_roles || 0}}`, data.summary.reviewed_roles_with_statements >= data.summary.min_reviewed_roles ? 'ok' : 'warn'),
        metric('发言', data.summary.statements, data.summary.statements ? 'ok' : 'warn'),
        metric('回答', `${{data.summary.deliverable_answer_exports || 0}}/${{data.summary.answer_exports || 0}}`, data.summary.deliverable_answer_exports ? 'ok' : 'warn'),
        metric('角色代理', `${{data.summary.role_agent_deliverable || 0}}/${{data.summary.role_agent_exports || 0}}`, data.summary.role_agent_runtime_configured ? 'ok' : 'warn'),
        metric('账户归档', `${{data.summary.account_archives || 0}}/${{data.summary.account_archives_blocked || 0}} 阻塞`, data.summary.account_archives_blocked ? 'warn' : 'accent'),
        metric('来源', data.summary.source_configs, data.summary.source_adapter_failed ? 'bad' : 'accent'),
        metric('运行', `${{data.summary.source_runs || 0}}/${{data.summary.action_runs || 0}}`, data.summary.action_run_failed ? 'warn' : 'accent'),
        metric('回归测试', `${{data.summary.answer_regression_passed || 0}}/${{((data.answer_regression || {{}}).summary || {{}}).total || 0}}`, data.summary.answer_regression_failed ? 'bad' : 'accent'),
        metric('角色比较', `${{data.summary.adopted_comparison_exports || 0}}/${{data.summary.comparison_exports || 0}}`, data.summary.adopted_comparison_exports ? 'ok' : 'accent'),
        metric('后续行动', data.summary.next_actions || 0, data.summary.next_actions ? 'warn' : 'accent'),
        metric('阻塞项', data.summary.release_blockers, data.summary.release_blockers ? 'bad' : 'ok'),
      ].join('');
    }}

    function renderActions() {{
      const nextActionRows = (data.next_actions || []).map((item) => `
        <tr>
          <td>${{statusPill(item.status, healthTone(item.status))}}</td>
          <td>${{esc(uiText(item.phase))}}</td>
          <td>${{esc(messageText(item.label))}}</td>
          <td>${{esc(messageText(item.action))}}</td>
          <td>${{esc(messageText(item.reason || (item.audit && item.audit.reason) || ''))}}</td>
          <td>${{nextActionButton(item)}}</td>
        </tr>
      `).join('');
      const completedActionRows = (((data.next_action_audit || {{}}).completed_actions) || []).map((item) => `
        <tr>
          <td>${{statusPill(item.status, healthTone(item.status))}}</td>
          <td>${{esc(uiText(item.phase))}}</td>
          <td>${{esc(messageText(item.label))}}</td>
          <td>${{esc(messageText(item.reason || ''))}}</td>
          <td><code>${{esc((item.completed_by || {{}}).kind || '')}}</code></td>
          <td><code class="command">${{esc((item.completed_by || {{}}).path || '')}}</code></td>
        </tr>
      `).join('');
      const remediationRows = (((data.remediation_queue || {{}}).items) || []).slice(0, 12).map((item) => `
        <tr>
          <td>${{statusPill(item.status, healthTone(item.status))}}</td>
          <td>${{esc(uiText(item.severity))}}</td>
          <td>${{esc(uiText(item.phase))}}</td>
          <td>${{esc(messageText(item.label))}}</td>
          <td>${{esc(messageText(item.reason || item.action || ''))}}</td>
          <td>${{remediationActionControl(item)}}</td>
        </tr>
      `).join('');
      const releaseActionRows = (data.release_actions || []).map((item) => `
        <tr>
          <td>${{statusPill(item.status, healthTone(item.status))}}</td>
          <td>${{esc(uiText(item.phase))}}</td>
          <td><code>${{esc(item.check_id)}}</code></td>
          <td>${{esc(messageText(item.action))}}</td>
          <td><code class="command">${{esc(item.command)}}</code></td>
        </tr>
      `).join('');
      const actionRunSummary = (data.action_runs || {{}}).summary || {{}};
      const visibleActionRuns = (((data.action_runs || {{}}).runs) || [])
        .filter((run) => state.actionRunStatus === 'all' || run.status === state.actionRunStatus)
        .slice(0, 12);
      const actionRunRows = visibleActionRuns.map((run) => {{
        const payload = run.payload || {{}};
        const result = run.result || {{}};
        const resultText = run.status === 'failed'
          ? (run.resolved_by ? `由 ${{run.resolved_by}} 处理` : (run.error || '失败'))
          : (result.artifact_path || result.review_status || '');
        const retryButton = run.status === 'failed' && run.retryable
          ? `<button type="button" data-action-run-retry="${{esc(run.run_id)}}">重试</button>`
          : (run.resolved_by ? '已处理' : '');
        return `
          <tr>
            <td>${{statusPill(run.status, healthTone(run.status))}}</td>
            <td>${{esc(uiText(run.action_type))}}</td>
            <td>${{esc(uiText(run.source))}}</td>
            <td>${{esc(payload.query || '')}}</td>
            <td>${{esc(run.completed_at || '')}}</td>
            <td><code class="command">${{esc(resultText)}}</code></td>
            <td>${{retryButton}}</td>
          </tr>
        `;
      }}).join('');
      byId('actionList').innerHTML = `
        <div class="summary-grid">
          <article class="summary-tile"><span>研究行动</span><strong>${{esc((data.next_actions || []).length)}}</strong></article>
          <article class="summary-tile"><span>修复队列</span><strong>${{esc((data.remediation_queue.summary || {{}}).ready || 0)}}/${{esc((data.remediation_queue.summary || {{}}).total || 0)}}</strong></article>
          <article class="summary-tile"><span>行动历史</span><strong>${{esc(actionRunSummary.total || 0)}}</strong></article>
          <article class="summary-tile"><span>发布阻塞</span><strong>${{esc(data.summary.release_blockers || 0)}}</strong></article>
        </div>
        <section class="panel-stack">
          <h2>研究行动</h2>
          <div class="action-table-scroll"><table><thead><tr><th>状态</th><th>阶段</th><th>名称</th><th>行动</th><th>原因</th><th>执行</th></tr></thead><tbody>${{nextActionRows || '<tr><td colspan="6">暂无研究行动。</td></tr>'}}</tbody></table></div>
        </section>
        <section class="panel-stack">
          <h2>已完成研究行动</h2>
          <div class="action-table-scroll"><table><thead><tr><th>状态</th><th>阶段</th><th>名称</th><th>完成原因</th><th>完成人</th><th>产物</th></tr></thead><tbody>${{completedActionRows || '<tr><td colspan="6">暂无已完成研究行动。</td></tr>'}}</tbody></table></div>
        </section>
        <section class="panel-stack">
          <h2>修复队列</h2>
          <div class="action-table-scroll"><table><thead><tr><th>状态</th><th>严重性</th><th>阶段</th><th>名称</th><th>原因</th><th>执行</th></tr></thead><tbody>${{remediationRows || '<tr><td colspan="6">暂无修复项。</td></tr>'}}</tbody></table></div>
        </section>
        <section class="panel-stack">
          <div class="view-header">
            <div>
              <h2>行动历史</h2>
              <p>${{esc(actionRunSummary.failed || 0)}} 失败 · ${{esc(actionRunSummary.retryable_failed || 0)}} 可重试</p>
            </div>
            <select id="actionRunFilter" aria-label="行动状态筛选">
              <option value="all" data-action-run-status="all" ${{state.actionRunStatus === 'all' ? 'selected' : ''}}>全部记录</option>
              <option value="failed" data-action-run-status="failed" ${{state.actionRunStatus === 'failed' ? 'selected' : ''}}>仅失败</option>
              <option value="completed" data-action-run-status="completed" ${{state.actionRunStatus === 'completed' ? 'selected' : ''}}>仅已完成</option>
            </select>
          </div>
          <div class="action-table-scroll"><table><thead><tr><th>状态</th><th>类型</th><th>来源</th><th>问题</th><th>完成时间</th><th>结果</th><th>重试</th></tr></thead><tbody>${{actionRunRows || '<tr><td colspan="7">当前筛选条件下暂无行动记录。</td></tr>'}}</tbody></table></div>
        </section>
        <section class="panel-stack">
          <h2>发布行动</h2>
          <div class="action-table-scroll"><table><thead><tr><th>状态</th><th>阶段</th><th>检查项</th><th>行动</th><th>命令</th></tr></thead><tbody>${{releaseActionRows || '<tr><td colspan="5">暂无发布行动。</td></tr>'}}</tbody></table></div>
        </section>
      `;
    }}

    function nextActionButton(item) {{
      if (item.endpoint) {{
        return `<button type="button" data-next-action="${{esc(item.id)}}">${{esc(messageText(item.label || '执行'))}}</button>`;
      }}
      return `<code class="command">${{esc(item.command || '')}}</code>`;
    }}

    function remediationActionControl(item) {{
      if (item.endpoint && item.status === 'ready') {{
        return `<button type="button" data-remediation-item="${{esc(item.id)}}">${{esc(messageText(item.label || '执行'))}}</button>`;
      }}
      return `<code class="command">${{esc(item.command || '')}}</code>`;
    }}

    function renderOverview() {{
      const roleRows = data.roles.slice(0, 12).map((role) => `<tr><td>${{esc(role.display_name || role.role_id)}}</td><td><code>${{esc(role.role_id)}}</code></td><td>${{statusPill(role.profile_status, healthTone(role.profile_status))}}</td><td>${{esc(role.statement_count)}}</td></tr>`).join('');
      const coverageRows = (data.role_coverage.roles || []).slice(0, 12).map((role) => `<tr><td><code>${{esc(role.role_id)}}</code></td><td>${{statusPill(role.coverage_status, healthTone(role.coverage_status))}}</td><td>${{esc(uiText(role.profile_status))}}</td><td>${{esc(role.statement_count)}}</td></tr>`).join('');
      const reportRows = data.reports.slice(0, 8).map((report) => `<tr><td>${{esc(report.generated_at || '未知')}}</td><td>${{esc(report.title)}}</td><td>${{esc(report.matches)}}</td></tr>`).join('');
      const answerRows = data.answer_exports.slice(0, 8).map((answer) => `<tr><td>${{esc(answer.generated_at || '未知')}}</td><td>${{esc(answer.query)}}</td><td>${{esc(answer.evidence_count)}}</td><td>${{statusPill(answer.status || (answer.evidence_backed ? 'evidence_backed' : 'invalid'), healthTone(answer.status || (answer.evidence_backed ? 'evidence_backed' : 'invalid')))}}</td><td><code>${{esc(uiText(answer.confidence))}}</code></td></tr>`).join('');
      byId('overview').innerHTML = `
        <section class="panel full">
          <div class="summary-grid">
            <article class="summary-tile"><span>已审阅角色</span><strong>${{esc(data.summary.reviewed_roles || 0)}}</strong></article>
            <article class="summary-tile"><span>发言</span><strong>${{esc(data.summary.statements || 0)}}</strong></article>
            <article class="summary-tile"><span>账户归档</span><strong>${{esc(data.summary.account_archives || 0)}}</strong></article>
            <article class="summary-tile"><span>发布阻塞</span><strong>${{esc(data.summary.release_blockers || 0)}}</strong></article>
          </div>
        </section>
        <section class="panel"><div class="view-header"><div><h2>角色</h2><p>查看角色档案状态和已索引发言数。</p></div></div><div class="action-table-scroll"><table><thead><tr><th>名称</th><th>角色</th><th>档案</th><th>发言</th></tr></thead><tbody>${{roleRows || '<tr><td colspan="4">暂无角色。</td></tr>'}}</tbody></table></div></section>
        <section class="panel"><div class="view-header"><div><h2>角色覆盖</h2><p>${{esc(data.role_coverage.reviewed_roles_with_statements)}} / ${{esc(data.role_coverage.min_reviewed_roles)}} 个已审阅且有发言的角色</p></div></div><div class="action-table-scroll"><table><thead><tr><th>角色</th><th>覆盖状态</th><th>档案</th><th>发言</th></tr></thead><tbody>${{coverageRows || '<tr><td colspan="4">暂无角色。</td></tr>'}}</tbody></table></div></section>
        <section class="panel"><div class="view-header"><div><h2>报告</h2><p>查看证据包和生成的研究报告。</p></div></div><div class="action-table-scroll"><table><thead><tr><th>生成时间</th><th>标题</th><th>命中</th></tr></thead><tbody>${{reportRows || '<tr><td colspan="3">暂无报告。</td></tr>'}}</tbody></table></div></section>
        <section class="panel"><div class="view-header"><div><h2>证据回答</h2><p>查看近期本地证据回答。</p></div></div><div class="action-table-scroll"><table><thead><tr><th>生成时间</th><th>问题</th><th>证据</th><th>状态</th><th>置信度</th></tr></thead><tbody>${{answerRows || '<tr><td colspan="5">暂无回答导出。</td></tr>'}}</tbody></table></div></section>
      `;
    }}

    function renderAnalysis() {{
      const analyses = data.analysis_exports || [];
      byId('analysisList').innerHTML = analyses.length ? analyses.map((item) => {{
        const analysisRoleRows = (item.role_summaries || []).map((role) => `<tr><td><code>${{esc(role.role_id)}}</code></td><td>${{esc(uiText(role.stance))}}</td><td>${{esc(uiText(role.confidence))}}</td><td>${{esc(role.evidence_count || 0)}}</td><td>${{esc(role.conclusion || '')}}</td></tr>`).join('');
        const analysisEvidenceRows = (item.evidence_summaries || []).map((evidence) => {{
          const sourceLabel = evidence.title || evidence.source_url || evidence.statement_id;
          const sourceCell = evidence.source_url ? `<a href="${{esc(evidence.source_url)}}">${{esc(sourceLabel)}}</a>` : esc(sourceLabel);
          return `<tr><td><code>${{esc(evidence.statement_id)}}</code></td><td>${{esc(evidence.role_id)}}</td><td>${{sourceCell}}<p>${{esc(evidence.excerpt || '')}}</p></td><td>${{esc(uiText(evidence.stance || ''))}}</td><td>${{esc(evidence.published_at || evidence.captured_at || '')}}</td></tr>`;
        }}).join('');
        return `
          <article class="statement">
            <h3>${{esc(item.title || item.event_id)}}</h3>
            <div class="meta"><span>${{esc(item.date || '未知')}}</span><span>${{esc(item.role_count)}} 个角色</span><span>${{esc(item.evidence_count)}} 条证据</span>${{pills(item.symbols)}}${{pills(item.topics)}}</div>
            <p>${{esc(item.synthesis_markdown || item.error || '暂无综合结论。')}}</p>
            <div class="action-table-scroll"><table><thead><tr><th>角色</th><th>立场</th><th>置信度</th><th>证据</th><th>结论</th></tr></thead><tbody>${{analysisRoleRows || '<tr><td colspan="5">暂无角色分析。</td></tr>'}}</tbody></table></div>
            <h3>证据</h3>
            <div class="action-table-scroll"><table><thead><tr><th>发言</th><th>角色</th><th>来源</th><th>立场</th><th>日期</th></tr></thead><tbody>${{analysisEvidenceRows || '<tr><td colspan="5">暂无支撑证据。</td></tr>'}}</tbody></table></div>
            <div class="meta"><a href="${{esc(item.analysis_json)}}">analysis.json</a><a href="${{esc(item.analysis_markdown)}}">analysis.md</a></div>
          </article>
        `;
      }}).join('') : '<div class="empty">暂无分析导出。可运行 voicevault analyze --kb &lt;path&gt; --event &lt;event.md&gt;。</div>';
    }}

    function hydrateFilters() {{
      const roles = ['all', ...new Set(data.statements.map((item) => item.role_id).filter(Boolean))];
      const answerRoles = ['__auto__', 'all', ...new Set(data.roles.map((item) => item.role_id).filter(Boolean))];
      const platforms = ['all', ...new Set(data.statements.map((item) => item.source_platform || 'unknown'))];
      byId('roleFilter').innerHTML = roles.map((role) => `<option value="${{esc(role)}}">${{esc(role === 'all' ? '全部角色' : role)}}</option>`).join('');
      byId('answerRole').innerHTML = answerRoles.map((role) => `<option value="${{esc(role)}}">${{esc(role === '__auto__' ? '自动选择角色' : (role === 'all' ? '全部角色' : role))}}</option>`).join('');
      byId('roleAgentRole').innerHTML = answerRoles.filter((role) => role !== 'all' && role !== '__auto__').map((role) => `<option value="${{esc(role)}}">${{esc(role)}}</option>`).join('');
      byId('platformFilter').innerHTML = platforms.map((platform) => `<option value="${{esc(platform)}}">${{esc(platform === 'all' ? '全部平台' : uiText(platform))}}</option>`).join('');
    }}

    function answerRegressionRoleOptions(selected = '', includeAuto = true) {{
      const roleIds = [...new Set((data.roles || []).map((item) => item.role_id).filter(Boolean))];
      const options = includeAuto ? [['', '自动选择角色']] : [['', '预期角色']];
      roleIds.forEach((roleId) => options.push([roleId, roleId]));
      return options.map(([value, label]) => `<option value="${{esc(value)}}" ${{value === selected ? 'selected' : ''}}>${{esc(label)}}</option>`).join('');
    }}

    function renderStatements() {{
      const query = state.query.trim();
      const matches = data.statements.filter((item) => {{
        const haystack = [item.title, item.body, item.role_id, item.source_platform, ...(item.symbols || []), ...(item.topics || [])].join(' ').toLowerCase();
        return (!query || haystack.includes(query)) &&
          (state.role === 'all' || item.role_id === state.role) &&
          (state.platform === 'all' || (item.source_platform || 'unknown') === state.platform);
      }});
      byId('statementList').innerHTML = matches.length ? matches.slice(0, 80).map((item) => `
        <article class="statement">
          <h3>${{esc(item.title || item.statement_id)}}</h3>
          <div class="meta"><span>${{esc(item.display_time)}}</span><span>${{statusPill(item.role_id, 'accent')}}</span><span>${{esc(uiText(item.source_platform || 'unknown'))}}</span>${{pills(item.symbols)}}${{pills(item.topics)}}</div>
          <p>${{esc(item.excerpt || item.body)}}</p>
        </article>
      `).join('') : '<div class="empty">暂无匹配发言。</div>';
    }}

    function renderAnswers() {{
      const answerQuality = data.answer_quality || {{}};
      const answerQualityRows = ((answerQuality.items || [])).slice(0, 12).map((item) => `
        <tr>
          <td>${{statusPill(item.status, healthTone(item.status))}}</td>
          <td>${{esc(item.score)}}</td>
          <td>${{esc(item.query)}}</td>
          <td>${{esc((item.failed_checks || []).map(messageText).join(', '))}}</td>
          <td><code class="command">${{esc(item.answer_json || '')}}</code></td>
        </tr>
      `).join('');
      const answerQualitySummary = answerQuality.summary || {{}};
      const answerQualityPanel = `
        <section class="panel">
          <div class="view-header"><div><h2>回答质量</h2><p>${{esc(answerQualitySummary.passed || 0)}} 通过 · ${{esc(answerQualitySummary.review || 0)}} 待审阅 · ${{esc(answerQualitySummary.failed || 0)}} 失败</p></div></div>
          <div class="action-table-scroll"><table><thead><tr><th>状态</th><th>分数</th><th>问题</th><th>未通过检查</th><th>导出</th></tr></thead><tbody>${{answerQualityRows || '<tr><td colspan="5">暂无回答质量记录。</td></tr>'}}</tbody></table></div>
        </section>
      `;
      const answerRegression = data.answer_regression || {{}};
      const answerRegressionRows = ((answerRegression.items || [])).slice(0, 12).map((item) => `
        <tr>
          <td>${{statusPill(item.status, healthTone(item.status))}}</td>
          <td>${{esc(item.score)}}</td>
          <td>${{esc(item.id)}}</td>
          <td>${{esc(item.query)}}</td>
          <td>${{esc(item.expected_role_id || item.role_id || '')}}</td>
          <td>${{sourceLink(item.source_url)}}</td>
          <td>${{esc(item.updated_by || '')}}<br>${{esc(item.updated_at || '')}}</td>
          <td>${{esc((item.failed_checks || []).map(messageText).join(', '))}}</td>
          <td>${{answerRegressionActionControl(item)}} <button type="button" data-answer-regression-delete="${{esc(item.id)}}">删除</button></td>
        </tr>
      `).join('');
      const answerRegressionChangeRows = ((answerRegression.recent_changes || [])).slice(-8).reverse().map((change) => `
        <tr>
          <td>${{esc(change.changed_at || '')}}</td>
          <td>${{esc(change.action || '')}}</td>
          <td><code>${{esc(change.question_id || '')}}</code></td>
          <td>${{esc(change.updated_by || '')}}</td>
        </tr>
      `).join('');
      const answerRegressionSummary = answerRegression.summary || {{}};
      const answerRegressionCoverageRows = ((answerRegression.checks || [])).map((check) => `
        <tr>
          <td>${{statusPill(check.ok ? 'ok' : 'fail', boolTone(check.ok))}}</td>
          <td><code>${{esc(check.id || '')}}</code></td>
          <td>${{esc(messageText(check.message || ''))}}</td>
          <td><code class="command">${{esc(messageText(check.remediation || ''))}}</code></td>
        </tr>
      `).join('');
      const answerRegressionPanel = `
        <section class="panel">
          <div class="view-header"><div><h2>回答回归测试</h2><p>${{esc(answerRegressionSummary.passed || 0)}} 通过 · ${{esc(answerRegressionSummary.review || 0)}} 待审阅 · ${{esc(answerRegressionSummary.failed || 0)}} 失败 · ${{esc(answerRegressionSummary.total || 0)}} / ${{esc(answerRegressionSummary.min_questions || 0)}} 覆盖</p></div></div>
          <h3>回归覆盖</h3>
          <div class="action-table-scroll"><table><thead><tr><th>状态</th><th>检查项</th><th>说明</th><th>修复建议</th></tr></thead><tbody>${{answerRegressionCoverageRows || '<tr><td colspan="4">暂无回答回归覆盖检查。</td></tr>'}}</tbody></table></div>
          <div class="ask-form">
            <div class="meta"><span>缺少来源：${{esc(answerRegressionSummary.missing_provenance || 0)}}</span><span>套件：${{esc(answerRegression.suite_path || '')}}</span></div>
            <textarea id="answerRegressionImportPayload" placeholder="粘贴回答回归套件 JSON，用于试运行或导入"></textarea>
            <div class="topline">
              <button id="answerRegressionExport" type="button">导出</button>
              <button type="button" data-answer-regression-import="dry-run">试运行导入</button>
              <button type="button" data-answer-regression-import="apply">导入</button>
            </div>
          </div>
          <form id="answerRegressionForm" class="ask-form">
            <div class="regression-grid">
              <input id="regressionQuery" type="search" placeholder="回归问题" required>
              <input id="regressionId" type="text" placeholder="可选 ID">
              <select id="regressionRole" aria-label="回归角色">${{answerRegressionRoleOptions('', true)}}</select>
              <input id="regressionSymbol" type="text" placeholder="标的代码">
              <input id="regressionTopic" type="text" placeholder="主题">
              <select id="regressionExpectedRole" aria-label="预期角色">${{answerRegressionRoleOptions('', false)}}</select>
              <input id="regressionMinEvidence" type="number" min="0" max="20" value="1" aria-label="最少证据数">
              <input id="regressionSourceUrl" type="url" placeholder="来源 URL">
              <input id="regressionRationale" type="text" placeholder="依据">
              <label class="check"><input id="regressionRequiresRoleAnswer" type="checkbox" checked> 角色回答</label>
              <button type="submit">保存</button>
            </div>
          </form>
          <div class="action-table-scroll"><table><thead><tr><th>状态</th><th>分数</th><th>ID</th><th>问题</th><th>角色</th><th>来源</th><th>更新时间</th><th>未通过检查</th><th>操作</th></tr></thead><tbody>${{answerRegressionRows || '<tr><td colspan="9">暂无固定回答回归问题。</td></tr>'}}</tbody></table></div>
          <h3>回答回归变更</h3>
          <div class="action-table-scroll"><table><thead><tr><th>变更时间</th><th>动作</th><th>问题</th><th>操作人</th></tr></thead><tbody>${{answerRegressionChangeRows || '<tr><td colspan="4">暂无回答回归变更。</td></tr>'}}</tbody></table></div>
        </section>
      `;
      const roleSkillRows = (((data.role_skills || {{}}).skills) || []).map((item) => `
        <tr>
          <td>${{statusPill(item.status, healthTone(item.status))}}</td>
          <td><code>${{esc(item.role_id)}}</code></td>
          <td>${{esc(item.source_statement_count || 0)}}</td>
          <td><code class="command">${{esc(item.skill_json || '')}}</code></td>
        </tr>
      `).join('');
      const roleAgentAudit = data.role_agent_audit || {{}};
      const roleAgentAuditSummary = roleAgentAudit.summary || {{}};
      const roleAgentReadiness = data.role_agent_readiness || {{}};
      const roleAgentReadinessSummary = roleAgentReadiness.summary || {{}};
      const roleAgentRuntime = data.role_agent_runtime || {{}};
      const roleAgentReadinessRows = ((roleAgentReadiness.roles || [])).map((item) => `
        <tr>
          <td>${{statusPill(item.status || '', healthTone(item.status || ''))}}</td>
          <td><code>${{esc(item.role_id || '')}}</code></td>
          <td>${{esc((item.counts || {{}}).prompt_ready || 0)}}</td>
          <td>${{esc((item.counts || {{}}).deliverable || 0)}}</td>
          <td>${{esc(item.suggested_query || '')}}</td>
          <td><code class="command">${{esc((item.remediation || []).join(' ; '))}}</code></td>
        </tr>
      `).join('');
      const roleAgentRows = ((roleAgentAudit.items || data.role_agent_exports || [])).slice(0, 8).map((item) => `
        <tr>
          <td>${{statusPill(item.quality_status || item.status, healthTone(item.quality_status || item.status))}}</td>
          <td><code>${{esc(item.role_id)}}</code></td>
          <td>${{esc(item.query)}}</td>
          <td>${{esc(uiText(item.llm_status))}}</td>
          <td>${{esc((item.failed_checks || []).map(messageText).join(', '))}}</td>
          <td><code class="command">${{esc(item.role_agent_json || '')}}</code></td>
        </tr>
      `).join('');
      const roleSkillsPanel = `
        <section class="panel">
          <div class="view-header"><div><h2>角色技能</h2><p>${{esc(data.summary.role_skills_ready || 0)}} 就绪 · ${{esc(data.summary.role_skills_missing || 0)}} 缺少</p></div></div>
          <div class="action-table-scroll"><table><thead><tr><th>状态</th><th>角色</th><th>发言</th><th>技能</th></tr></thead><tbody>${{roleSkillRows || '<tr><td colspan="4">暂无角色技能。</td></tr>'}}</tbody></table></div>
          <h3>角色代理就绪状态</h3>
          <p>${{esc(roleAgentReadinessSummary.roles_live_ready || 0)}} 个可实时回答角色 · ${{esc(roleAgentReadinessSummary.roles_prompt_ready || 0)}} 个提示词就绪角色 · ${{esc(roleAgentReadinessSummary.roles_missing_live || 0)}} 个缺少实时回答 · 运行时${{roleAgentRuntime.configured ? '已配置' : '未配置'}}</p>
          <div class="action-table-scroll"><table><thead><tr><th>状态</th><th>角色</th><th>提示词</th><th>实时回答</th><th>建议问题</th><th>下一条命令</th></tr></thead><tbody>${{roleAgentReadinessRows || '<tr><td colspan="6">暂无角色代理就绪记录。</td></tr>'}}</tbody></table></div>
          <h3>角色代理导出</h3>
          <p>${{esc(roleAgentAuditSummary.deliverable || 0)}} 可交付 · ${{esc(roleAgentAuditSummary.prompt_only || 0)}} 仅提示词 · ${{esc(roleAgentAuditSummary.failed || 0)}} 失败 · 运行时${{roleAgentRuntime.configured ? '已配置' : '未配置'}}</p>
          <div class="action-table-scroll"><table><thead><tr><th>质量</th><th>角色</th><th>问题</th><th>LLM</th><th>未通过检查</th><th>导出</th></tr></thead><tbody>${{roleAgentRows || '<tr><td colspan="6">暂无角色代理导出。</td></tr>'}}</tbody></table></div>
        </section>
      `;
      byId('answerList').innerHTML = data.answer_exports.length ? data.answer_exports.map((answer) => `
        <article class="statement">
          <h3>${{esc(answer.query || '未命名回答')}}</h3>
          <div class="meta"><span>${{esc(answer.generated_at || '未知')}}</span><span>${{esc(answer.evidence_count)}} 条证据</span>${{statusPill(answer.status || (answer.evidence_backed ? 'evidence_backed' : 'invalid'), healthTone(answer.status || (answer.evidence_backed ? 'evidence_backed' : 'invalid')))}}<span>${{esc(uiText(answer.confidence || 'unknown'))}}</span></div>
          <p>${{esc(answer.answer || '')}}</p>
          ${{roleAnswer(answer)}}
          ${{answerPoints(answer)}}
          <div class="meta"><a href="${{esc(answer.answer_json)}}">answer.json</a><a href="${{esc(answer.answer_markdown)}}">answer.md</a></div>
        </article>
      `).join('') : '<div class="empty">暂无回答导出。可运行 voicevault answer --kb &lt;path&gt; --query &lt;query&gt;。</div>';
      byId('answerList').innerHTML = roleSkillsPanel + answerRegressionPanel + answerQualityPanel + byId('answerList').innerHTML;
      byId('comparisonList').innerHTML = (data.comparison_exports || []).length ? (data.comparison_exports || []).map(comparisonCard).join('') : '<div class="empty">暂无角色比较导出。可运行 voicevault compare --kb &lt;path&gt; --query &lt;query&gt;。</div>';
    }}

    async function submitAnswer(event) {{
      event.preventDefault();
      if (state.answerLoading) return;
      const query = byId('answerQuery').value.trim();
      if (!query) {{
        setAnswerStatus('需要填写问题', true);
        return;
      }}
      state.answerLoading = true;
      byId('answerSubmit').disabled = true;
      byId('answerCompare').disabled = true;
      setAnswerStatus('正在生成回答...', false);
      try {{
        const selectedRole = byId('answerRole').value;
        const response = await fetch('/api/answer', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            query,
            auto_route: selectedRole === '__auto__',
            role_id: selectedRole === '__auto__' || selectedRole === 'all' ? '' : selectedRole,
            symbol: byId('answerSymbol').value.trim(),
            topic: byId('answerTopic').value.trim(),
            limit: Number(byId('answerLimit').value || 5),
          }}),
        }});
        const payload = await response.json();
        if (!response.ok || !payload.ok) {{
          refreshUiFromPayload(payload);
          renderRoleAgentResult(payload);
          throw new Error(payload.error || `HTTP ${{response.status}}`);
        }}
        renderLiveAnswer(payload);
        refreshUiFromPayload(payload);
        setAnswerStatus('回答已归档。', false, true);
      }} catch (error) {{
        setAnswerStatus(`${{error.message}}。请确认本地服务正在运行后再生成回答。`, true);
      }} finally {{
        state.answerLoading = false;
        byId('answerSubmit').disabled = false;
        byId('answerCompare').disabled = false;
      }}
    }}

    async function submitComparison() {{
      if (state.answerLoading) return;
      const query = byId('answerQuery').value.trim();
      if (!query) {{
        setAnswerStatus('需要填写问题', true);
        return;
      }}
      state.answerLoading = true;
      byId('answerSubmit').disabled = true;
      byId('answerCompare').disabled = true;
      setAnswerStatus('正在比较角色...', false);
      try {{
        const selectedRole = byId('answerRole').value;
        const response = await fetch('/api/compare', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            query,
            roles: selectedRole === '__auto__' ? 'auto' : selectedRole,
            symbol: byId('answerSymbol').value.trim(),
            topic: byId('answerTopic').value.trim(),
            limit: Number(byId('answerLimit').value || 3),
            evidence_limit: Number(byId('answerLimit').value || 3),
          }}),
        }});
        const payload = await response.json();
        if (!response.ok || !payload.ok) {{
          throw new Error(payload.error || `HTTP ${{response.status}}`);
        }}
        renderLiveComparison(payload);
        refreshUiFromPayload(payload);
        setAnswerStatus('角色比较已归档。', false, true);
      }} catch (error) {{
        setAnswerStatus(`${{error.message}}。请确认本地服务正在运行后再比较角色。`, true);
      }} finally {{
        state.answerLoading = false;
        byId('answerSubmit').disabled = false;
        byId('answerCompare').disabled = false;
      }}
    }}

    async function distillSelectedRoleSkill() {{
      const roleId = byId('roleAgentRole').value.trim();
      if (!roleId) {{
        setAnswerStatus('蒸馏角色技能需要选择角色', true);
        return;
      }}
      setAnswerStatus('正在蒸馏角色技能...', false);
      try {{
        const response = await fetch('/api/role/distill', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ role_id: roleId, limit: Number(byId('roleAgentLimit').value || 12) }}),
        }});
        const payload = await response.json();
        if (!response.ok || !payload.ok) {{
          throw new Error(payload.error || `HTTP ${{response.status}}`);
        }}
        refreshUiFromPayload(payload);
        renderRoleAgentResult(payload);
        setAnswerStatus('角色技能已蒸馏。', false, true);
      }} catch (error) {{
        setAnswerStatus(`${{error.message}}。请检查角色技能覆盖状态。`, true);
      }}
    }}

    async function submitRoleAgent(event) {{
      event.preventDefault();
      const query = byId('roleAgentQuery').value.trim();
      const roleId = byId('roleAgentRole').value.trim();
      const callLlm = byId('roleAgentCallLlm').checked;
      if (!query || !roleId) {{
        setAnswerStatus('角色代理需要填写角色和问题', true);
        return;
      }}
      setAnswerStatus(callLlm ? '正在运行角色代理...' : '正在生成角色代理提示词...', false);
      try {{
        const response = await fetch('/api/role/ask', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            role_id: roleId,
            query,
            symbol: byId('roleAgentSymbol').value.trim(),
            topic: byId('roleAgentTopic').value.trim(),
            limit: Number(byId('roleAgentLimit').value || 5),
            model: byId('roleAgentModel').value.trim(),
            temperature: Number(byId('roleAgentTemperature').value || 0.2),
            dry_run: !callLlm,
          }}),
        }});
        const payload = await response.json();
        if (!response.ok || !payload.ok) {{
          throw new Error(payload.error || `HTTP ${{response.status}}`);
        }}
        refreshUiFromPayload(payload);
        renderRoleAgentResult(payload);
        setAnswerStatus(callLlm ? '角色代理回答已归档。' : '角色代理提示词已归档。', false, true);
      }} catch (error) {{
        setAnswerStatus(`${{error.message}}。请检查角色代理运行时配置。`, true);
      }}
    }}

    function renderRoleAgentResult(payload) {{
      const prompt = payload.prompt_bundle || null;
      const skill = payload.skill || null;
      const target = byId('roleAgentResult');
      if (!target) return;
      if (prompt) {{
        target.innerHTML = `
          <article class="statement">
            <h3>${{esc(prompt.query || payload.query || '角色代理')}}</h3>
            <div class="meta"><span>${{esc(prompt.role_id || payload.role_id || '')}}</span><span>${{esc(uiText((payload.llm || {{}}).status || ''))}}</span><span>${{esc(((prompt.coverage || {{}}).evidence_count) || 0)}} 条证据</span></div>
            <p>${{esc((prompt.messages || [{{}}, {{}}])[1].content || '').slice(0, 520)}}...</p>
            <div class="meta"><a href="${{esc(payload.role_agent_json || '')}}">role-agent.json</a><a href="${{esc(payload.role_agent_markdown || '')}}">role-agent.md</a></div>
          </article>
        `;
        return;
      }}
      if (skill) {{
        target.innerHTML = `
          <article class="statement">
            <h3>${{esc(skill.display_name || skill.role_id || '角色技能')}}</h3>
            <div class="meta"><span>${{esc(skill.role_id || '')}}</span><span>${{esc(skill.source_statement_count || 0)}} 条发言</span></div>
            <p>${{esc(((skill.knowledge_system || {{}}).decision_frameworks || []).join(' '))}}</p>
            <div class="meta"><a href="${{esc(payload.skill_json || '')}}">role.skill.json</a><a href="${{esc(payload.skill_markdown || '')}}">role.skill.md</a></div>
          </article>
        `;
      }}
    }}

    function renderLiveAnswer(payload) {{
      const answer = answerExportItem(payload);
      data.answer_exports = [answer, ...(data.answer_exports || []).filter((item) => item.answer_json !== answer.answer_json)];
      byId('liveAnswer').innerHTML = answerCard(answer);
      renderAnswers();
    }}

    function renderLiveComparison(payload) {{
      const comparison = comparisonExportItem(payload);
      data.comparison_exports = [comparison, ...((data.comparison_exports || []).filter((item) => item.comparison_json !== comparison.comparison_json))];
      byId('liveAnswer').innerHTML = comparisonCard(comparison);
      renderAnswers();
    }}

    function answerExportItem(payload) {{
      const answer = payload.answer || {{}};
      const coverage = answer.coverage || {{}};
      return {{
        query: answer.query || '',
        generated_at: answer.generated_at || '',
        confidence: answer.confidence || '',
        answer: answer.answer || '',
        role_answer: answer.role_answer || null,
        key_points: answer.key_points || [],
        role_routing: answer.role_routing || payload.role_routing || null,
        selected_role_id: answer.selected_role_id || payload.selected_role_id || '',
        selection_mode: answer.selection_mode || payload.selection_mode || '',
        evidence_count: coverage.evidence_count || 0,
        citation_count: (answer.citations || []).length,
        status: coverage.evidence_count > 0 ? 'deliverable' : 'no_evidence',
        evidence_backed: (coverage.evidence_count || 0) > 0,
        answer_json: payload.answer_json || '',
        answer_markdown: payload.answer_markdown || '',
      }};
    }}

    function comparisonExportItem(payload) {{
      const comparison = payload.comparison || {{}};
      const coverage = comparison.coverage || {{}};
      return {{
        query: comparison.query || '',
        generated_at: comparison.generated_at || '',
        confidence: comparison.confidence || '',
        comparison_answer: comparison.comparison_answer || '',
        role_count: coverage.role_count || 0,
        evidence_count: coverage.evidence_count || 0,
        role_ids: (comparison.role_answers || []).map((item) => item.role_id).filter(Boolean),
        role_answers: comparison.role_answers || [],
        consensus: comparison.consensus || null,
        divergences: comparison.divergences || [],
        status: coverage.evidence_count > 0 ? 'deliverable' : 'no_evidence',
        evidence_backed: (coverage.evidence_count || 0) > 0,
        review_status: (comparison.review && comparison.review.status) || 'draft',
        reviewed_at: (comparison.review && comparison.review.reviewed_at) || '',
        reviewer: (comparison.review && comparison.review.reviewer) || '',
        review_notes: (comparison.review && comparison.review.notes) || '',
        adopted: ((comparison.review && comparison.review.status) || 'draft') === 'adopted',
        comparison_json: payload.comparison_json || '',
        comparison_markdown: payload.comparison_markdown || '',
      }};
    }}

    function answerCard(answer) {{
      return `
        <article class="statement">
          <h3>${{esc(answer.query || '未命名回答')}}</h3>
          <div class="meta"><span>${{esc(answer.generated_at || '未知')}}</span><span>${{esc(answer.evidence_count)}} 条证据</span>${{statusPill(answer.status || 'unknown', healthTone(answer.status || ''))}}<span>${{esc(uiText(answer.confidence || 'unknown'))}}</span><span>${{esc(uiText(answer.selection_mode || 'manual'))}}</span><span>${{esc(answer.selected_role_id || '全部角色')}}</span></div>
          <p>${{esc(answer.answer || '')}}</p>
          ${{roleAnswer(answer)}}
          ${{routeHints(answer)}}
          ${{answerPoints(answer)}}
          <div class="meta"><a href="${{esc(answer.answer_json)}}">answer.json</a><a href="${{esc(answer.answer_markdown)}}">answer.md</a></div>
        </article>
      `;
    }}

    function comparisonCard(comparison) {{
      const roles = comparison.role_answers || [];
      const roleRows = roles.length ? roles.map((role) => `<tr><td><code>${{esc(role.role_id)}}</code></td><td>${{esc(uiText(role.status || ''))}}</td><td>${{esc(role.evidence_count || 0)}}</td><td>${{esc(uiText(role.dominant_stance || ''))}}</td><td>${{esc((role.time_horizons || []).map(uiText).join(', '))}}</td></tr>`).join('') : (comparison.role_ids || []).map((roleId) => `<tr><td><code>${{esc(roleId)}}</code></td><td>${{esc(uiText(comparison.status || ''))}}</td><td>${{esc(comparison.evidence_count || 0)}}</td><td></td><td></td></tr>`).join('');
      const divergenceRows = (comparison.divergences || []).map((item) => `<li>${{esc(item.summary || item)}}</li>`).join('');
      const consensus = comparison.consensus && comparison.consensus.summary ? comparison.consensus.summary : '';
      const reviewStatus = comparison.review_status || 'draft';
      return `
        <article class="statement">
          <h3>${{esc(comparison.query || '未命名比较')}}</h3>
          <div class="meta"><span>${{esc(comparison.generated_at || '未知')}}</span><span>${{esc(comparison.role_count || 0)}} 个角色</span><span>${{esc(comparison.evidence_count || 0)}} 条证据</span>${{statusPill(comparison.status || 'unknown', healthTone(comparison.status || ''))}}${{statusPill(reviewStatus, healthTone(reviewStatus))}}<span>${{esc(uiText(comparison.confidence || 'unknown'))}}</span><span>${{esc(comparison.reviewer || '')}}</span></div>
          <p>${{esc(comparison.comparison_answer || '')}}</p>
          ${{consensus ? `<p>${{esc(consensus)}}</p>` : ''}}
          <div class="action-table-scroll"><table><thead><tr><th>角色</th><th>状态</th><th>证据</th><th>立场</th><th>周期</th></tr></thead><tbody>${{roleRows || '<tr><td colspan="5">暂无角色比较行。</td></tr>'}}</tbody></table></div>
          ${{divergenceRows ? `<ul class="key-points">${{divergenceRows}}</ul>` : ''}}
          <div class="meta">
            <button type="button" data-comparison-review data-query="${{esc(comparison.query || '')}}" data-status="reviewed">标记已审阅</button>
            <button type="button" data-comparison-review data-query="${{esc(comparison.query || '')}}" data-status="adopted">采纳</button>
            <button type="button" data-comparison-review data-query="${{esc(comparison.query || '')}}" data-status="rejected">拒绝</button>
          </div>
          <div class="meta"><a href="${{esc(comparison.comparison_json || '')}}">comparison.json</a><a href="${{esc(comparison.comparison_markdown || '')}}">comparison.md</a></div>
        </article>
      `;
    }}

    async function handleComparisonReviewClick(event) {{
      const button = event.target.closest('[data-comparison-review]');
      if (!button) return;
      await submitComparisonReview(button.dataset.query || '', button.dataset.status || 'reviewed');
    }}

    async function handleNextActionClick(event) {{
      const button = event.target.closest('[data-next-action]');
      if (!button) return;
      const action = (data.next_actions || []).find((item) => item.id === button.dataset.nextAction);
      if (!action || !action.endpoint) return;
      await runNextAction(action);
    }}

    async function handleRemediationClick(event) {{
      const button = event.target.closest('[data-remediation-item]');
      if (!button) return;
      const item = (((data.remediation_queue || {{}}).items) || []).find((entry) => entry.id === button.dataset.remediationItem);
      if (!item) return;
      await runRemediationItem(item);
    }}

    async function handleActionRunRetryClick(event) {{
      const button = event.target.closest('[data-action-run-retry]');
      if (!button) return;
      await retryActionRun(button.dataset.actionRunRetry || '');
    }}

    async function handleAnswerRegressionClick(event) {{
      const button = event.target.closest('[data-answer-regression-item]');
      if (!button) return;
      const item = (((data.answer_regression || {{}}).items) || []).find((entry) => entry.id === button.dataset.answerRegressionItem);
      if (!item) return;
      await runAnswerRegressionItem(item);
    }}

    async function handleAnswerRegressionDeleteClick(event) {{
      const button = event.target.closest('[data-answer-regression-delete]');
      if (!button) return;
      event.preventDefault();
      await deleteAnswerRegressionQuestion(button.dataset.answerRegressionDelete || '');
    }}

    async function handleAnswerRegressionBatchClick(event) {{
      const exportButton = event.target.closest('#answerRegressionExport');
      if (exportButton) {{
        event.preventDefault();
        await exportAnswerRegressionSuite();
        return;
      }}
      const importButton = event.target.closest('[data-answer-regression-import]');
      if (!importButton) return;
      event.preventDefault();
      await submitAnswerRegressionImport(importButton.dataset.answerRegressionImport !== 'apply');
    }}

    async function handleAnswerRegressionSubmit(event) {{
      if (!event.target || event.target.id !== 'answerRegressionForm') return;
      await submitAnswerRegressionQuestion(event);
    }}

    function handleActionRunFilterChange(event) {{
      if (!event.target || event.target.id !== 'actionRunFilter') return;
      state.actionRunStatus = event.target.value || 'all';
      renderActions();
    }}

    async function submitComparisonReview(query, status) {{
      if (!query) {{
        setAnswerStatus('需要填写问题', true);
        return;
      }}
      setAnswerStatus(`正在更新角色比较审阅状态：${{uiText(status)}}...`, false);
      try {{
        const response = await fetch('/api/comparison/review', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            query,
            status,
            reviewer: 'local-ui',
            notes: `已在本地 UI 标记为 ${{status}}。`,
          }}),
        }});
        const payload = await response.json();
        if (!response.ok || !payload.ok) {{
          throw new Error(payload.error || `HTTP ${{response.status}}`);
        }}
        renderLiveComparison(payload);
        refreshUiFromPayload(payload);
        setAnswerStatus(`角色比较已标记为${{uiText(status)}}。`, false, true);
      }} catch (error) {{
        setAnswerStatus(`${{error.message}}。请确认本地服务正在运行后再审阅角色比较。`, true);
      }}
    }}

    async function runNextAction(action) {{
      const payload = action.payload || {{}};
      setAnswerStatus(`正在执行后续行动：${{messageText(action.label || action.action_type)}}...`, false);
      try {{
        const response = await fetch(action.endpoint, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        const result = await response.json();
        if (!response.ok || !result.ok) {{
          throw new Error(result.error || `HTTP ${{response.status}}`);
        }}
        if (action.action_type === 'answer') {{
          renderLiveAnswer(result);
          showView('answers');
        }} else if (action.action_type === 'compare' || action.action_type === 'review_comparison') {{
          renderLiveComparison(result);
          showView('answers');
        }}
        refreshUiFromPayload(result);
        setAnswerStatus(`后续行动已完成：${{messageText(action.label || action.action_type)}}。`, false, true);
      }} catch (error) {{
        setAnswerStatus(`${{error.message}}。本地 API 不可用时，可使用研究行动表中的命令。`, true);
      }}
    }}

    async function runRemediationItem(item) {{
      if (!item.endpoint) {{
        setAnswerStatus('该修复项需要从命令列执行。', true);
        return;
      }}
      setAnswerStatus(`正在执行修复：${{messageText(item.label || item.action_type)}}...`, false);
      try {{
        const response = await fetch(item.endpoint, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(item.payload || {{}}),
        }});
        const payload = await response.json();
        if (!response.ok || !payload.ok) {{
          throw new Error(payload.error || `HTTP ${{response.status}}`);
        }}
        if (payload.answer) {{
          renderLiveAnswer(payload);
          showView('answers');
        }} else if (payload.comparison) {{
          renderLiveComparison(payload);
          showView('answers');
        }}
        refreshUiFromPayload(payload);
        setAnswerStatus(`修复已完成：${{messageText(item.label || item.action_type)}}。`, false, true);
      }} catch (error) {{
        setAnswerStatus(`${{error.message}}。请检查修复队列中的当前状态。`, true);
      }}
    }}

    function answerRegressionActionControl(item) {{
      if (item.status === 'pass' || !item.recommended_endpoint) return '';
      return `<button type="button" data-answer-regression-item="${{esc(item.id)}}">执行</button>`;
    }}

    async function submitAnswerRegressionQuestion(event) {{
      event.preventDefault();
      const query = byId('regressionQuery').value.trim();
      if (!query) {{
        setAnswerStatus('需要填写回归问题', true);
        return;
      }}
      const payload = {{
        id: byId('regressionId').value.trim(),
        query,
        role_id: byId('regressionRole').value.trim(),
        symbol: byId('regressionSymbol').value.trim(),
        topic: byId('regressionTopic').value.trim(),
        expected_role_id: byId('regressionExpectedRole').value.trim() || byId('regressionRole').value.trim(),
        min_evidence: Number.parseInt(byId('regressionMinEvidence').value || '1', 10),
        requires_role_answer: byId('regressionRequiresRoleAnswer').checked,
        source_url: byId('regressionSourceUrl').value.trim(),
        rationale: byId('regressionRationale').value.trim(),
        updated_by: 'local-ui',
      }};
      setAnswerStatus('正在保存回归问题...', false);
      try {{
        const response = await fetch('/api/evaluations/answer-question', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        const result = await response.json();
        if (!response.ok || !result.ok) {{
          throw new Error(result.error || `HTTP ${{response.status}}`);
        }}
        refreshUiFromPayload(result);
        showView('answers');
        setAnswerStatus(`回归问题已保存：${{result.question.id}}。`, false, true);
      }} catch (error) {{
        setAnswerStatus(`${{error.message}}。请确认本地服务正在运行后再管理固定回答回归问题。`, true);
      }}
    }}

    async function deleteAnswerRegressionQuestion(questionId) {{
      if (!questionId) {{
        setAnswerStatus('需要填写回归问题 ID', true);
        return;
      }}
      setAnswerStatus(`正在删除回归问题：${{questionId}}...`, false);
      try {{
        const response = await fetch('/api/evaluations/answer-question/delete', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ id: questionId, updated_by: 'local-ui' }}),
        }});
        const result = await response.json();
        if (!response.ok || !result.ok) {{
          throw new Error(result.error || `HTTP ${{response.status}}`);
        }}
        refreshUiFromPayload(result);
        showView('answers');
        setAnswerStatus(`回归问题已删除：${{result.deleted_id}}。`, false, true);
      }} catch (error) {{
        setAnswerStatus(`${{error.message}}。请检查回答回归套件的当前状态。`, true);
      }}
    }}

    async function exportAnswerRegressionSuite() {{
      setAnswerStatus('正在导出回归套件...', false);
      try {{
        const response = await fetch('/api/evaluations/answer-suite/export');
        const payload = await response.json();
        if (!response.ok || !payload.ok) {{
          throw new Error(payload.error || (payload.errors || []).join('; ') || `HTTP ${{response.status}}`);
        }}
        const target = byId('answerRegressionImportPayload');
        if (target) target.value = JSON.stringify(payload, null, 2);
        setAnswerStatus(`回归套件已导出：${{payload.question_count || 0}} 个问题。`, false, true);
      }} catch (error) {{
        setAnswerStatus(`${{error.message}}。请检查回答回归套件有效性。`, true);
      }}
    }}

    async function submitAnswerRegressionImport(dryRun) {{
      const target = byId('answerRegressionImportPayload');
      const text = target ? target.value.trim() : '';
      if (!text) {{
        setAnswerStatus('需要填写回答回归导入 JSON', true);
        return;
      }}
      let suite = null;
      try {{
        suite = JSON.parse(text);
      }} catch (error) {{
        setAnswerStatus(`回答回归导入 JSON 无效：${{error.message}}`, true);
        return;
      }}
      setAnswerStatus(dryRun ? '正在预览回归套件导入...' : '正在导入回归套件...', false);
      try {{
        const response = await fetch('/api/evaluations/answer-suite/import', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ suite, dry_run: dryRun, updated_by: 'local-ui' }}),
        }});
        const result = await response.json();
        if (!response.ok || !result.ok) {{
          throw new Error(result.error || (result.errors || []).join('; ') || `HTTP ${{response.status}}`);
        }}
        refreshUiFromPayload(result);
        showView('answers');
        const summary = result.summary || {{}};
        const verb = dryRun ? '导入试运行' : '回归套件已导入';
        setAnswerStatus(`${{verb}}：${{summary.create || 0}} 个新增，${{summary.update || 0}} 个更新，${{summary.unchanged || 0}} 个未变。`, false, true);
      }} catch (error) {{
        setAnswerStatus(`${{error.message}}。请检查回答回归导入内容。`, true);
      }}
    }}

    async function runAnswerRegressionItem(item) {{
      if (!item.recommended_endpoint) {{
        setAnswerStatus('该回归项没有可用修复接口。', true);
        return;
      }}
      setAnswerStatus(`正在修复回归回答：${{item.id || item.query}}...`, false);
      try {{
        const response = await fetch(item.recommended_endpoint, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(item.payload || {{}}),
        }});
        const payload = await response.json();
        if (!response.ok || !payload.ok) {{
          throw new Error(payload.error || `HTTP ${{response.status}}`);
        }}
        if (payload.answer) {{
          renderLiveAnswer(payload);
        }}
        refreshUiFromPayload(payload);
        showView('answers');
        setAnswerStatus(`回归项已修复：${{item.id || item.query}}。`, false, true);
      }} catch (error) {{
        setAnswerStatus(`${{error.message}}。请检查回答回归中的当前状态。`, true);
      }}
    }}

    async function retryActionRun(runId) {{
      if (!runId) {{
        setAnswerStatus('需要填写行动运行 ID', true);
        return;
      }}
      setAnswerStatus('正在重试行动运行...', false);
      try {{
        const response = await fetch('/api/action-runs/retry', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ run_id: runId }}),
        }});
        const payload = await response.json();
        if (!response.ok || !payload.ok) {{
          throw new Error(payload.error || `HTTP ${{response.status}}`);
        }}
        if (payload.answer) {{
          renderLiveAnswer(payload);
          showView('answers');
        }} else if (payload.comparison) {{
          renderLiveComparison(payload);
          showView('answers');
        }}
        refreshUiFromPayload(payload);
        setAnswerStatus('行动运行已重试。', false, true);
      }} catch (error) {{
        setAnswerStatus(`${{error.message}}。请在行动历史中查看新的重试结果。`, true);
      }}
    }}

    function routeHints(answer) {{
      const routing = answer.role_routing || null;
      if (!routing || !(routing.routes || []).length) return '';
      const rows = routing.routes.map((route) => `<tr><td><code>${{esc(route.role_id)}}</code></td><td>${{esc(uiText(route.confidence))}}</td><td>${{esc(route.evidence_count)}}</td><td>${{esc(messageText(route.reason || ''))}}</td></tr>`).join('');
      return `
        <div class="route-hints">
          <div class="meta"><span>已选角色：${{esc(answer.selected_role_id || routing.suggested_role_id || '')}}</span><span>路由置信度：${{esc(uiText(routing.confidence || ''))}}</span></div>
          <div class="action-table-scroll"><table><thead><tr><th>角色</th><th>置信度</th><th>证据</th><th>原因</th></tr></thead><tbody>${{rows}}</tbody></table></div>
        </div>
      `;
    }}

    function roleAnswer(answer) {{
      const role = answer.role_answer || null;
      if (!role || !role.answer) return '';
      const refs = (role.evidence_refs || []).join(' ');
      return `
        <div class="route-hints">
          <h3>角色回答</h3>
          <div class="meta"><span>${{esc(role.display_name || role.role_id || '角色')}}</span><span>${{esc(uiText(role.mode || ''))}}</span><span>${{esc(role.source_scope || '')}}</span><span>${{esc(refs)}}</span></div>
          <p>${{esc(role.answer || '')}}</p>
        </div>
      `;
    }}

    function setAnswerStatus(message, isError, isOk = false) {{
      byId('answerError').textContent = message;
      byId('answerError').className = `answer-status ${{isError ? 'error' : (isOk ? 'ok' : '')}}`;
    }}

    function answerPoints(answer) {{
      const points = answer.key_points || [];
      if (!points.length) return '';
      return `<ul class="key-points">${{points.map((point) => `<li>${{esc((point.refs || []).join(' '))}} ${{esc(point.text || '')}}</li>`).join('')}}</ul>`;
    }}

    function renderEvents() {{
      const eventRows = data.events.map((event) => `<tr><td>${{esc(event.date)}}</td><td><code>${{esc(event.event_id)}}</code></td><td>${{esc(event.title)}}</td><td>${{pills(event.symbols)}}${{pills(event.topics)}}</td></tr>`).join('');
      const releaseRows = data.release_readiness.checks.map((check) => `<tr><td>${{statusPill(check.ok ? 'ok' : 'fail', boolTone(check.ok))}}</td><td><code>${{esc(check.id)}}</code></td><td>${{esc(messageText(check.message))}}</td><td><code class="command">${{esc(messageText(check.remediation || ''))}}</code></td></tr>`).join('');
      byId('events').innerHTML = `
        <section class="panel"><div class="view-header"><div><h2>事件</h2><p>可用于多角色分析的事件文件。</p></div></div><div class="action-table-scroll"><table><thead><tr><th>日期</th><th>事件</th><th>标题</th><th>标签</th></tr></thead><tbody>${{eventRows}}</tbody></table></div></section>
        <section class="panel"><div class="view-header"><div><h2>发布检查</h2><p>当前交付产物的发布门禁。</p></div></div><div class="action-table-scroll"><table><thead><tr><th>状态</th><th>检查项</th><th>说明</th><th>修复建议</th></tr></thead><tbody>${{releaseRows}}</tbody></table></div></section>
      `;
    }}

    function renderCapture() {{
      const summary = data.capture_status.summary || {{}};
      const files = data.capture_status.files || [];
      const fileRows = files.map((file) => `<tr><td>${{statusPill(file.status, healthTone(file.status))}}</td><td><code>${{esc(file.source_file)}}</code></td><td>${{esc(file.records_seen)}}</td><td>${{esc(file.notes_written)}}</td><td>${{esc(file.duplicates_skipped)}}</td></tr>`).join('');
      const sourceRows = (data.source_configs || []).map((source) => `<tr><td>${{statusPill(source.status, healthTone(source.status))}}</td><td><code>${{esc(source.source_id)}}</code></td><td>${{esc(source.role_id)}}</td><td>${{esc(uiText(source.platform))}}</td><td>${{esc(source.source_url || '')}}</td></tr>`).join('');
      const sourceOptions = (data.source_configs || []).map((source) => `<option value="${{esc(source.source_id)}}">${{esc(source.source_id)}} · ${{esc(source.role_id)}}</option>`).join('');
      const sourceAdapterRows = ((data.source_adapter_validation && data.source_adapter_validation.sources) || []).map((source) => `<tr><td>${{statusPill(source.status, healthTone(source.status))}}</td><td><code>${{esc(source.source_id)}}</code></td><td>${{esc(uiText(source.adapter || ''))}}</td><td>${{esc(source.record_count || 0)}}</td><td>${{esc(messageText(source.message || ''))}}</td></tr>`).join('');
      const sourceImportRows = ((data.source_import_status && data.source_import_status.imports) || []).slice(0, 12).map((item) => `<tr><td>${{statusPill(item.status, healthTone(item.status))}}</td><td><code>${{esc(item.source_id)}}</code></td><td>${{esc(item.record_count || 0)}}</td><td>${{esc(uiText(item.preflight_status || ''))}}</td><td>${{esc(item.input_path || '')}}</td></tr>`).join('');
      const sourceRunRows = ((data.source_status && data.source_status.runs) || []).slice(0, 12).map((run) => `<tr><td>${{statusPill(run.status, healthTone(run.status))}}</td><td><code>${{esc(run.source_id)}}</code></td><td>${{esc(uiText(run.adapter || ''))}}</td><td>${{esc(run.ran_at || '')}}</td><td>${{esc(messageText(run.error || run.capture_path || ''))}}</td></tr>`).join('');
      const sourceJobRows = ((data.source_job_status && data.source_job_status.jobs) || []).slice(0, 12).map((job) => `<tr><td>${{statusPill(job.status, healthTone(job.status))}}</td><td><code>${{esc(job.job_id)}}</code></td><td>${{esc(job.source_id)}}</td><td>${{esc(job.due_at || '')}}</td><td>${{esc(messageText(job.last_error || job.run_id || ''))}}</td></tr>`).join('');
      const accountArchiveRows = (data.account_archives || []).map((account) => `<tr><td>${{statusPill(account.status || '', healthTone(account.status || ''))}}</td><td>${{statusPill(account.collection_mode || '', healthTone(account.collection_mode || ''))}}</td><td><code>${{esc(account.account_id || '')}}</code></td><td>${{esc(uiText(account.platform || ''))}}</td><td>${{esc(account.platform_account_id || '')}}</td><td>${{esc(account.role_id || '')}}</td></tr>`).join('');
      const accountOptions = (data.account_archives || []).map((account) => `<option value="${{esc(account.account_id || '')}}">${{esc(account.account_id || '')}} · ${{esc(uiText(account.collection_mode || ''))}}</option>`).join('');
      byId('capture').innerHTML = `
        <section class="panel full"><div class="view-header"><div><h2>账户归档</h2><p>${{esc((data.account_status.summary || {{}}).blocked || 0)}} 阻塞 · ${{esc((data.account_status.summary || {{}}).ready || 0)}} 就绪 · ${{esc((data.account_status.summary || {{}}).total || 0)}} 总数</p></div></div>
          <form id="accountArchiveForm" class="ask-form">
            <div class="onboarding-grid">
              <select id="accountArchivePlatform" aria-label="平台" required>
                <option value="rss">RSS / Atom</option>
                <option value="weibo">微博</option>
                <option value="wechat">微信公众号</option>
                <option value="xueqiu">雪球</option>
                <option value="local-export">本地导出</option>
                <option value="custom-api">自定义 API</option>
              </select>
              <input id="accountArchiveAccount" type="text" placeholder="账户归档 ID" required>
              <input id="accountArchivePlatformId" type="text" placeholder="平台账号 ID" required>
              <input id="accountArchiveRole" type="text" placeholder="角色 ID" required>
              <input id="accountArchiveDisplayName" type="text" placeholder="显示名称">
              <select id="accountArchiveMode" aria-label="采集模式">
                <option value="auto">自动</option>
                <option value="rss">RSS</option>
                <option value="local-export">本地导出</option>
                <option value="custom-api">自定义 API</option>
                <option value="blocked">阻塞</option>
              </select>
              <input id="accountArchiveFeedUrl" class="wide" type="text" placeholder="RSS/Atom feed URL 或本地文件路径">
              <input id="accountArchiveInputPath" class="wide" type="text" placeholder="本地导出路径">
              <input id="accountArchiveApiUrl" class="wide" type="url" placeholder="授权 API URL">
              <input id="accountArchiveSymbols" type="text" placeholder="标的代码">
              <input id="accountArchiveTopics" type="text" placeholder="主题">
              <button id="accountArchiveSubmit" type="submit">创建账户归档</button>
            </div>
          </form>
          <form id="accountArchiveCollectForm" class="ask-form">
            <div class="onboarding-grid">
              <select id="accountArchiveCollectAccount" class="wide" aria-label="待采集账户" required>${{accountOptions || '<option value="">暂无账户归档</option>'}}</select>
              <label class="check"><input id="accountArchiveCollectSync" type="checkbox" checked> 采集后同步</label>
              <label class="check"><input id="accountArchiveCollectArchive" type="checkbox"> 归档采集文件</label>
              <button id="accountArchiveCollectSubmit" type="submit">采集账户</button>
            </div>
          </form>
          <div id="accountArchiveStatus" class="answer-status" aria-live="polite"></div>
          <div id="accountArchiveResult" class="list"></div>
          <div class="action-table-scroll"><table><thead><tr><th>状态</th><th>模式</th><th>账户</th><th>平台</th><th>平台账号 ID</th><th>角色</th></tr></thead><tbody>${{accountArchiveRows || '<tr><td colspan="6">暂无账户归档配置。</td></tr>'}}</tbody></table></div>
        </section>
        <section class="panel full"><div class="view-header"><div><h2>来源接入</h2><p>创建来源后，将已审阅公开发言写入本地知识库。</p></div></div>
          <form id="onboardingForm" class="ask-form">
            <div class="onboarding-grid">
              <input id="onboardRoleId" type="text" placeholder="角色 ID" required>
              <input id="onboardDisplayName" type="text" placeholder="显示名称">
              <input id="onboardSourceId" type="text" placeholder="来源 ID" required>
              <input id="onboardPlatform" type="text" placeholder="平台" required>
              <input id="onboardSourceUrl" class="wide" type="url" placeholder="公开来源 URL">
              <input id="onboardSymbols" type="text" placeholder="标的代码">
              <input id="onboardTopics" type="text" placeholder="主题">
              <input id="onboardTags" type="text" placeholder="标签">
              <button id="onboardingSubmit" type="submit">创建来源</button>
            </div>
          </form>
          <form id="onboardingStatementForm" class="ask-form">
            <div class="onboarding-grid">
              <select id="onboardStatementSource" aria-label="发言来源" required>${{sourceOptions || '<option value="">暂无来源</option>'}}</select>
              <input id="onboardStatementTitle" type="text" placeholder="标题">
              <input id="onboardStatementUrl" class="wide" type="url" placeholder="发言 URL">
              <textarea id="onboardStatementText" class="full" placeholder="公开发言正文" required></textarea>
              <input id="onboardStatementSymbols" type="text" placeholder="标的代码">
              <input id="onboardStatementTopics" type="text" placeholder="主题">
              <select id="onboardStatementStance" aria-label="立场">
                <option value="unclear">不明确</option>
                <option value="bullish">看多</option>
                <option value="bearish">看空</option>
                <option value="mixed">分歧</option>
              </select>
              <select id="onboardStatementHorizon" aria-label="周期">
                <option value="unknown">未知</option>
                <option value="short_term">短期</option>
                <option value="medium_term">中期</option>
                <option value="long_term">长期</option>
              </select>
              <select id="onboardStatementConfidence" aria-label="置信度">
                <option value="low">低</option>
                <option value="medium">中</option>
                <option value="high">高</option>
              </select>
              <label class="check"><input id="onboardPromote" type="checkbox" checked> 更新档案</label>
              <button id="onboardingStatementSubmit" type="submit">采集发言</button>
            </div>
          </form>
          <div id="onboardingStatus" class="answer-status" aria-live="polite"></div>
          <div id="onboardingResult" class="list"></div>
        </section>
        <section class="panel full"><div class="summary-grid"><article class="summary-tile"><span>已处理</span><strong>${{esc(summary.processed || 0)}}</strong></article><article class="summary-tile"><span>失败</span><strong>${{esc(summary.failed || 0)}}</strong></article><article class="summary-tile"><span>记录</span><strong>${{esc(summary.records_seen || 0)}}</strong></article><article class="summary-tile"><span>写入</span><strong>${{esc(summary.notes_written || 0)}}</strong></article><article class="summary-tile"><span>重复</span><strong>${{esc(summary.duplicates_skipped || 0)}}</strong></article></div><h2>采集摘要</h2><div class="action-table-scroll"><table><thead><tr><th>已处理</th><th>失败</th><th>记录</th><th>写入</th><th>重复</th></tr></thead><tbody><tr><td>${{esc(summary.processed || 0)}}</td><td>${{esc(summary.failed || 0)}}</td><td>${{esc(summary.records_seen || 0)}}</td><td>${{esc(summary.notes_written || 0)}}</td><td>${{esc(summary.duplicates_skipped || 0)}}</td></tr></tbody></table></div></section>
        <section class="panel"><div class="view-header"><div><h2>来源配置</h2><p>已配置的公开来源适配器。</p></div></div><div class="action-table-scroll"><table><thead><tr><th>状态</th><th>来源</th><th>角色</th><th>平台</th><th>URL</th></tr></thead><tbody>${{sourceRows || '<tr><td colspan="5">暂无来源配置。</td></tr>'}}</tbody></table></div></section>
        <section class="panel"><div class="view-header"><div><h2>来源适配器</h2><p>适配器就绪状态和输入校验。</p></div></div><div class="action-table-scroll"><table><thead><tr><th>状态</th><th>来源</th><th>适配器</th><th>记录</th><th>说明</th></tr></thead><tbody>${{sourceAdapterRows || '<tr><td colspan="5">暂无待校验来源适配器。</td></tr>'}}</tbody></table></div></section>
        <section class="panel"><div class="view-header"><div><h2>来源导入</h2><p>近期规范化来源导出。</p></div></div><div class="action-table-scroll"><table><thead><tr><th>状态</th><th>来源</th><th>记录</th><th>预检</th><th>输入</th></tr></thead><tbody>${{sourceImportRows || '<tr><td colspan="5">暂无来源导入记录。</td></tr>'}}</tbody></table></div></section>
        <section class="panel"><div class="view-header"><div><h2>来源运行</h2><p>最近的适配器执行记录。</p></div></div><div class="action-table-scroll"><table><thead><tr><th>状态</th><th>来源</th><th>适配器</th><th>运行时间</th><th>结果</th></tr></thead><tbody>${{sourceRunRows || '<tr><td colspan="5">暂无来源运行记录。</td></tr>'}}</tbody></table></div></section>
        <section class="panel"><div class="view-header"><div><h2>来源任务</h2><p>排队或已完成的来源采集任务。</p></div></div><div class="action-table-scroll"><table><thead><tr><th>状态</th><th>任务</th><th>来源</th><th>到期时间</th><th>结果</th></tr></thead><tbody>${{sourceJobRows || '<tr><td colspan="5">暂无来源任务。</td></tr>'}}</tbody></table></div></section>
        <section class="panel"><div class="view-header"><div><h2>采集文件</h2><p>等待同步的采集文件。</p></div></div><div class="action-table-scroll"><table><thead><tr><th>状态</th><th>文件</th><th>记录</th><th>写入</th><th>重复</th></tr></thead><tbody>${{fileRows || '<tr><td colspan="5">暂无待处理采集文件。</td></tr>'}}</tbody></table></div></section>
      `;
      bindAccountArchiveForms();
      bindOnboardingForms();
    }}

    function bindAccountArchiveForms() {{
      const accountForm = byId('accountArchiveForm');
      const collectForm = byId('accountArchiveCollectForm');
      if (accountForm) accountForm.addEventListener('submit', submitAccountArchive);
      if (collectForm) collectForm.addEventListener('submit', submitAccountCollect);
    }}

    async function submitAccountArchive(event) {{
      event.preventDefault();
      if (state.onboardingLoading) return;
      setOnboardingLoading(true);
      setAccountArchiveStatus('正在创建账户归档...', false);
      try {{
        const response = await fetch('/api/accounts/create', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            account_id: byId('accountArchiveAccount').value.trim(),
            platform: byId('accountArchivePlatform').value,
            platform_account_id: byId('accountArchivePlatformId').value.trim(),
            role_id: byId('accountArchiveRole').value.trim(),
            display_name: byId('accountArchiveDisplayName').value.trim(),
            mode: byId('accountArchiveMode').value,
            feed_url: byId('accountArchiveFeedUrl').value.trim(),
            input_path: byId('accountArchiveInputPath').value.trim(),
            api_url: byId('accountArchiveApiUrl').value.trim(),
            symbols: listFromInput('accountArchiveSymbols'),
            topics: listFromInput('accountArchiveTopics'),
          }}),
        }});
        const payload = await response.json();
        if (!response.ok || !payload.ok) {{
          throw new Error(payload.error || `HTTP ${{response.status}}`);
        }}
        refreshUiFromPayload(payload);
        renderAccountArchiveResult(payload);
        setAccountArchiveStatus('账户归档已就绪。', false, true);
      }} catch (error) {{
        setAccountArchiveStatus(`${{error.message}}。请检查账户归档字段。`, true);
      }} finally {{
        setOnboardingLoading(false);
      }}
    }}

    async function submitAccountCollect(event) {{
      event.preventDefault();
      if (state.onboardingLoading) return;
      const accountId = byId('accountArchiveCollectAccount').value.trim();
      if (!accountId) {{
        setAccountArchiveStatus('需要选择账户归档', true);
        return;
      }}
      setOnboardingLoading(true);
      setAccountArchiveStatus('正在采集账户归档...', false);
      try {{
        const response = await fetch('/api/accounts/collect', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            account_id: accountId,
            sync: byId('accountArchiveCollectSync').checked,
            archive: byId('accountArchiveCollectArchive').checked,
          }}),
        }});
        const payload = await response.json();
        if (!response.ok || !payload.ok) {{
          const collection = payload.account_collection || {{}};
          throw new Error(collection.error || payload.error || `HTTP ${{response.status}}`);
        }}
        refreshUiFromPayload(payload);
        renderAccountArchiveResult(payload);
        setAccountArchiveStatus('账户归档采集完成。', false, true);
      }} catch (error) {{
        setAccountArchiveStatus(`${{error.message}}。请检查账户归档状态。`, true);
      }} finally {{
        setOnboardingLoading(false);
      }}
    }}

    function renderAccountArchiveResult(payload) {{
      const target = byId('accountArchiveResult');
      if (!target) return;
      const account = payload.account || (payload.account_collection || {{}});
      const collection = payload.account_collection || null;
      target.innerHTML = `
        <article class="statement">
          <h3>${{esc(account.account_id || '账户归档')}}</h3>
          <div class="meta"><span>${{esc(uiText(account.platform || ''))}}</span><span>${{esc(uiText(account.collection_mode || ''))}}</span><span>${{esc(account.role_id || '')}}</span><span>${{esc(collection ? `${{collection.written || 0}} 条写入` : '已配置')}}</span></div>
          <p>${{esc((collection && collection.capture_path) || account.config_path || '账户归档配置已更新。')}}</p>
        </article>
      `;
    }}

    function setAccountArchiveStatus(message, isError, isOk = false) {{
      const target = byId('accountArchiveStatus');
      if (!target) return;
      target.textContent = message;
      target.className = `answer-status ${{isError ? 'error' : (isOk ? 'ok' : '')}}`;
    }}

    function bindOnboardingForms() {{
      const roleForm = byId('onboardingForm');
      const statementForm = byId('onboardingStatementForm');
      if (roleForm) roleForm.addEventListener('submit', submitOnboardingRoleSource);
      if (statementForm) statementForm.addEventListener('submit', submitOnboardingStatement);
    }}

    async function submitOnboardingRoleSource(event) {{
      event.preventDefault();
      if (state.onboardingLoading) return;
      setOnboardingLoading(true);
      setOnboardingStatus('正在创建来源...', false);
      try {{
        const response = await fetch('/api/onboarding/role-source', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            role_id: byId('onboardRoleId').value.trim(),
            display_name: byId('onboardDisplayName').value.trim(),
            source_id: byId('onboardSourceId').value.trim(),
            platform: byId('onboardPlatform').value.trim(),
            source_url: byId('onboardSourceUrl').value.trim(),
            symbols: listFromInput('onboardSymbols'),
            topics: listFromInput('onboardTopics'),
            tags: listFromInput('onboardTags'),
          }}),
        }});
        const payload = await response.json();
        if (!response.ok || !payload.ok) {{
          throw new Error(payload.error || `HTTP ${{response.status}}`);
        }}
        refreshUiFromPayload(payload);
        renderOnboardingResult(payload);
        const sourceSelect = byId('onboardStatementSource');
        if (sourceSelect && payload.source && payload.source.source_id) sourceSelect.value = payload.source.source_id;
        setOnboardingStatus('来源已就绪。', false, true);
      }} catch (error) {{
        setOnboardingStatus(`${{error.message}}。请确认本地服务正在运行后再使用来源接入。`, true);
      }} finally {{
        setOnboardingLoading(false);
      }}
    }}

    async function submitOnboardingStatement(event) {{
      event.preventDefault();
      if (state.onboardingLoading) return;
      const sourceId = byId('onboardStatementSource').value.trim();
      const text = byId('onboardStatementText').value.trim();
      if (!sourceId || !text) {{
        setOnboardingStatus('需要选择来源并填写发言正文', true);
        return;
      }}
      setOnboardingLoading(true);
      setOnboardingStatus('正在采集发言...', false);
      try {{
        const response = await fetch('/api/onboarding/statement', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            source_id: sourceId,
            title: byId('onboardStatementTitle').value.trim(),
            source_url: byId('onboardStatementUrl').value.trim(),
            text,
            symbols: listFromInput('onboardStatementSymbols'),
            topics: listFromInput('onboardStatementTopics'),
            stance: byId('onboardStatementStance').value,
            time_horizon: byId('onboardStatementHorizon').value,
            confidence: byId('onboardStatementConfidence').value,
            sync: true,
            archive: true,
            generate_profile: true,
            promote_profile: byId('onboardPromote').checked,
            reviewer: 'local-ui',
            review_note: '通过本地接入流程审阅公开发言。',
          }}),
        }});
        const payload = await response.json();
        if (!response.ok || !payload.ok) {{
          throw new Error(payload.error || `HTTP ${{response.status}}`);
        }}
        refreshUiFromPayload(payload);
        renderOnboardingResult(payload);
        setOnboardingStatus('发言已同步。', false, true);
      }} catch (error) {{
        setOnboardingStatus(`${{error.message}}。请确认本地服务正在运行后再采集发言。`, true);
      }} finally {{
        setOnboardingLoading(false);
      }}
    }}

    function refreshUiFromPayload(payload) {{
      const nextData = payload && payload.ui && payload.ui.data;
      if (!nextData || typeof nextData !== 'object') return;
      Object.keys(data).forEach((key) => delete data[key]);
      Object.assign(data, nextData);
      renderMetrics();
      renderOverview();
      renderActions();
      renderAnalysis();
      renderAnswers();
      renderEvents();
      hydrateFilters();
      renderStatements();
      renderCapture();
    }}

    function renderOnboardingResult(payload) {{
      const target = byId('onboardingResult');
      if (!target) return;
      const roleId = (payload.role && payload.role.role_id) || ((payload.source_run && payload.source_run.record && payload.source_run.record.role_id) || '');
      const sourceId = (payload.source && payload.source.source_id) || ((payload.source_run && payload.source_run.source_id) || '');
      const written = payload.source_run ? payload.source_run.written : '';
      const notesWritten = payload.sync ? payload.sync.notes_written : '';
      const profilePath = payload.profile_path || payload.generated_profile_path || '';
      target.innerHTML = `
        <article class="statement">
          <h3>${{esc(sourceId || roleId || '接入结果')}}</h3>
          <div class="meta"><span>${{esc(roleId || '角色待定')}}</span><span>${{esc(sourceId || '来源待定')}}</span><span>${{esc(written !== '' ? `${{written}} 条写入` : '来源已配置')}}</span><span>${{esc(notesWritten !== '' ? `${{notesWritten}} 条已同步` : '')}}</span></div>
          <p>${{esc(profilePath || '来源配置已准备好，可采集公开发言。')}}</p>
        </article>
      `;
    }}

    function setOnboardingStatus(message, isError, isOk = false) {{
      const target = byId('onboardingStatus');
      if (!target) return;
      target.textContent = message;
      target.className = `answer-status ${{isError ? 'error' : (isOk ? 'ok' : '')}}`;
    }}

    function setOnboardingLoading(loading) {{
      state.onboardingLoading = loading;
      ['accountArchiveSubmit', 'accountArchiveCollectSubmit', 'onboardingSubmit', 'onboardingStatementSubmit'].forEach((id) => {{
        const button = byId(id);
        if (button) button.disabled = loading;
      }});
    }}

    function listFromInput(id) {{
      const input = byId(id);
      if (!input) return [];
      return input.value.split(',').map((item) => item.trim()).filter(Boolean);
    }}

    init();
  </script>
</body>
</html>
"""
