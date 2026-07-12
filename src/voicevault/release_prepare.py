from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from .checksums import file_sha256, write_sha256_file
from .dashboard import write_dashboard
from .guide import write_quickstart_guide
from .kb import KnowledgeBase
from .release import check_release_readiness, write_release_bundle
from .source_jobs import drain_source_jobs, read_source_job_status
from .sources import validate_source_adapters
from .ui import write_local_ui


PREPARE_REPORT_SCHEMA_VERSION = 1


def prepare_release(
    kb: KnowledgeBase,
    *,
    out_dir: Path | None = None,
    repo_root: Path | None = None,
    drain_jobs: bool = True,
    execute_jobs: bool = False,
) -> dict[str, Any]:
    root = repo_root.resolve() if repo_root else Path.cwd().resolve()
    source_adapter_validation = validate_source_adapters(kb)
    source_jobs_before = read_source_job_status(kb)
    source_job_drain = _prepare_source_jobs(
        kb,
        source_jobs_before,
        drain_jobs=drain_jobs,
        execute_jobs=execute_jobs,
    )
    dashboard_path = write_dashboard(kb)
    ui_path = write_local_ui(kb, repo_root=root)
    bundle = write_release_bundle(kb, out_dir=out_dir, repo_root=root)
    quickstart = write_quickstart_guide(kb, repo_root=root, out_dir=Path(bundle["bundle_dir"]))
    bundle["files"]["quickstart_json"] = quickstart["guide_json"]
    bundle["files"]["quickstart_markdown"] = quickstart["guide_markdown"]
    release_check = check_release_readiness(kb)
    steps = [
        _step("source_adapters", source_adapter_validation["ok"], source_adapter_validation["summary"]),
        _step("source_jobs", source_job_drain["ok"], source_job_drain),
        _step("dashboard", dashboard_path.is_file(), {"path": str(dashboard_path)}),
        _step("ui", ui_path.is_file(), {"index_html": str(ui_path), "data_json": str(ui_path.with_name("data.json"))}),
        _step("quickstart", bool(Path(quickstart["guide_json"]).is_file() and Path(quickstart["guide_markdown"]).is_file()), {"guide_json": quickstart["guide_json"], "guide_markdown": quickstart["guide_markdown"]}),
        _step("release_bundle", bundle["ok"], {"bundle_dir": bundle["bundle_dir"], "bundle_zip": bundle["bundle_zip"]}),
        _step("release_check", release_check["ok"], release_check["summary"]),
    ]
    result = {
        "schema_version": PREPARE_REPORT_SCHEMA_VERSION,
        "ok": bool(release_check["ok"] and bundle["ok"] and source_job_drain["ok"]),
        "root": str(kb.root),
        "repo_root": str(root),
        "execute_jobs": execute_jobs,
        "steps": steps,
        "source_adapter_validation": source_adapter_validation,
        "source_jobs_before": source_jobs_before,
        "source_job_drain": source_job_drain,
        "dashboard": str(dashboard_path),
        "ui": {
            "index_html": str(ui_path),
            "data_json": str(ui_path.with_name("data.json")),
        },
        "quickstart": {
            "guide_json": quickstart["guide_json"],
            "guide_markdown": quickstart["guide_markdown"],
        },
        "release_check": release_check,
        "bundle": bundle,
    }
    _write_prepare_report(result)
    return result


def _prepare_source_jobs(
    kb: KnowledgeBase,
    source_jobs_before: dict[str, Any],
    *,
    drain_jobs: bool,
    execute_jobs: bool,
) -> dict[str, Any]:
    pending = int(source_jobs_before["summary"]["pending"])
    if not drain_jobs:
        return _skipped_drain(kb, source_jobs_before, reason="disabled")
    if pending == 0:
        return _skipped_drain(kb, source_jobs_before, reason="no_pending_jobs")
    result = drain_source_jobs(kb, dry_run=not execute_jobs)
    result["skipped"] = False
    return result


def _skipped_drain(kb: KnowledgeBase, source_jobs: dict[str, Any], *, reason: str) -> dict[str, Any]:
    return {
        "ok": bool(source_jobs["ok"]),
        "root": str(kb.root),
        "status_path": source_jobs["status_path"],
        "skipped": True,
        "reason": reason,
        "dry_run": True,
        "limit": 0,
        "processed": 0,
        "completed": 0,
        "failed": 0,
        "jobs": [],
        "summary": source_jobs["summary"],
        "errors": source_jobs["errors"],
    }


def _step(step_id: str, ok: bool, details: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": step_id,
        "ok": bool(ok),
        "details": details,
    }


def _write_prepare_report(result: dict[str, Any]) -> None:
    bundle_dir = Path(str(result["bundle"]["bundle_dir"]))
    bundle_zip = Path(str(result["bundle"]["bundle_zip"]))
    report_path = bundle_dir / "release-prepare.json"
    result["prepare_report"] = str(report_path)
    result["bundle"]["files"]["release_prepare"] = str(report_path)
    report_payload = deepcopy(result)
    report_payload["bundle"].pop("bundle_zip_sha256", None)
    report_payload["bundle"].pop("bundle_zip_sha256_path", None)
    report_payload["bundle"]["files"].pop("bundle_zip_sha256", None)
    report_path.write_text(
        json.dumps(report_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    _rewrite_bundle_zip(bundle_dir, bundle_zip)
    zip_sha256 = file_sha256(bundle_zip)
    zip_sha256_path = write_sha256_file(bundle_zip, zip_sha256)
    result["bundle"]["bundle_zip_sha256"] = zip_sha256
    result["bundle"]["bundle_zip_sha256_path"] = str(zip_sha256_path)
    result["bundle"]["files"]["bundle_zip_sha256"] = str(zip_sha256_path)


def _rewrite_bundle_zip(bundle_dir: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with ZipFile(zip_path, "w", ZIP_DEFLATED) as archive:
        for path in sorted(bundle_dir.iterdir()):
            if path.is_file():
                archive.write(path, arcname=path.name)
