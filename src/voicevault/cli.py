from __future__ import annotations

import argparse
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .accounts import collect_account, create_account, list_accounts, read_account_status
from .app_db import AppDatabase
from .analysis import analyze_event
from .analysis_exports import list_analysis_exports, summarize_analysis_exports
from .answer import (
    ANSWER_EXPORT_STATUS_CHOICES,
    answer_query,
    default_answer_dir,
    filter_answer_exports,
    list_answer_exports,
    prune_answer_exports,
    summarize_answer_exports,
    write_answer_outputs,
)
from .answer_regression import (
    audit_answer_regression,
    export_answer_regression_suite,
    import_answer_regression_suite,
)
from .capture import append_capture_record, build_capture_record
from .collections import create_evidence_pack, list_reports
from .comparison import (
    COMPARISON_EXPORT_STATUS_CHOICES,
    COMPARISON_REVIEW_STATUS_CHOICES,
    COMPARISON_REVIEW_STATUS_FILTER_CHOICES,
    compare_roles,
    default_comparison_dir,
    filter_comparison_exports,
    list_comparison_exports,
    review_comparison_export,
    summarize_comparison_exports,
    write_comparison_outputs,
)
from .dashboard import write_dashboard
from .diagnostics import inspect_kb, repair_kb
from .distribution import write_distribution_package
from .events import create_event, default_export_dir, list_events
from .exporters import write_analysis_outputs
from .guide import write_quickstart_guide
from .importers import load_event, load_statements_from_kb
from .index import VoiceVaultIndex
from .historical_archive_import import HistoricalArchiveImporter
from .kb import KnowledgeBase, init_kb
from .legacy_import import LegacyImporter
from .profile import generate_profile, promote_generated_profile
from .release import check_release_readiness, write_release_bundle, write_release_manifest
from .release_inspect import inspect_release_handoff
from .release_prepare import prepare_release
from .release_ship import ship_release
from .release_verify import verify_ship_manifest
from .role_agent import (
    ask_role_agent,
    audit_role_agent_exports,
    audit_role_agent_readiness,
    inspect_role_agent_runtime,
    list_role_agent_exports,
)
from .role_skill import audit_role_skill_coverage, distill_role_skill, list_role_skills, write_role_skill
from .roles import create_role, evaluate_role_coverage, list_role_summaries
from .routing import suggest_roles
from .samples import preview_sample_removal, remove_sample_content
from .search import search_statements
from .server import create_server
from .runtime import RuntimeRegistry
from .source_jobs import (
    complete_source_job,
    drain_source_jobs,
    enqueue_source_jobs,
    fail_source_job,
    get_source_job,
    read_source_job_status,
    retry_source_job,
)
from .source_imports import (
    import_source_input,
    normalize_source_input,
    read_source_import_status,
    write_source_input_template,
)
from .sources import (
    create_source,
    list_sources,
    read_source_status,
    record_source_run_error,
    run_source,
    summarize_sources,
    validate_source_adapters,
)
from .sync import SyncResult, read_capture_status, read_sync_status, sync_once, validate_capture_path, watch_sync
from .ui import write_local_ui


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voicevault")
    parser.add_argument("--version", action="store_true", help="Show version and exit.")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Initialize a local knowledge base.")
    init_parser.add_argument("--kb", required=True, help="Knowledge base path.")

    ingest_parser = subparsers.add_parser("ingest", help="Read source files and report import counts.")
    ingest_parser.add_argument("--kb", required=True, help="Knowledge base path.")

    build_parser_ = subparsers.add_parser("build", help="Rebuild the local SQLite index.")
    build_parser_.add_argument("--kb", required=True, help="Knowledge base path.")

    dashboard_parser = subparsers.add_parser("dashboard", help="Generate a static local dashboard HTML.")
    dashboard_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    dashboard_parser.add_argument("--out", help="Output directory. Defaults to <kb>\\exports\\dashboard.")
    dashboard_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    ui_parser = subparsers.add_parser("ui", help="Generate a static local UI workbench.")
    ui_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    ui_parser.add_argument("--root", help="Repository root for generated release action commands.")
    ui_parser.add_argument("--out", help="Output directory. Defaults to <kb>\\exports\\ui.")
    ui_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    serve_parser = subparsers.add_parser("serve", help="Run the local browser answer workbench.")
    serve_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    serve_parser.add_argument("--root", help="Repository root for generated release action commands.")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Bind host. Defaults to 127.0.0.1.")
    serve_parser.add_argument("--port", type=int, default=8765, help="Bind port. Defaults to 8765.")
    serve_parser.add_argument("--data-dir", help="VoiceVault runtime data directory.")

    legacy_parser = subparsers.add_parser("legacy", help="Legacy archive migration commands.")
    legacy_subparsers = legacy_parser.add_subparsers(dest="legacy_command")
    legacy_import_parser = legacy_subparsers.add_parser(
        "import", help="Import readable legacy statements into the person archive."
    )
    legacy_import_parser.add_argument("--kb", required=True, help="Legacy knowledge base path.")
    legacy_import_parser.add_argument("--data-dir", help="VoiceVault runtime data directory.")

    archive_parser = subparsers.add_parser("archive", help="Historical post archive commands.")
    archive_subparsers = archive_parser.add_subparsers(dest="archive_command")
    archive_import_parser = archive_subparsers.add_parser(
        "import", help="Preview or import a completed historical Xueqiu post archive."
    )
    archive_import_parser.add_argument("--source", required=True, help="Historical posts.json path.")
    archive_import_parser.add_argument("--person-id", required=True, help="Existing target person ID.")
    archive_import_parser.add_argument("--account-id", required=True, help="Existing target platform account ID.")
    archive_import_parser.add_argument("--data-dir", help="VoiceVault runtime data directory.")
    archive_import_parser.add_argument(
        "--apply", action="store_true", help="Write validated records. Defaults to read-only preview."
    )

    collection_parser = subparsers.add_parser("collection", help="Person-archive collection handoff commands.")
    collection_subparsers = collection_parser.add_subparsers(dest="collection_command")
    collection_claim_parser = collection_subparsers.add_parser("claim", help="Claim a local collection handoff.")
    collection_claim_parser.add_argument("--handoff", required=True, help="Opaque collection handoff ID.")
    collection_claim_parser.add_argument("--collector", default="", help="Opaque collector ID. Generated when omitted.")
    collection_claim_parser.add_argument("--data-dir", help="VoiceVault runtime data directory.")
    collection_submit_parser = collection_subparsers.add_parser(
        "submit", help="Submit a staged local collection result."
    )
    collection_submit_parser.add_argument("--job", required=True, help="Collection job ID.")
    collection_submit_parser.add_argument("--collector", required=True, help="Opaque collector ID.")
    collection_submit_parser.add_argument(
        "--handoff-version", required=True, type=int, help="Claimed handoff version."
    )
    collection_submit_parser.add_argument(
        "--manifest-sha256", required=True, help="Staged manifest SHA-256 digest."
    )
    collection_submit_parser.add_argument("--data-dir", help="VoiceVault runtime data directory.")

    question_parser = subparsers.add_parser(
        "question", help="Current Codex task question-run commands."
    )
    question_subparsers = question_parser.add_subparsers(dest="question_command")
    question_evidence_parser = question_subparsers.add_parser(
        "evidence", help="Read a frozen question evidence bundle."
    )
    question_evidence_parser.add_argument("--run", required=True, help="Question run ID.")
    question_evidence_parser.add_argument("--data-dir", help="VoiceVault runtime data directory.")
    question_submit_parser = question_subparsers.add_parser(
        "submit", help="Submit a structured candidate answer."
    )
    question_submit_parser.add_argument("--run", required=True, help="Question run ID.")
    question_submit_parser.add_argument(
        "--result", required=True, help="Path to a closed ProposedAnswer JSON object."
    )
    question_submit_parser.add_argument("--data-dir", help="VoiceVault runtime data directory.")

    doctor_parser = subparsers.add_parser("doctor", help="Inspect knowledge-base readiness.")
    doctor_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    doctor_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    doctor_parser.add_argument("--repair", action="store_true", help="Create missing required directories.")

    roles_parser = subparsers.add_parser("roles", help="Role commands.")
    roles_subparsers = roles_parser.add_subparsers(dest="roles_command")
    roles_list_parser = roles_subparsers.add_parser("list", help="List role status and statement counts.")
    roles_list_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    roles_list_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    roles_coverage_parser = roles_subparsers.add_parser("coverage", help="Inspect multi-role coverage readiness.")
    roles_coverage_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    roles_coverage_parser.add_argument("--min-roles", type=int, default=2, help="Minimum reviewed roles with statements.")
    roles_coverage_parser.add_argument("--min-statements", type=int, default=1, help="Minimum statements per ready role.")
    roles_coverage_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    roles_create_parser = roles_subparsers.add_parser("create", help="Create a public voice role profile draft.")
    roles_create_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    roles_create_parser.add_argument("--role", required=True, help="Role ID.")
    roles_create_parser.add_argument("--display-name", default="", help="Human-readable display name.")
    roles_create_parser.add_argument("--platform", default="", help="Primary source platform.")
    roles_create_parser.add_argument("--source-url", default="", help="Public source URL.")
    roles_create_parser.add_argument("--tags", default="", help="Comma-separated tags.")
    roles_create_parser.add_argument("--notes", default="", help="Onboarding notes.")
    roles_create_parser.add_argument("--overwrite", action="store_true", help="Overwrite an existing generated profile draft.")
    roles_create_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    role_parser = subparsers.add_parser("role", help="Role Skill and Role Agent commands.")
    role_subparsers = role_parser.add_subparsers(dest="role_command")
    role_distill_parser = role_subparsers.add_parser("distill", help="Distill a role into a reusable Role Skill.")
    role_distill_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    role_distill_parser.add_argument("--role", required=True, help="Role ID.")
    role_distill_parser.add_argument("--limit", type=int, default=12, help="Maximum representative statements in the skill.")
    role_distill_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    role_skills_parser = role_subparsers.add_parser("skills", help="List Role Skill coverage.")
    role_skills_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    role_skills_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    role_ask_parser = role_subparsers.add_parser("ask", help="Build or run a Role Agent prompt for an external LLM.")
    role_ask_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    role_ask_parser.add_argument("--role", required=True, help="Role ID.")
    role_ask_parser.add_argument("--query", required=True, help="User question.")
    role_ask_parser.add_argument("--symbol", default="", help="Optional symbol filter.")
    role_ask_parser.add_argument("--topic", default="", help="Optional topic filter.")
    role_ask_parser.add_argument("--limit", type=int, default=5, help="Evidence limit.")
    role_ask_parser.add_argument("--model", default="", help="External LLM model name.")
    role_ask_parser.add_argument("--temperature", type=float, default=0.2, help="External LLM temperature.")
    role_ask_parser.add_argument("--dry-run", action="store_true", help="Write the prompt bundle without calling an LLM.")
    role_ask_parser.add_argument("--call-llm", action="store_true", help="Call the configured external LLM.")
    role_ask_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    role_agents_parser = role_subparsers.add_parser("agents", help="List Role Agent exports and quality audit.")
    role_agents_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    role_agents_parser.add_argument(
        "--status",
        choices=["all", "prompt_only", "completed", "failed", "malformed"],
        default="all",
        help="Export status filter.",
    )
    role_agents_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    role_readiness_parser = role_subparsers.add_parser("readiness", help="Audit per-role Role Agent live readiness.")
    role_readiness_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    role_readiness_parser.add_argument("--require-live", action="store_true", help="Require a deliverable live LLM answer per ready role.")
    role_readiness_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    sources_parser = subparsers.add_parser("sources", help="Capture source configuration commands.")
    sources_subparsers = sources_parser.add_subparsers(dest="sources_command")
    sources_create_parser = sources_subparsers.add_parser("create", help="Create or update a capture source config.")
    sources_create_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    sources_create_parser.add_argument("--source", required=True, help="Source config ID.")
    sources_create_parser.add_argument("--role", required=True, help="Role ID this source feeds.")
    sources_create_parser.add_argument("--platform", required=True, help="Source platform.")
    sources_create_parser.add_argument("--source-url", default="", help="Public source URL.")
    sources_create_parser.add_argument("--display-name", default="", help="Human-readable source name.")
    sources_create_parser.add_argument("--adapter", default="manual", help="Adapter name. Defaults to manual.")
    sources_create_parser.add_argument("--adapter-config", default="", help="Adapter config JSON object.")
    sources_create_parser.add_argument("--symbols", default="", help="Comma-separated default symbols.")
    sources_create_parser.add_argument("--topics", default="", help="Comma-separated default topics.")
    sources_create_parser.add_argument("--tags", default="", help="Comma-separated source tags.")
    sources_create_parser.add_argument("--cadence", default="", help="Collection cadence label.")
    sources_create_parser.add_argument("--notes", default="", help="Source notes.")
    sources_create_parser.add_argument("--disabled", action="store_true", help="Create source as disabled.")
    sources_create_parser.add_argument("--overwrite", action="store_true", help="Overwrite an existing source config.")
    sources_create_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    sources_list_parser = sources_subparsers.add_parser("list", help="List capture source configs.")
    sources_list_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    sources_list_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    sources_status_parser = sources_subparsers.add_parser("status", help="Show source adapter run status.")
    sources_status_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    sources_status_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    sources_validate_parser = sources_subparsers.add_parser("validate", help="Validate source adapter configs.")
    sources_validate_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    sources_validate_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    sources_template_parser = sources_subparsers.add_parser("template", help="Write a public source input template.")
    sources_template_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    sources_template_parser.add_argument("--source", required=True, help="Source config ID.")
    sources_template_parser.add_argument("--format", choices=["csv", "jsonl", "json"], default="csv", help="Template format.")
    sources_template_parser.add_argument("--out", help="Output path. Defaults to <kb>\\inbox\\exports\\<source>-public-feed.<format>.")
    sources_template_parser.add_argument("--overwrite", action="store_true", help="Overwrite an existing template file.")
    sources_template_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    sources_normalize_parser = sources_subparsers.add_parser("normalize", help="Normalize a local public export file for a source adapter.")
    sources_normalize_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    sources_normalize_parser.add_argument("--source", required=True, help="Source config ID.")
    sources_normalize_parser.add_argument("--input", required=True, help="Local CSV, JSONL, or JSON public export file.")
    sources_normalize_parser.add_argument("--out", help="Output JSONL path. Defaults to <kb>\\inbox\\adapter-fixtures\\<source>.jsonl.")
    sources_normalize_parser.add_argument("--dry-run", action="store_true", help="Normalize records without writing the output file.")
    sources_normalize_parser.add_argument("--update-source", action="store_true", help="Point the source config at the normalized local-jsonl file.")
    sources_normalize_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    sources_import_parser = sources_subparsers.add_parser("import", help="Normalize and preflight a public source export.")
    sources_import_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    sources_import_parser.add_argument("--source", required=True, help="Source config ID.")
    sources_import_parser.add_argument("--input", required=True, help="Local CSV, JSONL, or JSON public export file.")
    sources_import_parser.add_argument("--out", help="Normalized JSONL path. Defaults to <kb>\\inbox\\adapter-fixtures\\<source>.jsonl.")
    sources_import_parser.add_argument("--dry-run", action="store_true", help="Validate import without writing fixture, updating source, or recording source run.")
    sources_import_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    sources_imports_parser = sources_subparsers.add_parser("imports", help="Show source import/preflight status.")
    sources_imports_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    sources_imports_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    sources_enqueue_parser = sources_subparsers.add_parser("enqueue", help="Enqueue source collection jobs.")
    sources_enqueue_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    sources_enqueue_parser.add_argument("--source", default="", help="Optional source config ID. Defaults to all active sources.")
    sources_enqueue_parser.add_argument("--due-at", default="", help="Optional due timestamp or label.")
    sources_enqueue_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    sources_jobs_parser = sources_subparsers.add_parser("jobs", help="List source collection jobs.")
    sources_jobs_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    sources_jobs_parser.add_argument("--status", choices=["all", "pending", "completed", "failed"], default="all", help="Job status filter.")
    sources_jobs_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    sources_drain_parser = sources_subparsers.add_parser("drain", help="Run pending source collection jobs.")
    sources_drain_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    sources_drain_parser.add_argument("--limit", type=int, default=0, help="Maximum pending jobs to run. Defaults to all.")
    sources_drain_parser.add_argument("--dry-run", action="store_true", help="Run adapters without writing capture files.")
    sources_drain_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    sources_retry_parser = sources_subparsers.add_parser("retry", help="Retry a failed source collection job.")
    sources_retry_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    sources_retry_parser.add_argument("--job", required=True, help="Failed source job ID to retry.")
    sources_retry_parser.add_argument("--due-at", default="", help="Optional replacement due timestamp or label.")
    sources_retry_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    sources_run_parser = sources_subparsers.add_parser("run", help="Write one capture record from a source config.")
    sources_run_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    sources_run_parser.add_argument("--source", default="", help="Source config ID. Optional when --job is provided.")
    sources_run_parser.add_argument("--job", default="", help="Optional source job ID to complete or fail.")
    sources_run_parser.add_argument("--text", default="", help="Captured public statement text. Required by the manual adapter.")
    sources_run_parser.add_argument("--title", default="", help="Capture title.")
    sources_run_parser.add_argument("--source-url", default="", help="Statement URL override.")
    sources_run_parser.add_argument("--published-at", default="", help="Published timestamp.")
    sources_run_parser.add_argument("--captured-at", default="", help="Captured timestamp.")
    sources_run_parser.add_argument("--symbols", default="", help="Comma-separated symbol override.")
    sources_run_parser.add_argument("--topics", default="", help="Comma-separated topic override.")
    sources_run_parser.add_argument("--stance", default="unclear", help="Optional stance.")
    sources_run_parser.add_argument("--time-horizon", default="unknown", help="Optional time horizon.")
    sources_run_parser.add_argument("--confidence", default="low", help="Optional confidence.")
    sources_run_parser.add_argument("--notes", default="", help="Optional notes.")
    sources_run_parser.add_argument("--out", help="Output JSONL path. Defaults to <kb>\\inbox\\captures\\source-<source>.jsonl.")
    sources_run_parser.add_argument("--dry-run", action="store_true", help="Build the capture record without writing a file.")
    sources_run_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    accounts_parser = subparsers.add_parser("accounts", help="Platform account archive commands.")
    accounts_subparsers = accounts_parser.add_subparsers(dest="accounts_command")
    accounts_create_parser = accounts_subparsers.add_parser("create", help="Create or update a platform account archive.")
    accounts_create_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    accounts_create_parser.add_argument("--account", required=True, help="VoiceVault account config ID.")
    accounts_create_parser.add_argument("--platform", required=True, help="Platform key.")
    accounts_create_parser.add_argument("--platform-account-id", required=True, help="Account/user ID on the source platform.")
    accounts_create_parser.add_argument("--role", required=True, help="Role ID this account feeds.")
    accounts_create_parser.add_argument("--source", default="", help="Optional source config ID. Defaults to account-<account>.")
    accounts_create_parser.add_argument("--source-url", default="", help="Public source URL.")
    accounts_create_parser.add_argument("--display-name", default="", help="Human-readable account/source name.")
    accounts_create_parser.add_argument("--mode", default="auto", help="Collection mode: auto, rss, local-export, custom-api, blocked.")
    accounts_create_parser.add_argument("--feed-url", default="", help="RSS or Atom feed URL/path for allowed feed collection.")
    accounts_create_parser.add_argument("--input", default="", help="Local CSV, JSON, JSONL, HTML, or text export path.")
    accounts_create_parser.add_argument("--api-url", default="", help="Authorized JSON API endpoint.")
    accounts_create_parser.add_argument("--adapter-config", default="", help="Additional adapter config JSON object.")
    accounts_create_parser.add_argument("--symbols", default="", help="Comma-separated default symbols.")
    accounts_create_parser.add_argument("--topics", default="", help="Comma-separated default topics.")
    accounts_create_parser.add_argument("--tags", default="", help="Comma-separated account tags.")
    accounts_create_parser.add_argument("--notes", default="", help="Account notes.")
    accounts_create_parser.add_argument("--disabled", action="store_true", help="Create account as disabled.")
    accounts_create_parser.add_argument("--overwrite", action="store_true", help="Overwrite an existing account/source config.")
    accounts_create_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    accounts_list_parser = accounts_subparsers.add_parser("list", help="List platform account archives.")
    accounts_list_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    accounts_list_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    accounts_status_parser = accounts_subparsers.add_parser("status", help="Show platform account archive status.")
    accounts_status_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    accounts_status_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    accounts_collect_parser = accounts_subparsers.add_parser("collect", help="Collect one platform account archive.")
    accounts_collect_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    accounts_collect_parser.add_argument("--account", required=True, help="Account config ID to collect.")
    accounts_collect_parser.add_argument("--dry-run", action="store_true", help="Collect without writing capture records.")
    accounts_collect_parser.add_argument("--sync", action="store_true", help="Sync capture inbox after successful collection.")
    accounts_collect_parser.add_argument("--archive", action="store_true", help="Archive processed capture files when syncing.")
    accounts_collect_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    event_parser = subparsers.add_parser("event", help="Event commands.")
    event_subparsers = event_parser.add_subparsers(dest="event_command")
    event_list_parser = event_subparsers.add_parser("list", help="List event Markdown files.")
    event_list_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    event_list_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    event_create_parser = event_subparsers.add_parser("create", help="Create an event Markdown template.")
    event_create_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    event_create_parser.add_argument("--event-id", required=True, help="Event ID and output filename stem.")
    event_create_parser.add_argument("--title", required=True, help="Event title.")
    event_create_parser.add_argument("--date", required=True, help="Event date, YYYY-MM-DD.")
    event_create_parser.add_argument("--symbols", default="", help="Comma-separated symbols.")
    event_create_parser.add_argument("--topics", default="", help="Comma-separated topics.")
    event_create_parser.add_argument("--summary", default="", help="Event summary body.")
    event_create_parser.add_argument("--overwrite", action="store_true", help="Overwrite an existing event file.")

    profile_parser = subparsers.add_parser("profile", help="Role profile commands.")
    profile_subparsers = profile_parser.add_subparsers(dest="profile_command")
    generate_parser = profile_subparsers.add_parser("generate", help="Generate profile.generated.md.")
    generate_parser.add_argument("--role", required=True, help="Role ID.")
    generate_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    promote_parser = profile_subparsers.add_parser("promote", help="Promote profile.generated.md to reviewed profile.md.")
    promote_parser.add_argument("--role", required=True, help="Role ID.")
    promote_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    promote_parser.add_argument("--overwrite", action="store_true", help="Overwrite an existing profile.md.")
    promote_parser.add_argument("--reviewer", default="manual", help="Reviewer recorded in promoted profile frontmatter.")
    promote_parser.add_argument("--note", default="", help="Review note recorded in promoted profile frontmatter.")
    promote_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    analyze_parser = subparsers.add_parser("analyze", help="Analyze a market event through roles.")
    analyze_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    analyze_parser.add_argument("--event", required=True, help="Event Markdown path.")
    analyze_parser.add_argument("--roles", default="all", help="all or comma-separated role IDs.")
    analyze_parser.add_argument("--out", help="Output directory. Defaults to <kb>\\exports\\<event_id>.")
    analyze_parser.add_argument("--json", action="store_true", help="Print machine-readable output paths.")

    search_parser = subparsers.add_parser("search", help="Search indexed statements.")
    search_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    search_parser.add_argument("--query", required=True, help="Keyword query.")
    search_parser.add_argument("--role", default="", help="Optional role ID filter.")
    search_parser.add_argument("--symbol", default="", help="Optional symbol filter.")
    search_parser.add_argument("--topic", default="", help="Optional topic filter.")
    search_parser.add_argument("--limit", type=int, default=10, help="Maximum results.")
    search_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    route_parser = subparsers.add_parser("route", help="Suggest roles for a query from local evidence.")
    route_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    route_parser.add_argument("--query", required=True, help="Question or keyword query.")
    route_parser.add_argument("--symbol", default="", help="Optional symbol filter.")
    route_parser.add_argument("--topic", default="", help="Optional topic filter.")
    route_parser.add_argument("--limit", type=int, default=5, help="Maximum role suggestions.")
    route_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    compare_parser = subparsers.add_parser("compare", help="Compare role answers from cited local evidence.")
    compare_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    compare_parser.add_argument("--query", required=True, help="Question or keyword query.")
    compare_parser.add_argument("--roles", default="auto", help="auto, all, or comma-separated role IDs.")
    compare_parser.add_argument("--symbol", default="", help="Optional symbol filter.")
    compare_parser.add_argument("--topic", default="", help="Optional topic filter.")
    compare_parser.add_argument("--limit", type=int, default=3, help="Maximum roles to compare.")
    compare_parser.add_argument("--evidence-limit", type=int, default=3, help="Maximum evidence items per role.")
    compare_parser.add_argument("--out", help="Output directory. Defaults to <kb>\\exports\\comparisons\\<query-slug>.")
    compare_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    answer_parser = subparsers.add_parser("answer", help="Answer a query from cited local evidence.")
    answer_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    answer_parser.add_argument("--query", required=True, help="Question or keyword query.")
    answer_parser.add_argument("--role", default="", help="Optional role ID filter.")
    answer_parser.add_argument("--symbol", default="", help="Optional symbol filter.")
    answer_parser.add_argument("--topic", default="", help="Optional topic filter.")
    answer_parser.add_argument("--limit", type=int, default=5, help="Maximum evidence items.")
    answer_parser.add_argument("--out", help="Output directory. Defaults to <kb>\\exports\\answers\\<query-slug>.")
    answer_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    answers_parser = subparsers.add_parser("answers", help="Answer export lifecycle commands.")
    answers_subparsers = answers_parser.add_subparsers(dest="answers_command")
    answers_list_parser = answers_subparsers.add_parser("list", help="List generated answer exports.")
    answers_list_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    answers_list_parser.add_argument("--status", choices=ANSWER_EXPORT_STATUS_CHOICES, default="all", help="Answer export status filter.")
    answers_list_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    answers_prune_parser = answers_subparsers.add_parser("prune", help="Preview or remove answer exports by status.")
    answers_prune_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    answers_prune_parser.add_argument("--status", choices=ANSWER_EXPORT_STATUS_CHOICES, default="invalid", help="Answer export status filter.")
    answers_prune_parser.add_argument("--dry-run", action="store_true", help="Preview matching exports without deleting them.")
    answers_prune_parser.add_argument("--yes", action="store_true", help="Delete matching exports instead of previewing.")
    answers_prune_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    evaluations_parser = subparsers.add_parser("evaluations", help="Evaluation and regression audit commands.")
    evaluations_subparsers = evaluations_parser.add_subparsers(dest="evaluations_command")
    evaluations_answers_parser = evaluations_subparsers.add_parser("answers", help="Audit fixed answer regression questions.")
    evaluations_answers_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    evaluations_answers_parser.add_argument("--suite", default="", help="Optional questions.json suite path.")
    evaluations_answers_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    evaluations_export_parser = evaluations_subparsers.add_parser("export", help="Export fixed answer regression questions.")
    evaluations_export_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    evaluations_export_parser.add_argument("--suite", default="", help="Optional source questions.json suite path.")
    evaluations_export_parser.add_argument("--out", required=True, help="Output JSON path.")
    evaluations_export_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    evaluations_import_parser = evaluations_subparsers.add_parser("import", help="Import fixed answer regression questions.")
    evaluations_import_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    evaluations_import_parser.add_argument("--input", required=True, help="Input suite export JSON path.")
    evaluations_import_parser.add_argument("--dry-run", action="store_true", help="Preview batch changes without writing questions.json.")
    evaluations_import_parser.add_argument("--yes", action="store_true", help="Apply the batch import.")
    evaluations_import_parser.add_argument("--updated-by", default="local-cli", help="Actor recorded in the answer regression changelog.")
    evaluations_import_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    comparisons_parser = subparsers.add_parser("comparisons", help="Comparison export review commands.")
    comparisons_subparsers = comparisons_parser.add_subparsers(dest="comparisons_command")
    comparisons_list_parser = comparisons_subparsers.add_parser("list", help="List generated comparison exports.")
    comparisons_list_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    comparisons_list_parser.add_argument("--status", choices=COMPARISON_EXPORT_STATUS_CHOICES, default="all", help="Comparison export status filter.")
    comparisons_list_parser.add_argument("--review-status", choices=COMPARISON_REVIEW_STATUS_FILTER_CHOICES, default="all", help="Comparison review status filter.")
    comparisons_list_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    comparisons_review_parser = comparisons_subparsers.add_parser("review", help="Mark a comparison export reviewed, adopted, rejected, or draft.")
    comparisons_review_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    comparisons_review_parser.add_argument("--query", default="", help="Comparison query slug source. Defaults to --path when provided.")
    comparisons_review_parser.add_argument("--path", default="", help="Direct comparison.json path.")
    comparisons_review_parser.add_argument("--status", choices=COMPARISON_REVIEW_STATUS_CHOICES, required=True, help="Review status to write.")
    comparisons_review_parser.add_argument("--reviewer", default="manual", help="Reviewer recorded in comparison.json.")
    comparisons_review_parser.add_argument("--notes", default="", help="Review notes recorded in comparison.json.")
    comparisons_review_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    collect_parser = subparsers.add_parser("collect", help="Create a Markdown evidence pack from search results.")
    collect_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    collect_parser.add_argument("--title", required=True, help="Evidence pack title.")
    collect_parser.add_argument("--query", required=True, help="Keyword query.")
    collect_parser.add_argument("--role", default="", help="Optional role ID filter.")
    collect_parser.add_argument("--symbol", default="", help="Optional symbol filter.")
    collect_parser.add_argument("--topic", default="", help="Optional topic filter.")
    collect_parser.add_argument("--limit", type=int, default=20, help="Maximum evidence items.")
    collect_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    reports_parser = subparsers.add_parser("reports", help="Report commands.")
    reports_subparsers = reports_parser.add_subparsers(dest="reports_command")
    reports_list_parser = reports_subparsers.add_parser("list", help="List generated Markdown reports.")
    reports_list_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    reports_list_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    analyses_parser = subparsers.add_parser("analyses", help="Analysis export discovery commands.")
    analyses_subparsers = analyses_parser.add_subparsers(dest="analyses_command")
    analyses_list_parser = analyses_subparsers.add_parser("list", help="List generated event analysis exports.")
    analyses_list_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    analyses_list_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    release_parser = subparsers.add_parser("release", help="Release readiness commands.")
    release_subparsers = release_parser.add_subparsers(dest="release_command")
    release_check_parser = release_subparsers.add_parser("check", help="Check local product readiness.")
    release_check_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    release_check_parser.add_argument(
        "--require-live-role-agent",
        action="store_true",
        help="Fail unless every ready role has a deliverable external-LLM Role Agent answer.",
    )
    release_check_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    release_manifest_parser = release_subparsers.add_parser("manifest", help="Write a release manifest JSON.")
    release_manifest_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    release_manifest_parser.add_argument("--out", help="Output directory. Defaults to <kb>\\exports\\release.")
    release_manifest_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    release_bundle_parser = release_subparsers.add_parser("bundle", help="Write a release handoff bundle.")
    release_bundle_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    release_bundle_parser.add_argument("--root", help="Repository root for generated release handoff commands. Defaults to the current working directory.")
    release_bundle_parser.add_argument("--out", help="Output directory. Defaults to <kb>\\exports\\release\\voicevault-v<version>.")
    release_bundle_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    release_prepare_parser = release_subparsers.add_parser("prepare", help="Run the standard pre-release workflow.")
    release_prepare_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    release_prepare_parser.add_argument("--root", help="Repository root for generated quickstart paths. Defaults to the current working directory.")
    release_prepare_parser.add_argument("--out", help="Output directory. Defaults to <kb>\\exports\\release\\voicevault-v<version>.")
    release_prepare_parser.add_argument("--skip-drain", action="store_true", help="Do not drain pending source jobs.")
    release_prepare_parser.add_argument("--execute-jobs", action="store_true", help="Drain pending source jobs and write capture files.")
    release_prepare_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    release_package_parser = release_subparsers.add_parser("package", help="Write a VoiceVault CLI distribution package.")
    release_package_parser.add_argument("--root", help="Repository root. Defaults to the current working directory.")
    release_package_parser.add_argument("--out", help="Output directory. Defaults to <root>\\dist.")
    release_package_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    release_ship_parser = release_subparsers.add_parser("ship", help="Write the final release handoff manifest.")
    release_ship_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    release_ship_parser.add_argument("--root", help="Repository root. Defaults to the current working directory.")
    release_ship_parser.add_argument("--out", help="Output directory. Defaults to <root>\\dist.")
    release_ship_parser.add_argument("--skip-drain", action="store_true", help="Do not drain pending source jobs.")
    release_ship_parser.add_argument("--execute-jobs", action="store_true", help="Drain pending source jobs and write capture files.")
    release_ship_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    release_verify_parser = release_subparsers.add_parser("verify", help="Verify a final release ship manifest.")
    release_verify_parser.add_argument("--manifest", required=True, help="Ship manifest JSON path.")
    release_verify_parser.add_argument("--out", help="Verification report path. Defaults to <manifest>-verification-report.json.")
    release_verify_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    release_inspect_parser = release_subparsers.add_parser("inspect", help="Inspect final release handoff artifact status.")
    release_inspect_parser.add_argument("--manifest", required=True, help="Ship manifest JSON path.")
    release_inspect_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    release_audit_parser = release_subparsers.add_parser("audit", help="Audit a final release ship manifest without writing artifacts.")
    release_audit_parser.add_argument("--manifest", required=True, help="Ship manifest JSON path.")
    release_audit_parser.add_argument("--summary", action="store_true", help="Print a compact Markdown audit summary.")
    release_audit_parser.add_argument("--summary-out", help="Write the Markdown audit summary to this path.")
    release_audit_parser.add_argument(
        "--summary-check",
        help="Verify an archived Markdown audit summary matches the current audit result.",
    )
    release_audit_parser.add_argument(
        "--summary-check-out",
        help="Write JSON evidence for --summary-check to this path.",
    )
    release_audit_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    guide_parser = subparsers.add_parser("guide", help="Guided operator workflows.")
    guide_subparsers = guide_parser.add_subparsers(dest="guide_command")
    guide_quickstart_parser = guide_subparsers.add_parser("quickstart", help="Write a first-use quickstart guide.")
    guide_quickstart_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    guide_quickstart_parser.add_argument("--root", help="Repository root. Defaults to the current working directory.")
    guide_quickstart_parser.add_argument("--out", help="Output directory. Defaults to <kb>\\exports\\guide.")
    guide_quickstart_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    sample_parser = subparsers.add_parser("sample", help="Sample content commands.")
    sample_subparsers = sample_parser.add_subparsers(dest="sample_command")
    sample_remove_parser = sample_subparsers.add_parser("remove", help="Remove seeded sample content and rebuild index.")
    sample_remove_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    sample_remove_parser.add_argument("--dry-run", action="store_true", help="Show what would be removed without changing files.")
    sample_remove_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    capture_parser = subparsers.add_parser("capture", help="Capture inbox commands.")
    capture_subparsers = capture_parser.add_subparsers(dest="capture_command")
    capture_status_parser = capture_subparsers.add_parser("status", help="Show capture inbox lifecycle status.")
    capture_status_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    capture_status_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    capture_validate_parser = capture_subparsers.add_parser("validate", help="Validate capture JSON or JSONL without syncing.")
    capture_validate_parser.add_argument("--path", required=True, help="Capture file or directory to validate.")
    capture_validate_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    capture_append_parser = capture_subparsers.add_parser("append", help="Append one normalized capture record to JSONL.")
    capture_append_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    capture_append_parser.add_argument("--out", help="Output JSONL path. Defaults to <kb>\\inbox\\captures\\manual.jsonl.")
    capture_append_parser.add_argument("--role", required=True, help="Role ID.")
    capture_append_parser.add_argument("--platform", required=True, help="Source platform.")
    capture_append_parser.add_argument("--text", required=True, help="Capture text/body.")
    capture_append_parser.add_argument("--url", default="", help="Source URL.")
    capture_append_parser.add_argument("--title", default="", help="Capture title.")
    capture_append_parser.add_argument("--author", default="", help="Source author display name.")
    capture_append_parser.add_argument("--user-id", default="", help="Source platform user ID.")
    capture_append_parser.add_argument("--published-at", default="", help="Published timestamp.")
    capture_append_parser.add_argument("--captured-at", default="", help="Captured timestamp.")
    capture_append_parser.add_argument("--symbols", default="", help="Comma-separated symbols.")
    capture_append_parser.add_argument("--topics", default="", help="Comma-separated topics.")
    capture_append_parser.add_argument("--statement-id", default="", help="Optional source statement ID.")
    capture_append_parser.add_argument("--stance", default="unclear", help="Optional stance.")
    capture_append_parser.add_argument("--time-horizon", default="unknown", help="Optional time horizon.")
    capture_append_parser.add_argument("--confidence", default="low", help="Optional confidence.")
    capture_append_parser.add_argument("--notes", default="", help="Optional notes.")
    capture_append_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    sync_parser = subparsers.add_parser("sync", help="Sync capture inbox into Obsidian Markdown and rebuild index.")
    sync_parser.add_argument("--kb", required=True, help="Knowledge base path.")
    sync_parser.add_argument("--watch", action="store_true", help="Continuously poll inbox\\captures.")
    sync_parser.add_argument("--interval", type=float, default=5.0, help="Watch interval in seconds.")
    sync_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    sync_parser.add_argument("--status", action="store_true", help="Print the latest sync status without running sync.")
    sync_parser.add_argument("--archive", action="store_true", help="Move successfully processed capture files to inbox\\archive.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        from . import __version__

        print(__version__)
        return 0
    if not args.command:
        parser.print_help()
        return 0

    try:
        return _run(args)
    except Exception as exc:
        print(f"voicevault: {exc}")
        return 1


def _run(args: argparse.Namespace) -> int:
    if args.command == "init":
        kb = init_kb(args.kb)
        print(f"Initialized knowledge base: {kb.root}")
        return 0
    if args.command == "ingest":
        kb = KnowledgeBase.from_path(args.kb)
        statements = load_statements_from_kb(kb)
        roles = sorted({statement.role_id for statement in statements})
        print(f"Imported {len(statements)} statements from {len(roles)} role(s).")
        return 0
    if args.command == "build":
        kb = KnowledgeBase.from_path(args.kb)
        statements = load_statements_from_kb(kb)
        count = VoiceVaultIndex(kb).rebuild(statements)
        print(f"Indexed {count} statements: {kb.index_path}")
        return 0
    if args.command == "dashboard":
        kb = KnowledgeBase.from_path(args.kb)
        path = write_dashboard(kb, out_dir=Path(args.out) if args.out else None)
        if args.json:
            print(json.dumps({"kind": "static_html", "path": str(path)}, ensure_ascii=False))
            return 0
        print(f"Wrote dashboard: {path}")
        return 0
    if args.command == "ui":
        kb = KnowledgeBase.from_path(args.kb)
        path = write_local_ui(
            kb,
            out_dir=Path(args.out) if args.out else None,
            repo_root=Path(args.root) if args.root else None,
        )
        data_path = path.with_name("data.json")
        if args.json:
            print(
                json.dumps(
                    {"kind": "static_ui", "index_html": str(path), "data_json": str(data_path)},
                    ensure_ascii=False,
                )
            )
            return 0
        print(f"Wrote local UI: {path}")
        print(f"Wrote UI data: {data_path}")
        return 0
    if args.command == "serve":
        kb = KnowledgeBase.from_path(args.kb)
        app_database = AppDatabase(data_dir=Path(args.data_dir) if args.data_dir else None)
        runtime_registry = RuntimeRegistry(data_dir=Path(args.data_dir) if args.data_dir else None)
        server = create_server(
            kb,
            host=args.host,
            port=args.port,
            repo_root=Path(args.root) if args.root else None,
            app_database=app_database,
            runtime_registry=runtime_registry,
        )
        host, port = server.server_address
        print(f"VoiceVault local workbench: http://{host}:{port}/")
        print(f"Knowledge base: {kb.root}")
        print(f"UI: {server.ui_index}")
        print(f"Runtime: {runtime_registry.path}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("Stopped VoiceVault local workbench.")
        finally:
            server.server_close()
        return 0
    if args.command == "legacy" and args.legacy_command == "import":
        database = AppDatabase(data_dir=Path(args.data_dir) if args.data_dir else None)
        database.initialize()
        report = LegacyImporter(database).import_kb(args.kb)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "archive" and args.archive_command == "import":
        database = AppDatabase(data_dir=Path(args.data_dir) if args.data_dir else None)
        database.initialize()
        importer = HistoricalArchiveImporter(database)
        if args.apply:
            report = importer.import_archive(
                args.source, person_id=args.person_id, account_id=args.account_id
            )
        else:
            report = {
                "status": "preview",
                **importer.preview(
                    args.source, person_id=args.person_id, account_id=args.account_id
                ),
            }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "collection" and args.collection_command == "claim":
        collector_id = args.collector.strip() or f"collector-{secrets.token_urlsafe(18)}"
        registry = RuntimeRegistry(data_dir=Path(args.data_dir) if args.data_dir else None)
        runtime = registry.discover()
        payload = _post_local_json(
            f"{runtime.base_url}/api/collection-handoffs/{quote(args.handoff, safe='')}/claim",
            {"collector_id": collector_id},
        )
        job_id = _safe_collection_job_id(payload.get("job", {}).get("job_id"))
        exchange_dir = (registry.path.parent.absolute() / "jobs" / job_id / "out")
        print(
            json.dumps(
                {
                    "collector_id": collector_id,
                    "exchange_dir": str(exchange_dir),
                    "job": payload["job"],
                    "manifest": payload["manifest"],
                    "lease": payload["lease"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "collection" and args.collection_command == "submit":
        registry = RuntimeRegistry(data_dir=Path(args.data_dir) if args.data_dir else None)
        runtime = registry.discover()
        payload = _post_local_json(
            f"{runtime.base_url}/api/collection-jobs/{quote(args.job, safe='')}/submit",
            {
                "collector_id": args.collector,
                "handoff_version": args.handoff_version,
                "manifest_sha256": args.manifest_sha256,
            },
        )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "question" and args.question_command == "evidence":
        registry = RuntimeRegistry(data_dir=Path(args.data_dir) if args.data_dir else None)
        runtime = registry.discover()
        payload = _get_local_json(
            f"{runtime.base_url}/api/question-runs/{quote(args.run, safe='')}/evidence"
        )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "question" and args.question_command == "submit":
        registry = RuntimeRegistry(data_dir=Path(args.data_dir) if args.data_dir else None)
        runtime = registry.discover()
        try:
            payload = json.loads(Path(args.result).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Question result file must contain valid JSON.") from None
        if not isinstance(payload, dict):
            raise ValueError("Question result file must contain a JSON object.")
        response = _post_local_json(
            f"{runtime.base_url}/api/question-runs/{quote(args.run, safe='')}/answer",
            payload,
        )
        print(json.dumps(response, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "doctor":
        kb = KnowledgeBase.from_path(args.kb)
        report = repair_kb(kb) if args.repair else inspect_kb(kb)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"Knowledge base: {report['root']}")
            print(f"Index: {report['index_path']}")
            print(f"Roles: {report['role_count']}")
            print(f"Statements: {report['statement_count']}")
            if report.get("created_dirs"):
                print("Created directories:")
                for created_dir in report["created_dirs"]:
                    print(f"- {created_dir}")
            if report["warnings"]:
                print("Warnings:")
                for warning in report["warnings"]:
                    print(f"- {warning}")
            else:
                print("Status: ok")
        return 0 if report["ok"] else 1
    if args.command == "roles" and args.roles_command == "list":
        kb = KnowledgeBase.from_path(args.kb)
        summaries = list_role_summaries(kb)
        if args.json:
            print(json.dumps(summaries, ensure_ascii=False, indent=2))
        else:
            for summary in summaries:
                print(f"{summary['role_id']}\t{summary['profile_status']}\t{summary['statement_count']} statements")
        return 0
    if args.command == "roles" and args.roles_command == "coverage":
        kb = KnowledgeBase.from_path(args.kb)
        coverage = evaluate_role_coverage(
            kb,
            min_reviewed_roles=args.min_roles,
            min_statements_per_role=args.min_statements,
        )
        if args.json:
            print(json.dumps(coverage, ensure_ascii=False, indent=2))
        else:
            status = "ok" if coverage["ok"] else "needs attention"
            print(f"Role coverage: {status}")
            print(
                f"Ready roles: {coverage['reviewed_roles_with_statements']} / "
                f"{coverage['min_reviewed_roles']}"
            )
            for role in coverage["roles"]:
                print(
                    f"{role['coverage_status']}\t{role['role_id']}\t"
                    f"{role['profile_status']}\t{role['statement_count']} statements"
                )
            if not coverage["ok"]:
                print("Remediation:")
                for command in coverage["remediation"]:
                    print(f"- {command}")
        return 0 if coverage["ok"] else 1
    if args.command == "roles" and args.roles_command == "create":
        kb = KnowledgeBase.from_path(args.kb)
        result = create_role(
            kb,
            role_id=args.role,
            display_name=args.display_name,
            platform=args.platform,
            source_url=args.source_url,
            tags=_split_cli_list(args.tags),
            notes=args.notes,
            overwrite=args.overwrite,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print(f"Created role draft: {result['generated_profile_path']}")
        return 0
    if args.command == "role" and args.role_command == "distill":
        kb = KnowledgeBase.from_path(args.kb)
        skill = distill_role_skill(kb, args.role, limit=args.limit)
        output = write_role_skill(kb, skill)
        payload = {"ok": True, "skill": skill, "skill_json": str(output["skill_json"]), "skill_markdown": str(output["skill_markdown"])}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        print(f"Wrote Role Skill JSON: {output['skill_json']}")
        print(f"Wrote Role Skill Markdown: {output['skill_markdown']}")
        return 0
    if args.command == "role" and args.role_command == "skills":
        kb = KnowledgeBase.from_path(args.kb)
        coverage = audit_role_skill_coverage(kb)
        result = {"coverage": coverage, **list_role_skills(kb)}
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if coverage["ok"] else 1
        print(f"Role Skills: {coverage['summary']['ready']} ready, {coverage['summary']['missing']} missing.")
        for item in coverage["missing_roles"]:
            print(f"missing\t{item['role_id']}\t{item['remediation']}")
        return 0 if coverage["ok"] else 1
    if args.command == "role" and args.role_command == "ask":
        kb = KnowledgeBase.from_path(args.kb)
        result = ask_role_agent(
            kb,
            args.role,
            args.query,
            symbol=args.symbol,
            topic=args.topic,
            limit=args.limit,
            dry_run=(args.dry_run or not args.call_llm),
            model=args.model,
            temperature=args.temperature,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
        print(f"Wrote Role Agent JSON: {result['role_agent_json']}")
        print(f"Wrote Role Agent Markdown: {result['role_agent_markdown']}")
        print(f"LLM status: {result['llm']['status']}")
        return 0 if result["ok"] else 1
    if args.command == "role" and args.role_command == "agents":
        kb = KnowledgeBase.from_path(args.kb)
        quality = audit_role_agent_exports(kb)
        readiness = audit_role_agent_readiness(kb)
        result = {
            "schema_version": 1,
            "ok": bool(quality["ok"] and readiness["ok"]),
            "summary": quality["summary"],
            "exports": list_role_agent_exports(kb, status=args.status),
            "quality": quality,
            "readiness": readiness,
            "runtime": inspect_role_agent_runtime(),
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
        print(
            "Role Agents: "
            f"{result['summary']['total']} total, "
            f"{result['summary']['deliverable']} deliverable, "
            f"{result['summary']['failed']} failed."
        )
        return 0 if result["ok"] else 1
    if args.command == "role" and args.role_command == "readiness":
        kb = KnowledgeBase.from_path(args.kb)
        readiness = audit_role_agent_readiness(kb, require_live=args.require_live)
        if args.json:
            print(json.dumps(readiness, ensure_ascii=False, indent=2))
            return 0 if readiness["ok"] else 1
        print(
            "Role Agent readiness: "
            f"{readiness['summary']['roles_live_ready']} live ready, "
            f"{readiness['summary']['roles_prompt_ready']} prompt ready, "
            f"{readiness['summary']['roles_missing_live']} missing live."
        )
        for role in readiness["roles"]:
            print(f"{role['status']}\t{role['role_id']}\t{role['suggested_query']}")
        return 0 if readiness["ok"] else 1
    if args.command == "sources" and args.sources_command == "create":
        kb = KnowledgeBase.from_path(args.kb)
        result = create_source(
            kb,
            source_id=args.source,
            role_id=args.role,
            platform=args.platform,
            source_url=args.source_url,
            display_name=args.display_name,
            adapter=args.adapter,
            adapter_config=_parse_json_object(args.adapter_config, "adapter-config"),
            symbols=_split_cli_list(args.symbols),
            topics=_split_cli_list(args.topics),
            tags=_split_cli_list(args.tags),
            cadence=args.cadence,
            notes=args.notes,
            enabled=not args.disabled,
            overwrite=args.overwrite,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print(f"Created source config: {result['config_path']}")
        return 0
    if args.command == "sources" and args.sources_command == "list":
        kb = KnowledgeBase.from_path(args.kb)
        sources = list_sources(kb)
        payload = {"root": str(kb.root), "summary": summarize_sources(sources), "sources": sources}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        print(f"Sources: {payload['summary']['active']} active, {payload['summary']['total']} total.")
        for source in sources:
            print(f"{source['status']}\t{source['source_id']}\t{source['role_id']}\t{source['platform']}\t{source['source_url']}")
        return 0
    if args.command == "sources" and args.sources_command == "status":
        kb = KnowledgeBase.from_path(args.kb)
        status = read_source_status(kb)
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0 if status["ok"] else 1
        print(
            "Source runs: "
            f"{status['summary']['total']} total, "
            f"{status['summary']['failed']} failed, "
            f"{status['summary']['active_without_runs']} active source(s) without runs."
        )
        for run in status["runs"][:12]:
            print(f"{run['status']}\t{run['source_id']}\t{run['ran_at']}\t{run['error']}")
        return 0 if status["ok"] else 1
    if args.command == "sources" and args.sources_command == "validate":
        kb = KnowledgeBase.from_path(args.kb)
        report = validate_source_adapters(kb)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ok"] else 1
        print(
            "Source adapters: "
            f"{report['summary']['ready']} ready, "
            f"{report['summary']['failed']} failed, "
            f"{report['summary']['disabled']} disabled."
        )
        for source in report["sources"]:
            print(f"{source['status']}\t{source['source_id']}\t{source['adapter']}\t{source['message']}")
        return 0 if report["ok"] else 1
    if args.command == "sources" and args.sources_command == "template":
        kb = KnowledgeBase.from_path(args.kb)
        result = write_source_input_template(
            kb,
            args.source,
            output_format=args.format,
            out=Path(args.out) if args.out else None,
            overwrite=args.overwrite,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print(f"Wrote source input template: {result['template_path']}")
        print(f"Next: {result['next_command']}")
        return 0
    if args.command == "sources" and args.sources_command == "normalize":
        kb = KnowledgeBase.from_path(args.kb)
        result = normalize_source_input(
            kb,
            args.source,
            Path(args.input),
            out=Path(args.out) if args.out else None,
            dry_run=args.dry_run,
            update_source=args.update_source,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        verb = "Would write" if result["dry_run"] else "Wrote"
        print(f"{verb} normalized source adapter input: {result['output_path']}")
        print(f"Records: {result['record_count']}")
        return 0
    if args.command == "sources" and args.sources_command == "import":
        kb = KnowledgeBase.from_path(args.kb)
        result = import_source_input(
            kb,
            args.source,
            Path(args.input),
            out=Path(args.out) if args.out else None,
            dry_run=args.dry_run,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
        print(f"Source import preflight: {'ok' if result['ok'] else 'needs attention'}")
        print(f"Records: {result['normalized']['record_count']}")
        print(f"Output: {result['normalized']['output_path']}")
        for command in result["next_commands"]:
            print(f"Next: {command}")
        return 0 if result["ok"] else 1
    if args.command == "sources" and args.sources_command == "imports":
        kb = KnowledgeBase.from_path(args.kb)
        status = read_source_import_status(kb)
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0 if status["ok"] else 1
        print(
            "Source imports: "
            f"{status['summary']['ready']} ready, "
            f"{status['summary']['failed']} failed, "
            f"{status['summary']['total']} total."
        )
        for item in status["imports"][:12]:
            print(f"{item['status']}\t{item['source_id']}\t{item['record_count']}\t{item['input_path']}")
        return 0 if status["ok"] else 1
    if args.command == "sources" and args.sources_command == "enqueue":
        kb = KnowledgeBase.from_path(args.kb)
        result = enqueue_source_jobs(
            kb,
            source_id=args.source or None,
            due_at=args.due_at,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if not result["errors"] else 1
        print(f"Enqueued {result['created']} source job(s).")
        for job in result["jobs"]:
            print(f"{job['status']}\t{job['job_id']}\t{job['source_id']}\t{job['due_at']}")
        return 0 if not result["errors"] else 1
    if args.command == "sources" and args.sources_command == "jobs":
        kb = KnowledgeBase.from_path(args.kb)
        status = read_source_job_status(kb, status_filter=args.status)
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0 if status["ok"] else 1
        print(
            "Source jobs: "
            f"{status['summary']['pending']} pending, "
            f"{status['summary']['completed']} completed, "
            f"{status['summary']['failed']} failed."
        )
        for job in status["jobs"][:20]:
            print(f"{job['status']}\t{job['job_id']}\t{job['source_id']}\t{job['last_error']}")
        return 0 if status["ok"] else 1
    if args.command == "sources" and args.sources_command == "drain":
        kb = KnowledgeBase.from_path(args.kb)
        result = drain_source_jobs(kb, limit=args.limit, dry_run=args.dry_run)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
        print(
            "Drained source jobs: "
            f"{result['processed']} processed, "
            f"{result['completed']} completed, "
            f"{result['failed']} failed."
        )
        for job in result["jobs"]:
            print(f"{job['status']}\t{job['job_id']}\t{job['source_id']}\t{job.get('error', '')}")
        return 0 if result["ok"] else 1
    if args.command == "sources" and args.sources_command == "retry":
        kb = KnowledgeBase.from_path(args.kb)
        job = retry_source_job(kb, args.job, due_at=args.due_at)
        status = read_source_job_status(kb)
        payload = {
            "root": str(kb.root),
            "status_path": status["status_path"],
            "job": job,
            "summary": status["summary"],
            "errors": status["errors"],
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        print(f"Retried source job: {job['job_id']}")
        print(f"Pending source jobs: {payload['summary']['pending']}")
        return 0
    if args.command == "sources" and args.sources_command == "run":
        kb = KnowledgeBase.from_path(args.kb)
        job = get_source_job(kb, args.job) if args.job else None
        source_id = args.source or str((job or {}).get("source_id") or "")
        if job and args.source and args.source != job["source_id"]:
            raise ValueError(f"Source job {args.job} belongs to source {job['source_id']}, not {args.source}.")
        if not source_id:
            raise ValueError("Source ID is required unless --job is provided.")
        try:
            result = run_source(
                kb,
                source_id,
                text=args.text,
                title=args.title,
                source_url=args.source_url,
                published_at=args.published_at,
                captured_at=args.captured_at,
                symbols=_split_cli_list(args.symbols) or None,
                topics=_split_cli_list(args.topics) or None,
                stance=args.stance,
                time_horizon=args.time_horizon,
                confidence=args.confidence,
                notes=args.notes,
                dry_run=args.dry_run,
                out=Path(args.out) if args.out else None,
            )
        except Exception as exc:
            result = record_source_run_error(
                kb,
                source_id,
                str(exc),
                dry_run=args.dry_run,
                out=Path(args.out) if args.out else None,
            )
            if job:
                result["job"] = fail_source_job(kb, args.job, str(exc))
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 1
            print(f"Source run failed: {result['error']}")
            return 1
        if job:
            result["job"] = complete_source_job(kb, args.job, result["run"])
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        verb = "Would write" if result["dry_run"] else "Wrote"
        print(f"{verb} capture record: {result['capture_path']}")
        return 0
    if args.command == "accounts" and args.accounts_command == "create":
        kb = KnowledgeBase.from_path(args.kb)
        result = create_account(
            kb,
            account_id=args.account,
            platform=args.platform,
            platform_account_id=args.platform_account_id,
            role_id=args.role,
            source_id=args.source,
            source_url=args.source_url,
            display_name=args.display_name,
            collection_mode=args.mode,
            feed_url=args.feed_url,
            input_path=args.input,
            api_url=args.api_url,
            adapter_config=_parse_json_object(args.adapter_config, "accounts create --adapter-config"),
            symbols=_split_cli_list(args.symbols),
            topics=_split_cli_list(args.topics),
            tags=_split_cli_list(args.tags),
            notes=args.notes,
            enabled=not args.disabled,
            overwrite=args.overwrite,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print(f"Created account archive: {result['account_id']} ({result['collection_mode']})")
        return 0
    if args.command == "accounts" and args.accounts_command == "list":
        kb = KnowledgeBase.from_path(args.kb)
        accounts = list_accounts(kb)
        status = read_account_status(kb)
        payload = {"summary": status["summary"], "accounts": accounts}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0 if status["ok"] else 1
        for account in accounts:
            print(
                f"{account.get('status', '')}\t{account.get('collection_mode', '')}\t"
                f"{account.get('account_id', '')}\t{account.get('platform', '')}\t{account.get('role_id', '')}"
            )
        return 0 if status["ok"] else 1
    if args.command == "accounts" and args.accounts_command == "status":
        kb = KnowledgeBase.from_path(args.kb)
        status = read_account_status(kb)
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0 if status["ok"] else 1
        summary = status["summary"]
        print(
            "Account archives: "
            f"{summary['active']} active, {summary['blocked']} blocked, {summary['runs']} run(s)."
        )
        return 0 if status["ok"] else 1
    if args.command == "accounts" and args.accounts_command == "collect":
        kb = KnowledgeBase.from_path(args.kb)
        result = collect_account(
            kb,
            args.account,
            dry_run=args.dry_run,
            sync=args.sync,
            archive=args.archive,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
        if result["ok"]:
            print(
                f"Collected account archive: {result['account_id']} "
                f"({result['written']} written, {result['duplicates_skipped']} duplicate(s))."
            )
            return 0
        print(f"Account collection failed: {result['error']}")
        return 1
    if args.command == "event" and args.event_command == "create":
        kb = KnowledgeBase.from_path(args.kb)
        path = create_event(
            kb,
            event_id=args.event_id,
            title=args.title,
            date=args.date,
            symbols=_split_cli_list(args.symbols),
            topics=_split_cli_list(args.topics),
            summary=args.summary,
            overwrite=args.overwrite,
        )
        print(f"Created event: {path}")
        return 0
    if args.command == "event" and args.event_command == "list":
        kb = KnowledgeBase.from_path(args.kb)
        events = list_events(kb)
        if args.json:
            print(json.dumps(events, ensure_ascii=False, indent=2))
            return 0
        for event in events:
            print(f"{event['date']}\t{event['event_id']}\t{event['title']}\t{event['path']}")
        return 0
    if args.command == "profile" and args.profile_command == "generate":
        kb = KnowledgeBase.from_path(args.kb)
        path = generate_profile(kb, args.role)
        print(f"Generated profile draft: {path}")
        return 0
    if args.command == "profile" and args.profile_command == "promote":
        kb = KnowledgeBase.from_path(args.kb)
        path = promote_generated_profile(
            kb,
            args.role,
            overwrite=args.overwrite,
            reviewer=args.reviewer,
            review_note=args.note,
        )
        if args.json:
            print(json.dumps({"role_id": args.role, "profile_path": str(path)}, ensure_ascii=False))
            return 0
        print(f"Promoted reviewed profile: {path}")
        return 0
    if args.command == "analyze":
        kb = KnowledgeBase.from_path(args.kb)
        event = load_event(Path(args.event))
        roles = "all" if args.roles == "all" else [part.strip() for part in args.roles.split(",") if part.strip()]
        result = analyze_event(kb, event, roles=roles)
        output_dir = Path(args.out) if args.out else default_export_dir(kb, event.event_id)
        output = write_analysis_outputs(output_dir, result)
        if args.json:
            print(
                json.dumps(
                    {
                        "event_id": event.event_id,
                        "role_count": len(result["role_analyses"]),
                        "analysis_json": str(output.json_path),
                        "analysis_markdown": str(output.markdown_path),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        print(f"Wrote analysis JSON: {output.json_path}")
        print(f"Wrote analysis Markdown: {output.markdown_path}")
        return 0
    if args.command == "search":
        kb = KnowledgeBase.from_path(args.kb)
        result = search_statements(
            kb,
            args.query,
            role_id=args.role,
            symbol=args.symbol,
            topic=args.topic,
            limit=args.limit,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print(f"Search: {result['query']}")
        print(f"Matches: {result['total_matches']}")
        for item in result["results"]:
            print(
                f"{item['score']}\t{item['role_id']}\t{item['published_at']}\t"
                f"{item['title']}\t{item['source_url']}"
            )
        return 0
    if args.command == "route":
        kb = KnowledgeBase.from_path(args.kb)
        result = suggest_roles(kb, args.query, symbol=args.symbol, topic=args.topic, limit=args.limit)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print(f"Suggested role: {result['suggested_role_id'] or 'none'}")
        print(f"Confidence: {result['confidence']}")
        for item in result["routes"]:
            print(f"{item['score']}\t{item['role_id']}\t{item['evidence_count']} evidence\t{item['reason']}")
        return 0
    if args.command == "compare":
        kb = KnowledgeBase.from_path(args.kb)
        result = compare_roles(
            kb,
            args.query,
            roles=args.roles,
            symbol=args.symbol,
            topic=args.topic,
            limit=args.limit,
            evidence_limit=args.evidence_limit,
        )
        output_dir = Path(args.out) if args.out else default_comparison_dir(kb, args.query)
        output = write_comparison_outputs(output_dir, result)
        payload = {
            "comparison": result,
            "comparison_json": str(output["comparison_json"]),
            "comparison_markdown": str(output["comparison_markdown"]),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        print(result["comparison_markdown"])
        print(f"Wrote comparison JSON: {output['comparison_json']}")
        print(f"Wrote comparison Markdown: {output['comparison_markdown']}")
        return 0
    if args.command == "comparisons" and args.comparisons_command == "list":
        kb = KnowledgeBase.from_path(args.kb)
        all_exports = list_comparison_exports(kb)
        comparisons = filter_comparison_exports(
            all_exports,
            status=args.status,
            review_status=args.review_status,
        )
        payload = {
            "root": str(kb.root),
            "status": args.status,
            "review_status": args.review_status,
            "summary": summarize_comparison_exports(all_exports),
            "comparisons": comparisons,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        print(f"Comparison exports: {len(comparisons)} shown, {payload['summary']['total']} total.")
        for item in comparisons:
            print(
                f"{item['status']}\t{item['review_status']}\t{item['evidence_count']} evidence\t"
                f"{item['query']}\t{item['comparison_json']}"
            )
        return 0
    if args.command == "comparisons" and args.comparisons_command == "review":
        kb = KnowledgeBase.from_path(args.kb)
        if args.path:
            comparison_path = Path(args.path)
        elif args.query:
            comparison_path = default_comparison_dir(kb, args.query) / "comparison.json"
        else:
            raise ValueError("comparisons review requires --query or --path.")
        result = review_comparison_export(
            comparison_path,
            status=args.status,
            reviewer=args.reviewer,
            notes=args.notes,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print(f"Marked comparison {args.status}: {result['comparison_json']}")
        return 0
    if args.command == "answer":
        kb = KnowledgeBase.from_path(args.kb)
        result = answer_query(
            kb,
            args.query,
            role_id=args.role,
            symbol=args.symbol,
            topic=args.topic,
            limit=args.limit,
        )
        output_dir = Path(args.out) if args.out else default_answer_dir(kb, args.query)
        output = write_answer_outputs(output_dir, result)
        payload = {
            "answer": result,
            "answer_json": str(output["answer_json"]),
            "answer_markdown": str(output["answer_markdown"]),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        print(result["answer_markdown"])
        print(f"Wrote answer JSON: {output['answer_json']}")
        print(f"Wrote answer Markdown: {output['answer_markdown']}")
        return 0
    if args.command == "answers" and args.answers_command == "list":
        kb = KnowledgeBase.from_path(args.kb)
        all_exports = list_answer_exports(kb)
        answers = filter_answer_exports(all_exports, status=args.status)
        payload = {
            "root": str(kb.root),
            "status": args.status,
            "summary": summarize_answer_exports(all_exports),
            "answers": answers,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        print(f"Answer exports: {len(answers)} shown, {payload['summary']['total']} total.")
        for item in answers:
            print(
                f"{item['status']}\t{item['query'] or '<unknown>'}\t"
                f"{item['evidence_count']} evidence\t{item['citation_count']} citation(s)\t{item['answer_json']}"
            )
        return 0
    if args.command == "answers" and args.answers_command == "prune":
        if args.dry_run and args.yes:
            raise ValueError("Use either --dry-run or --yes, not both.")
        kb = KnowledgeBase.from_path(args.kb)
        result = prune_answer_exports(kb, status=args.status, dry_run=not args.yes)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if not result["errors"] else 1
        verb = "Would remove" if result["dry_run"] else "Removed"
        print(f"{verb} {result['matched']} answer export(s) with status {args.status}.")
        for item in result["answers"]:
            print(f"- {item['status']}\t{item['query'] or '<unknown>'}\t{item['answer_json']}")
        if result["errors"]:
            print("Errors:")
            for error in result["errors"]:
                print(f"- {error}")
        return 0 if not result["errors"] else 1
    if args.command == "evaluations" and args.evaluations_command == "answers":
        kb = KnowledgeBase.from_path(args.kb)
        result = audit_answer_regression(kb, suite_path=Path(args.suite) if args.suite else None)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
        summary = result["summary"]
        print(
            "Answer regression: "
            f"{summary['passed']} passed, {summary['review']} review, {summary['failed']} failed."
        )
        for item in result["items"]:
            print(f"{item['status']}\t{item['id']}\t{item['query']}\t{','.join(item['failed_checks'])}")
        for error in result["errors"]:
            print(f"error\t{error}")
        return 0 if result["ok"] else 1
    if args.command == "evaluations" and args.evaluations_command == "export":
        kb = KnowledgeBase.from_path(args.kb)
        result = export_answer_regression_suite(
            kb,
            suite_path=Path(args.suite) if args.suite else None,
            out_path=Path(args.out),
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
        print(f"Wrote answer regression suite export: {result['export_path']}")
        print(f"Questions: {result['question_count']}")
        for error in result["errors"]:
            print(f"error\t{error}")
        return 0 if result["ok"] else 1
    if args.command == "evaluations" and args.evaluations_command == "import":
        kb = KnowledgeBase.from_path(args.kb)
        result = import_answer_regression_suite(
            kb,
            Path(args.input),
            dry_run=bool(args.dry_run or not args.yes),
            updated_by=args.updated_by,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
        verb = "Would import" if result["dry_run"] else "Imported"
        summary = result["summary"]
        print(
            f"{verb} answer regression suite: "
            f"{summary['create']} create, {summary['update']} update, {summary['unchanged']} unchanged."
        )
        for error in result["errors"]:
            print(f"error\t{error}")
        return 0 if result["ok"] else 1
    if args.command == "collect":
        kb = KnowledgeBase.from_path(args.kb)
        path = create_evidence_pack(
            kb,
            title=args.title,
            query=args.query,
            role_id=args.role,
            symbol=args.symbol,
            topic=args.topic,
            limit=args.limit,
        )
        if args.json:
            print(json.dumps({"title": args.title, "path": str(path)}, ensure_ascii=False))
            return 0
        print(f"Wrote evidence pack: {path}")
        return 0
    if args.command == "reports" and args.reports_command == "list":
        kb = KnowledgeBase.from_path(args.kb)
        reports = list_reports(kb)
        if args.json:
            print(json.dumps(reports, ensure_ascii=False, indent=2))
            return 0
        for report in reports:
            print(
                f"{report['generated_at'] or 'unknown'}\t"
                f"{report['matches']} matches\t"
                f"{report['title']}\t"
                f"{report['path']}"
            )
        return 0
    if args.command == "analyses" and args.analyses_command == "list":
        kb = KnowledgeBase.from_path(args.kb)
        analyses = list_analysis_exports(kb)
        payload = {
            "root": str(kb.root),
            "summary": summarize_analysis_exports(analyses),
            "analyses": analyses,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0 if payload["summary"]["malformed"] == 0 else 1
        print(f"Analysis exports: {payload['summary']['ready']} ready, {payload['summary']['malformed']} malformed.")
        for item in analyses:
            print(
                f"{item['status']}\t{item['event_id']}\t"
                f"{item['role_count']} role(s)\t{item['evidence_count']} evidence\t{item['analysis_json']}"
            )
        return 0 if payload["summary"]["malformed"] == 0 else 1
    if args.command == "release" and args.release_command == "check":
        kb = KnowledgeBase.from_path(args.kb)
        report = check_release_readiness(kb, require_live_role_agent=args.require_live_role_agent)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ok"] else 1
        print(f"Release readiness: {'ok' if report['ok'] else 'needs attention'}")
        for check in report["checks"]:
            marker = "ok" if check["ok"] else "fail"
            print(f"{marker}\t{check['id']}\t{check['message']}")
        return 0 if report["ok"] else 1
    if args.command == "release" and args.release_command == "manifest":
        kb = KnowledgeBase.from_path(args.kb)
        path = write_release_manifest(kb, out_dir=Path(args.out) if args.out else None)
        report = check_release_readiness(kb)
        if args.json:
            print(json.dumps({"ok": report["ok"], "manifest_path": str(path)}, ensure_ascii=False))
            return 0 if report["ok"] else 1
        print(f"Wrote release manifest: {path}")
        print(f"Release readiness: {'ok' if report['ok'] else 'needs attention'}")
        return 0 if report["ok"] else 1
    if args.command == "release" and args.release_command == "bundle":
        kb = KnowledgeBase.from_path(args.kb)
        bundle = write_release_bundle(
            kb,
            out_dir=Path(args.out) if args.out else None,
            repo_root=Path(args.root) if args.root else None,
        )
        if args.json:
            print(json.dumps(bundle, ensure_ascii=False, indent=2))
            return 0 if bundle["ok"] else 1
        print(f"Wrote release bundle: {bundle['bundle_dir']}")
        print(f"Wrote release bundle zip: {bundle['bundle_zip']}")
        print(f"Release readiness: {'ok' if bundle['ok'] else 'needs attention'}")
        return 0 if bundle["ok"] else 1
    if args.command == "release" and args.release_command == "prepare":
        kb = KnowledgeBase.from_path(args.kb)
        result = prepare_release(
            kb,
            out_dir=Path(args.out) if args.out else None,
            repo_root=Path(args.root) if args.root else None,
            drain_jobs=not args.skip_drain,
            execute_jobs=args.execute_jobs,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
        print(f"Release prepare: {'ok' if result['ok'] else 'needs attention'}")
        for step in result["steps"]:
            marker = "ok" if step["ok"] else "fail"
            print(f"{marker}\t{step['id']}")
        print(f"Wrote release bundle: {result['bundle']['bundle_dir']}")
        return 0 if result["ok"] else 1
    if args.command == "release" and args.release_command == "package":
        result = write_distribution_package(
            Path(args.root) if args.root else Path.cwd(),
            out_dir=Path(args.out) if args.out else None,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
        print(f"Wrote CLI package: {result['package_zip']}")
        print(f"Wrote package manifest: {result['manifest_path']}")
        print(f"Wrote install guide: {result['install_guide']}")
        return 0 if result["ok"] else 1
    if args.command == "release" and args.release_command == "ship":
        kb = KnowledgeBase.from_path(args.kb)
        result = ship_release(
            Path(args.root) if args.root else Path.cwd(),
            kb,
            out_dir=Path(args.out) if args.out else None,
            drain_jobs=not args.skip_drain,
            execute_jobs=args.execute_jobs,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
        print(f"Release ship: {'ok' if result['ok'] else 'needs attention'}")
        print(f"Wrote ship manifest: {result['ship_manifest']}")
        print(f"Wrote ship summary: {result['ship_summary']}")
        return 0 if result["ok"] else 1
    if args.command == "release" and args.release_command == "verify":
        result = verify_ship_manifest(Path(args.manifest), out_path=Path(args.out) if args.out else None)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
        print(f"Release verify: {'ok' if result['ok'] else 'needs attention'}")
        print(f"Wrote verification report: {result['report_path']}")
        for check in result["checks"]:
            marker = "ok" if check["ok"] else "fail"
            print(f"{marker}\t{check['id']}\t{check['message']}")
        return 0 if result["ok"] else 1
    if args.command == "release" and args.release_command == "inspect":
        result = inspect_release_handoff(Path(args.manifest))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
        summary = result["summary"]
        print(f"Release inspect: {'ok' if result['ok'] else 'needs attention'}")
        print(f"Manifest: {result['manifest_path']}")
        print(f"Artifact index: {result['artifact_index_path']}")
        print(
            "Artifacts: "
            f"{summary['existing']}/{summary['total']} existing, "
            f"{summary['missing_required']} required missing, "
            f"{summary['sha256_mismatched']} sha256 mismatched."
        )
        for artifact in result["artifacts"]:
            marker = "ok" if artifact["exists"] and artifact["sha256_ok"] is not False else "fail"
            print(f"{marker}\t{artifact['id']}\t{artifact['phase']}\t{artifact['path']}")
        for error in result["errors"]:
            print(f"error\t{error}")
        return 0 if result["ok"] else 1
    if args.command == "release" and args.release_command == "audit":
        if args.summary_check_out and not args.summary_check:
            raise ValueError("release audit --summary-check-out requires --summary-check.")
        result = verify_ship_manifest(
            Path(args.manifest),
            write_artifacts=False,
            require_archived_attestation=True,
        )
        summary_text = _audit_summary_markdown(result) if args.summary or args.summary_out or args.summary_check else ""
        if args.summary_out:
            _write_audit_summary(Path(args.summary_out), summary_text)
        if args.summary_check:
            result = dict(result)
            result["summary_check"] = _check_audit_summary(Path(args.summary_check), summary_text)
            result["ok"] = bool(result["ok"] and result["summary_check"]["ok"])
        if args.summary_check_out:
            result = dict(result)
            result["summary_check_report"] = str(Path(args.summary_check_out))
            _write_audit_summary_check_report(Path(args.summary_check_out), result)
        if args.summary:
            print(summary_text)
            return 0 if result["ok"] else 1
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
        print(f"Release audit: {'ok' if result['ok'] else 'needs attention'}")
        for check in result["checks"]:
            marker = "ok" if check["ok"] else "fail"
            print(f"{marker}\t{check['id']}\t{check['message']}")
        if args.summary_check:
            marker = "ok" if result["summary_check"]["ok"] else "fail"
            print(f"{marker}\tsummary_check\t{result['summary_check']['message']}")
        return 0 if result["ok"] else 1
    if args.command == "guide" and args.guide_command == "quickstart":
        kb = KnowledgeBase.from_path(args.kb)
        result = write_quickstart_guide(
            kb,
            repo_root=Path(args.root) if args.root else None,
            out_dir=Path(args.out) if args.out else None,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print(f"Wrote quickstart guide JSON: {result['guide_json']}")
        print(f"Wrote quickstart guide Markdown: {result['guide_markdown']}")
        return 0
    if args.command == "sample" and args.sample_command == "remove":
        kb = KnowledgeBase.from_path(args.kb)
        result = preview_sample_removal(kb) if args.dry_run else remove_sample_content(kb)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.dry_run:
            print(
                "Sample content removal preview: "
                f"{len(result['removed_roles'])} role(s), "
                f"{len(result['removed_events'])} event(s)."
            )
            return 0
        print(
            "Removed sample content: "
            f"{len(result['removed_roles'])} role(s), "
            f"{len(result['removed_events'])} event(s), "
            f"{result['statements_indexed']} statement(s) indexed."
        )
        return 0
    if args.command == "capture" and args.capture_command == "status":
        kb = KnowledgeBase.from_path(args.kb)
        status = read_capture_status(kb)
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0 if status["summary"]["failed"] == 0 else 1
        print(f"Capture status: {status['summary']['processed']} processed, {status['summary']['failed']} failed.")
        print(f"Duplicates skipped: {status['summary'].get('duplicates_skipped', 0)}")
        print(f"Pending inbox files: {status['pending_count']}")
        return 0 if status["summary"]["failed"] == 0 else 1
    if args.command == "capture" and args.capture_command == "validate":
        report = validate_capture_path(Path(args.path))
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ok"] else 1
        print(f"Capture validation: {'ok' if report['ok'] else 'failed'}")
        print(f"Records: {report['records']}")
        for error in report["errors"]:
            print(f"- {error}")
        return 0 if report["ok"] else 1
    if args.command == "capture" and args.capture_command == "append":
        kb = KnowledgeBase.from_path(args.kb)
        path = Path(args.out) if args.out else kb.inbox_captures_dir / "manual.jsonl"
        record = build_capture_record(
            role_id=args.role,
            platform=args.platform,
            text=args.text,
            url=args.url,
            title=args.title,
            author=args.author,
            platform_user_id=args.user_id,
            published_at=args.published_at,
            captured_at=args.captured_at,
            symbols=_split_cli_list(args.symbols),
            topics=_split_cli_list(args.topics),
            statement_id=args.statement_id,
            stance=args.stance,
            time_horizon=args.time_horizon,
            confidence=args.confidence,
            notes=args.notes,
        )
        append_capture_record(path, record)
        validation = validate_capture_path(path)
        if args.json:
            print(json.dumps({"path": str(path), "record": record, "validation": validation}, ensure_ascii=False, indent=2))
            return 0 if validation["ok"] else 1
        print(f"Appended capture: {path}")
        print(f"Records: {validation['records']}")
        return 0 if validation["ok"] else 1
    if args.command == "sync":
        kb = KnowledgeBase.from_path(args.kb)
        if args.status:
            status = read_sync_status(kb)
            if args.json:
                print(json.dumps(status, ensure_ascii=False, indent=2))
            else:
                print(f"Sync status: {'ok' if status['ok'] else 'needs attention'}")
                if status.get("warnings"):
                    print("Warnings:")
                    for warning in status["warnings"]:
                        print(f"- {warning}")
            return 0
        if args.watch:
            if not args.json:
                print(f"Watching capture inbox: {kb.inbox_captures_dir}")
                print("Press Ctrl+C to stop.")
            try:
                for result in watch_sync(kb, args.interval, archive_processed=args.archive):
                    _print_sync_result(result, json_output=args.json)
            except KeyboardInterrupt:
                if not args.json:
                    print("Stopped sync watch.")
                return 0
        result = sync_once(kb, archive_processed=args.archive)
        _print_sync_result(result, json_output=args.json)
        return 0 if not result.errors else 1
    raise ValueError(f"Unknown command: {args.command}")


def _audit_summary_markdown(result: dict[str, Any]) -> str:
    summary = result.get("summary", {})
    product = result.get("product", {})
    failed_ids = summary.get("failed_ids", []) if isinstance(summary, dict) else []
    failed_text = ", ".join(str(item) for item in failed_ids) if failed_ids else "none"
    lines = [
        "# VoiceVault Release Audit",
        "",
        f"- Product: {product.get('english_name', 'VoiceVault')}",
        f"- Version: {product.get('version', '')}",
        f"- Status: {'ok' if result.get('ok') else 'needs attention'}",
        f"- Write artifacts: {str(result.get('write_artifacts', True)).lower()}",
        f"- Checks: {summary.get('passed', 0)} passed / {summary.get('failed', 0)} failed",
        f"- Failed checks: {failed_text}",
        "",
        "## Artifacts",
        "",
        f"- Manifest: {result.get('manifest_path', '')}",
        f"- Verification report: {result.get('report_path', '')}",
        f"- Attestation: {result.get('attestation_path', '')}",
        f"- Attestation SHA256: {result.get('attestation_sha256_path', '')}",
    ]
    failed_checks = [check for check in result.get("checks", []) if isinstance(check, dict) and not check.get("ok")]
    if failed_checks:
        lines.extend(["", "## Blocking Checks", ""])
        for index, check in enumerate(failed_checks):
            if index:
                lines.append("")
            lines.extend(_audit_failed_check_markdown(check, str(result.get("manifest_path", ""))))
    return "\n".join(lines)


def _write_audit_summary(path: Path, summary: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_audit_summary_bytes(summary))


def _audit_summary_bytes(summary: str) -> bytes:
    return f"{summary}\n".encode("utf-8")


def _check_audit_summary(path: Path, summary: str) -> dict[str, Any]:
    expected_bytes = _audit_summary_bytes(summary)
    expected_sha256 = hashlib.sha256(expected_bytes).hexdigest()
    if not path.is_file():
        return {
            "path": str(path),
            "ok": False,
            "exists": False,
            "expected_sha256": expected_sha256,
            "actual_sha256": "",
            "message": "Archived audit summary file is missing.",
        }
    actual_bytes = path.read_bytes()
    actual_sha256 = hashlib.sha256(actual_bytes).hexdigest()
    ok = actual_bytes == expected_bytes
    return {
        "path": str(path),
        "ok": ok,
        "exists": True,
        "expected_sha256": expected_sha256,
        "actual_sha256": actual_sha256,
        "message": (
            "Archived audit summary matches the current audit summary."
            if ok
            else "Archived audit summary does not match the current audit summary."
        ),
    }


def _write_audit_summary_check_report(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_audit_summary_check_report(result), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _audit_summary_check_report(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "product": result.get("product", {}),
        "ok": bool(result.get("ok")),
        "manifest_path": str(result.get("manifest_path", "")),
        "summary": result.get("summary", {}),
        "write_artifacts": bool(result.get("write_artifacts", True)),
        "summary_check": result.get("summary_check", {}),
    }


def _audit_failed_check_markdown(check: dict[str, Any], manifest_path: str) -> list[str]:
    lines = [
        f"### {check.get('id', 'unknown_check')}",
        "",
        f"- Message: {check.get('message', '')}",
    ]
    details = check.get("details")
    if isinstance(details, dict) and details:
        lines.append("- Details:")
        for detail_line in _audit_detail_markdown(details):
            lines.append(f"  {detail_line}")
    lines.append("- Remediation:")
    for command in _audit_remediation_commands(manifest_path):
        lines.append(f"  - `{command}`")
    return lines


def _audit_detail_markdown(details: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key in sorted(details):
        lines.extend(_audit_value_markdown(str(key), details[key], indent=0))
    return lines


def _audit_value_markdown(key: str, value: Any, *, indent: int) -> list[str]:
    prefix = " " * indent
    if isinstance(value, dict):
        lines = [f"{prefix}- {key}:"]
        for nested_key in sorted(value):
            lines.extend(_audit_value_markdown(str(nested_key), value[nested_key], indent=indent + 2))
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{prefix}- {key}: []"]
        lines = [f"{prefix}- {key}:"]
        for item in value:
            if isinstance(item, dict):
                lines.append(f"{prefix}  -")
                for nested_key in sorted(item):
                    lines.extend(_audit_value_markdown(str(nested_key), item[nested_key], indent=indent + 4))
            elif isinstance(item, list):
                lines.append(f"{prefix}  - {_audit_scalar_markdown(item)}")
            else:
                lines.append(f"{prefix}  - {_audit_scalar_markdown(item)}")
        return lines
    return [f"{prefix}- {key}: {_audit_scalar_markdown(value)}"]


def _audit_scalar_markdown(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return "null"
    if isinstance(value, list):
        return ", ".join(_audit_scalar_markdown(item) for item in value)
    return str(value)


def _audit_remediation_commands(manifest_path: str) -> list[str]:
    return [
        f"python -m voicevault release verify --manifest {manifest_path} --json",
        f"python -m voicevault release audit --manifest {manifest_path} --summary",
    ]


def _split_cli_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _post_local_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            error = error_payload.get("error", {})
            code = str(error.get("code") or f"http_{exc.code}")
            message = str(error.get("message") or exc.reason)
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            code = f"http_{exc.code}"
            message = str(exc.reason)
        raise ValueError(f"{code}: {message}") from None
    except URLError as exc:
        raise ValueError(f"VoiceVault local service is unreachable: {exc.reason}") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("VoiceVault local service returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise ValueError("VoiceVault local service returned an invalid response.")
    return result


def _get_local_json(url: str) -> dict[str, Any]:
    request = Request(url, method="GET")
    try:
        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            error = error_payload.get("error", {})
            code = str(error.get("code") or f"http_{exc.code}")
            message = str(error.get("message") or exc.reason)
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            code = f"http_{exc.code}"
            message = str(exc.reason)
        raise ValueError(f"{code}: {message}") from None
    except URLError as exc:
        raise ValueError(f"VoiceVault local service is unreachable: {exc.reason}") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("VoiceVault local service returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise ValueError("VoiceVault local service returned an invalid response.")
    return result


def _safe_collection_job_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError("VoiceVault local service returned an invalid collection job ID.")
    return value


def _parse_json_object(value: str, label: str) -> dict[str, Any]:
    if not value.strip():
        return {}
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return payload


def _print_sync_result(result: SyncResult, json_output: bool) -> None:
    payload = {
        "captures_seen": result.captures_seen,
        "notes_written": result.notes_written,
        "duplicates_skipped": result.duplicates_skipped,
        "statements_indexed": result.statements_indexed,
        "source_files": result.source_files,
        "errors": result.errors,
        "archived_files": result.archived_files,
        "capture_files": result.capture_files,
    }
    if json_output:
        print(json.dumps(payload, ensure_ascii=False))
        return
    print(
        "Synced captures: "
        f"{result.captures_seen} seen, "
        f"{result.notes_written} note(s) written, "
        f"{result.duplicates_skipped} duplicate(s) skipped, "
        f"{result.statements_indexed} statement(s) indexed."
    )
