from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from .answer import inspect_answer_export, is_deliverable_answer_export
from .checksums import file_sha256
from .comparison import inspect_comparison_export, is_adopted_comparison_export, is_deliverable_comparison_export

VERIFICATION_REPORT_SCHEMA_VERSION = 1
RELEASE_ATTESTATION_SCHEMA_VERSION = 1
KB_RELEASE_DIGEST_ENTRIES = [
    "readiness.json",
    "manifest.json",
    "release-summary.md",
    "release-plan.md",
    "release-prepare.json",
    "quickstart.json",
    "quickstart.md",
]


def verify_ship_manifest(
    manifest_path: Path,
    out_path: Path | None = None,
    *,
    write_artifacts: bool = True,
    require_archived_attestation: bool = False,
) -> dict[str, Any]:
    path = manifest_path.resolve()
    report_path = _resolve_report_path(path, out_path)
    checks: list[dict[str, Any]] = []
    manifest = _read_manifest(path, checks)
    if manifest:
        _verify_ship_manifest_contract(manifest, checks)
        _verify_ship_summary_handoff(manifest, checks)
        _verify_release_artifact_index(manifest, path, checks)
        _verify_release_artifact_index_sidecar(manifest, checks)
        _verify_cli_package(manifest, checks)
        _verify_kb_release(manifest, checks)
        _verify_ui_artifacts(manifest, checks)
    product = manifest.get("product", {}) if isinstance(manifest, dict) else {}
    result = {
        "schema_version": VERIFICATION_REPORT_SCHEMA_VERSION,
        "product": product,
        "ok": False,
        "manifest_path": str(path),
        "report_path": str(report_path),
        "attestation_path": "",
        "attestation_sha256_path": "",
        "write_artifacts": write_artifacts,
        "summary": _verification_summary(checks),
        "checks": checks,
    }
    _verify_verification_report_contract(result, checks)
    attestation_path: Path | None = None
    attestation_sha256_path: Path | None = None
    attestation: dict[str, Any] | None = None
    existing_attestation: dict[str, Any] | None = None
    if manifest:
        attestation_path = _resolve_release_attestation_path(manifest, path)
        attestation_sha256_path = _resolve_release_attestation_sha256_path(manifest, attestation_path)
        result["attestation_path"] = str(attestation_path)
        result["attestation_sha256_path"] = str(attestation_sha256_path)
        attestation = _build_release_attestation(manifest, result, attestation_path)
        _verify_release_attestation_contract(attestation, checks)
    result["ok"] = all(check["ok"] for check in checks)
    result["summary"] = _verification_summary(checks)
    if manifest and attestation_path is not None and attestation_sha256_path is not None:
        expected_checks = checks + [
            _release_attestation_archive_pass_check(attestation_path, existing=attestation_path.is_file()),
            _release_attestation_sidecar_pass_check(
                attestation_sha256_path,
                attestation_path,
                existing=attestation_sha256_path.is_file(),
            ),
        ]
        expected_attestation = _build_release_attestation(
            manifest,
            _result_with_checks(result, expected_checks),
            attestation_path,
        )
        _verify_existing_release_attestation_contract(
            checks,
            attestation_path,
            expected_attestation,
            require_existing=require_archived_attestation,
        )
        if _check_ok(checks, "release_attestation_archive_contract"):
            existing_attestation = _read_release_attestation(attestation_path)
        sidecar_attestation = existing_attestation or expected_attestation
        _verify_release_attestation_sidecar_contract(
            checks,
            attestation_sha256_path,
            attestation_path,
            sidecar_attestation,
            require_existing=require_archived_attestation,
        )
        result["ok"] = all(check["ok"] for check in checks)
        result["summary"] = _verification_summary(checks)
        attestation = existing_attestation or _build_release_attestation(manifest, result, attestation_path)
    if write_artifacts:
        _write_verification_report(report_path, result)
    if (
        write_artifacts
        and attestation is not None
        and attestation_path is not None
        and attestation_sha256_path is not None
        and _check_ok(checks, "release_attestation_contract")
        and _check_ok(checks, "release_attestation_archive_contract")
        and _check_ok(checks, "release_attestation_sidecar_contract")
    ):
        if existing_attestation is None:
            _write_release_attestation(attestation_path, attestation)
        _write_release_attestation_sha256(attestation_sha256_path, attestation_path, attestation)
    return result


def _resolve_report_path(manifest_path: Path, out_path: Path | None) -> Path:
    if out_path is not None:
        return out_path.resolve()
    name = manifest_path.name
    if name.endswith("-ship-manifest.json"):
        return manifest_path.with_name(name.replace("-ship-manifest.json", "-verification-report.json"))
    return manifest_path.with_name(f"{manifest_path.stem}-verification-report.json")


def _write_verification_report(report_path: Path, result: dict[str, Any]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_release_attestation(attestation_path: Path, attestation: dict[str, Any]) -> None:
    attestation_path.parent.mkdir(parents=True, exist_ok=True)
    attestation_path.write_bytes(_release_attestation_bytes(attestation))


def _release_attestation_bytes(attestation: dict[str, Any]) -> bytes:
    return (json.dumps(attestation, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_release_attestation_sha256(
    sidecar_path: Path,
    attestation_path: Path,
    attestation: dict[str, Any],
) -> None:
    digest = hashlib.sha256(_release_attestation_bytes(attestation)).hexdigest()
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(f"{digest}  {attestation_path.name}\n", encoding="utf-8", newline="\n")


def _verification_summary(checks: list[dict[str, Any]]) -> dict[str, Any]:
    failed_ids = [str(check.get("id", "")) for check in checks if not check.get("ok")]
    return {
        "total": len(checks),
        "passed": len(checks) - len(failed_ids),
        "failed": len(failed_ids),
        "failed_ids": failed_ids,
    }


def _verify_verification_report_contract(result: dict[str, Any], checks: list[dict[str, Any]]) -> None:
    contract_errors: list[str] = []
    if result.get("schema_version") != 1:
        contract_errors.append("verification_report.schema_version must be 1")
    product = result.get("product")
    if not isinstance(product, dict):
        contract_errors.append("verification_report.product must be an object")
    else:
        if str(product.get("english_name", "")) != "VoiceVault":
            contract_errors.append("verification_report.product.english_name must be VoiceVault")
        if not isinstance(product.get("version"), str) or not product["version"].strip():
            contract_errors.append("verification_report.product.version must be a nonempty string")
    for field in ["manifest_path", "report_path"]:
        if not isinstance(result.get(field), str) or not result[field].strip():
            contract_errors.append(f"verification_report.{field} must be a nonempty string")
    if not isinstance(result.get("checks"), list) or not result["checks"]:
        contract_errors.append("verification_report.checks must be a nonempty list")
    summary = result.get("summary")
    if not isinstance(summary, dict):
        contract_errors.append("verification_report.summary must be an object")
    else:
        for field in ["total", "passed", "failed"]:
            if not isinstance(summary.get(field), int):
                contract_errors.append(f"verification_report.summary.{field} must be an integer")
        if not isinstance(summary.get("failed_ids"), list):
            contract_errors.append("verification_report.summary.failed_ids must be a list")
    _add_check(
        checks,
        "verification_report_contract",
        not contract_errors,
        "Verification report contract is complete." if not contract_errors else "Verification report contract is incomplete.",
        {"contract_errors": contract_errors},
    )


def _resolve_release_attestation_path(manifest: dict[str, Any], manifest_path: Path) -> Path:
    raw_path = manifest.get("artifacts", {}).get("release_attestation")
    if isinstance(raw_path, str) and raw_path.strip():
        return Path(raw_path).resolve()
    name = manifest_path.name
    if name.endswith("-ship-manifest.json"):
        return manifest_path.with_name(name.replace("-ship-manifest.json", "-release-attestation.json"))
    return manifest_path.with_name(f"{manifest_path.stem}-release-attestation.json")


def _resolve_release_attestation_sha256_path(manifest: dict[str, Any], attestation_path: Path) -> Path:
    raw_path = manifest.get("artifacts", {}).get("release_attestation_sha256_path")
    if isinstance(raw_path, str) and raw_path.strip():
        return Path(raw_path).resolve()
    return attestation_path.with_name(f"{attestation_path.name}.sha256")


def _build_release_attestation(
    manifest: dict[str, Any],
    result: dict[str, Any],
    attestation_path: Path,
) -> dict[str, Any]:
    artifacts = manifest.get("artifacts", {})
    cli_package = artifacts.get("cli_package", {}) if isinstance(artifacts, dict) else {}
    kb_release = artifacts.get("kb_release", {}) if isinstance(artifacts, dict) else {}
    ui = artifacts.get("ui", {}) if isinstance(artifacts, dict) else {}
    summary = result.get("summary", {})
    return {
        "schema_version": RELEASE_ATTESTATION_SCHEMA_VERSION,
        "product": manifest.get("product", {}),
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "status": "accepted" if result.get("ok") else "rejected",
        "manifest_path": str(result.get("manifest_path", "")),
        "verification_report": str(result.get("report_path", "")),
        "attestation_path": str(attestation_path),
        "summary": summary,
        "failed_ids": list(summary.get("failed_ids", [])) if isinstance(summary, dict) else [],
        "required_checks": [str(check.get("id", "")) for check in result.get("checks", []) if str(check.get("id", ""))],
        "artifact_sha256": {
            "cli_package": str(cli_package.get("sha256", "")) if isinstance(cli_package, dict) else "",
            "kb_release": str(kb_release.get("bundle_zip_sha256", "")) if isinstance(kb_release, dict) else "",
        },
        "artifacts": {
            "ship_manifest": str(result.get("manifest_path", "")),
            "ship_summary": str(artifacts.get("ship_summary", "")) if isinstance(artifacts, dict) else "",
            "verification_report": str(result.get("report_path", "")),
            "cli_package": str(cli_package.get("path", "")) if isinstance(cli_package, dict) else "",
            "kb_release": str(kb_release.get("bundle_zip", "")) if isinstance(kb_release, dict) else "",
            "local_ui": str(ui.get("index_html", "")) if isinstance(ui, dict) else "",
            "ui_data": str(ui.get("data_json", "")) if isinstance(ui, dict) else "",
        },
        "data_boundary": list(manifest.get("data_boundary", [])) if isinstance(manifest.get("data_boundary"), list) else [],
        "release_statement": "VoiceVault release verification completed for the referenced local artifacts.",
    }


def _verify_release_attestation_contract(attestation: dict[str, Any], checks: list[dict[str, Any]]) -> None:
    contract_errors: list[str] = []
    if attestation.get("schema_version") != RELEASE_ATTESTATION_SCHEMA_VERSION:
        contract_errors.append("release_attestation.schema_version must be 1")
    product = attestation.get("product")
    if not isinstance(product, dict):
        contract_errors.append("release_attestation.product must be an object")
    else:
        if str(product.get("english_name", "")) != "VoiceVault":
            contract_errors.append("release_attestation.product.english_name must be VoiceVault")
        if not isinstance(product.get("version"), str) or not product["version"].strip():
            contract_errors.append("release_attestation.product.version must be a nonempty string")
    status = attestation.get("status")
    if status not in {"accepted", "rejected"}:
        contract_errors.append("release_attestation.status must be accepted or rejected")
    for field in ["manifest_path", "verification_report", "attestation_path", "release_statement"]:
        value = attestation.get(field)
        if not isinstance(value, str) or not value.strip():
            contract_errors.append(f"release_attestation.{field} must be a nonempty string")
        elif "<repo_root>" in value:
            contract_errors.append(f"release_attestation.{field} must not contain <repo_root>")
    summary = attestation.get("summary")
    if not isinstance(summary, dict):
        contract_errors.append("release_attestation.summary must be an object")
    else:
        for field in ["total", "passed", "failed"]:
            if not isinstance(summary.get(field), int):
                contract_errors.append(f"release_attestation.summary.{field} must be an integer")
        if not isinstance(summary.get("failed_ids"), list):
            contract_errors.append("release_attestation.summary.failed_ids must be a list")
    if not isinstance(attestation.get("failed_ids"), list):
        contract_errors.append("release_attestation.failed_ids must be a list")
    required_checks = attestation.get("required_checks")
    if not isinstance(required_checks, list) or not required_checks:
        contract_errors.append("release_attestation.required_checks must be a nonempty list")
    else:
        for check_id in [
            "ship_manifest_contract",
            "verification_report_contract",
            "cli_package_entry_digests_contract",
            "kb_release_entry_digests_contract",
            "ship_summary_handoff",
        ]:
            if check_id not in required_checks:
                contract_errors.append(f"release_attestation.required_checks must include {check_id}")
    artifact_sha256 = attestation.get("artifact_sha256")
    if not isinstance(artifact_sha256, dict):
        contract_errors.append("release_attestation.artifact_sha256 must be an object")
    else:
        for field in ["cli_package", "kb_release"]:
            value = artifact_sha256.get(field)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                contract_errors.append(f"release_attestation.artifact_sha256.{field} must be a sha256 hex digest")
    artifacts = attestation.get("artifacts")
    if not isinstance(artifacts, dict):
        contract_errors.append("release_attestation.artifacts must be an object")
    else:
        for field in ["ship_manifest", "ship_summary", "verification_report", "cli_package", "kb_release", "local_ui", "ui_data"]:
            value = artifacts.get(field)
            if not isinstance(value, str) or not value.strip():
                contract_errors.append(f"release_attestation.artifacts.{field} must be a nonempty string")
    data_boundary = attestation.get("data_boundary")
    if not isinstance(data_boundary, list) or not data_boundary:
        contract_errors.append("release_attestation.data_boundary must be a nonempty list")
    elif any(not isinstance(item, str) or not item.strip() for item in data_boundary):
        contract_errors.append("release_attestation.data_boundary must contain nonempty strings")
    _add_check(
        checks,
        "release_attestation_contract",
        not contract_errors,
        (
            "Release attestation contract is complete."
            if not contract_errors
            else "Release attestation contract is incomplete."
        ),
        {"path": str(attestation.get("attestation_path", "")), "contract_errors": contract_errors},
    )


def _verify_existing_release_attestation_contract(
    checks: list[dict[str, Any]],
    attestation_path: Path,
    expected_attestation: dict[str, Any],
    *,
    require_existing: bool = False,
) -> None:
    contract_errors: list[str] = []
    if not attestation_path.is_file() and require_existing:
        contract_errors.append("existing release attestation is required for read-only audit")
    elif attestation_path.is_file():
        try:
            existing = json.loads(attestation_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            contract_errors.append(f"existing release attestation must be valid JSON: {exc}")
            existing = None
        except OSError as exc:
            contract_errors.append(f"existing release attestation is unreadable: {exc}")
            existing = None
        if existing is not None:
            if not isinstance(existing, dict):
                contract_errors.append("existing release attestation must contain a JSON object")
            elif _stable_attestation(existing) != _stable_attestation(expected_attestation):
                contract_errors.append("existing release attestation must match the current computed attestation")
    _add_check(
        checks,
        "release_attestation_archive_contract",
        not contract_errors,
        (
            "Existing release attestation matches the computed release attestation."
            if attestation_path.is_file() and not contract_errors
            else "Release attestation can be written because no existing attestation is present."
            if not contract_errors
            else "Existing release attestation contract is incomplete."
        ),
        {"path": str(attestation_path), "existing": attestation_path.is_file(), "contract_errors": contract_errors},
    )


def _read_release_attestation(attestation_path: Path) -> dict[str, Any] | None:
    try:
        existing = json.loads(attestation_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return existing if isinstance(existing, dict) else None


def _stable_attestation(attestation: dict[str, Any]) -> dict[str, Any]:
    stable = dict(attestation)
    stable.pop("generated_at", None)
    return stable


def _release_attestation_archive_pass_check(attestation_path: Path, *, existing: bool) -> dict[str, Any]:
    return {
        "id": "release_attestation_archive_contract",
        "ok": True,
        "message": (
            "Existing release attestation matches the computed release attestation."
            if existing
            else "Release attestation can be written because no existing attestation is present."
        ),
        "details": {"path": str(attestation_path), "existing": existing, "contract_errors": []},
    }


def _release_attestation_sidecar_pass_check(
    sidecar_path: Path,
    attestation_path: Path,
    *,
    existing: bool,
) -> dict[str, Any]:
    return {
        "id": "release_attestation_sidecar_contract",
        "ok": True,
        "message": (
            "Existing release attestation SHA256 sidecar matches the attestation."
            if existing
            else "Release attestation SHA256 sidecar can be written because no existing sidecar is present."
        ),
        "details": {
            "path": str(sidecar_path),
            "attestation": str(attestation_path),
            "existing": existing,
            "contract_errors": [],
        },
    }


def _verify_release_attestation_sidecar_contract(
    checks: list[dict[str, Any]],
    sidecar_path: Path,
    attestation_path: Path,
    expected_attestation: dict[str, Any],
    *,
    require_existing: bool = False,
) -> None:
    contract_errors: list[str] = []
    if not sidecar_path.is_file() and require_existing:
        contract_errors.append("release attestation sidecar is required for read-only audit")
    elif sidecar_path.is_file():
        expected_digest = hashlib.sha256(_release_attestation_bytes(expected_attestation)).hexdigest()
        try:
            text = sidecar_path.read_text(encoding="utf-8")
        except OSError as exc:
            contract_errors.append(f"release attestation sidecar is unreadable: {exc}")
        else:
            parts = text.strip().split()
            if len(parts) != 2:
                contract_errors.append("release attestation sidecar must contain '<sha256>  <filename>'")
            else:
                digest, filename = parts
                if digest != expected_digest:
                    contract_errors.append("release attestation sidecar digest must match the attestation")
                if filename != attestation_path.name:
                    contract_errors.append("release attestation sidecar filename must match the attestation filename")
    _add_check(
        checks,
        "release_attestation_sidecar_contract",
        not contract_errors,
        (
            "Existing release attestation SHA256 sidecar matches the attestation."
            if sidecar_path.is_file() and not contract_errors
            else "Release attestation SHA256 sidecar can be written because no existing sidecar is present."
            if not contract_errors
            else "Release attestation SHA256 sidecar contract is incomplete."
        ),
        {
            "path": str(sidecar_path),
            "attestation": str(attestation_path),
            "existing": sidecar_path.is_file(),
            "contract_errors": contract_errors,
        },
    )


def _result_with_checks(result: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    candidate = dict(result)
    candidate["checks"] = checks
    candidate["ok"] = all(check.get("ok") for check in checks)
    candidate["summary"] = _verification_summary(checks)
    return candidate


def _read_manifest(path: Path, checks: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not path.is_file():
        _add_check(checks, "ship_manifest", False, "Ship manifest file is missing.", {"path": str(path)})
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _add_check(checks, "ship_manifest", False, "Ship manifest is not valid JSON.", {"path": str(path), "error": str(exc)})
        return None
    _add_check(checks, "ship_manifest", True, "Ship manifest JSON is readable.", {"path": str(path)})
    return manifest


def _verify_ship_manifest_contract(manifest: dict[str, Any], checks: list[dict[str, Any]]) -> None:
    contract_errors: list[str] = []
    if manifest.get("schema_version") != 1:
        contract_errors.append("ship_manifest.schema_version must be 1")
    product = manifest.get("product")
    if not isinstance(product, dict):
        contract_errors.append("ship_manifest.product must be an object")
    else:
        if str(product.get("english_name", "")) != "VoiceVault":
            contract_errors.append("ship_manifest.product.english_name must be VoiceVault")
        if not isinstance(product.get("version"), str) or not product["version"].strip():
            contract_errors.append("ship_manifest.product.version must be a nonempty string")
    if not isinstance(manifest.get("ok"), bool):
        contract_errors.append("ship_manifest.ok must be a boolean")
    repo_root = str(manifest.get("repo_root", ""))
    if not repo_root:
        contract_errors.append("ship_manifest.repo_root must be a nonempty string")
    if "<repo_root>" in repo_root:
        contract_errors.append("ship_manifest.repo_root must not contain <repo_root>")
    if not str(manifest.get("knowledge_base", "")):
        contract_errors.append("ship_manifest.knowledge_base must be a nonempty string")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        contract_errors.append("ship_manifest.artifacts must be an object")
    else:
        for field in ["ship_summary", "verification_report"]:
            if not isinstance(artifacts.get(field), str) or not artifacts[field].strip():
                contract_errors.append(f"ship_manifest.artifacts.{field} must be a nonempty string")
        release_attestation = artifacts.get("release_attestation")
        if not isinstance(release_attestation, str) or not release_attestation.strip():
            contract_errors.append("ship_manifest.artifacts.release_attestation must be a nonempty string")
        elif "<repo_root>" in release_attestation:
            contract_errors.append("ship_manifest.artifacts.release_attestation must not contain <repo_root>")
        release_attestation_sha256_path = artifacts.get("release_attestation_sha256_path")
        if not isinstance(release_attestation_sha256_path, str) or not release_attestation_sha256_path.strip():
            contract_errors.append("ship_manifest.artifacts.release_attestation_sha256_path must be a nonempty string")
        elif "<repo_root>" in release_attestation_sha256_path:
            contract_errors.append("ship_manifest.artifacts.release_attestation_sha256_path must not contain <repo_root>")
        release_audit_summary = artifacts.get("release_audit_summary")
        if not isinstance(release_audit_summary, str) or not release_audit_summary.strip():
            contract_errors.append("ship_manifest.artifacts.release_audit_summary must be a nonempty string")
        elif "<repo_root>" in release_audit_summary:
            contract_errors.append("ship_manifest.artifacts.release_audit_summary must not contain <repo_root>")
        release_audit_summary_check = artifacts.get("release_audit_summary_check")
        if not isinstance(release_audit_summary_check, str) or not release_audit_summary_check.strip():
            contract_errors.append("ship_manifest.artifacts.release_audit_summary_check must be a nonempty string")
        elif "<repo_root>" in release_audit_summary_check:
            contract_errors.append("ship_manifest.artifacts.release_audit_summary_check must not contain <repo_root>")
        release_artifact_index = artifacts.get("release_artifact_index")
        if not isinstance(release_artifact_index, str) or not release_artifact_index.strip():
            contract_errors.append("ship_manifest.artifacts.release_artifact_index must be a nonempty string")
        elif "<repo_root>" in release_artifact_index:
            contract_errors.append("ship_manifest.artifacts.release_artifact_index must not contain <repo_root>")
        release_artifact_index_sha256 = artifacts.get("release_artifact_index_sha256")
        if not isinstance(release_artifact_index_sha256, str) or not release_artifact_index_sha256.strip():
            contract_errors.append("ship_manifest.artifacts.release_artifact_index_sha256 must be a nonempty string")
        elif "<repo_root>" in release_artifact_index_sha256:
            contract_errors.append("ship_manifest.artifacts.release_artifact_index_sha256 must not contain <repo_root>")
        _verify_ship_manifest_artifact_group(
            artifacts,
            "cli_package",
            ["path", "sha256", "sha256_path", "manifest", "install_guide"],
            contract_errors,
        )
        cli_package = artifacts.get("cli_package")
        if isinstance(cli_package, dict):
            entry_digests = cli_package.get("package_entry_sha256")
            if not isinstance(entry_digests, dict) or not entry_digests:
                contract_errors.append("ship_manifest.artifacts.cli_package.package_entry_sha256 must be a nonempty object")
        _verify_ship_manifest_artifact_group(
            artifacts,
            "kb_release",
            [
                "bundle_dir",
                "bundle_zip",
                "bundle_zip_sha256",
                "bundle_zip_sha256_path",
                "prepare_report",
                "release_summary",
                "release_plan",
                "quickstart_json",
                "quickstart_markdown",
            ],
            contract_errors,
        )
        kb_release = artifacts.get("kb_release")
        if isinstance(kb_release, dict):
            entry_digests = kb_release.get("bundle_entry_sha256")
            if not isinstance(entry_digests, dict) or not entry_digests:
                contract_errors.append("ship_manifest.artifacts.kb_release.bundle_entry_sha256 must be a nonempty object")
        _verify_ship_manifest_artifact_group(artifacts, "ui", ["index_html", "data_json"], contract_errors)

    readiness = manifest.get("readiness")
    if not isinstance(readiness, dict):
        contract_errors.append("ship_manifest.readiness must be an object")
    else:
        if readiness.get("schema_version") != 1:
            contract_errors.append("ship_manifest.readiness.schema_version must be 1")
        if not isinstance(readiness.get("ok"), bool):
            contract_errors.append("ship_manifest.readiness.ok must be a boolean")
        if not isinstance(readiness.get("summary"), dict):
            contract_errors.append("ship_manifest.readiness.summary must be an object")
        if not isinstance(readiness.get("checks"), list):
            contract_errors.append("ship_manifest.readiness.checks must be a list")

    data_boundary = manifest.get("data_boundary")
    if not isinstance(data_boundary, list) or not data_boundary:
        contract_errors.append("ship_manifest.data_boundary must be a nonempty list")
    elif any(not isinstance(item, str) or not item.strip() for item in data_boundary):
        contract_errors.append("ship_manifest.data_boundary must contain nonempty strings")

    _add_check(
        checks,
        "ship_manifest_contract",
        not contract_errors,
        "Ship manifest contract is complete." if not contract_errors else "Ship manifest contract is incomplete.",
        {"contract_errors": contract_errors},
    )


def _verify_ship_manifest_artifact_group(
    artifacts: dict[str, Any],
    group_name: str,
    fields: list[str],
    errors: list[str],
) -> None:
    group = artifacts.get(group_name)
    if not isinstance(group, dict):
        errors.append(f"ship_manifest.artifacts.{group_name} must be an object")
        return
    for field in fields:
        if not isinstance(group.get(field), str) or not group[field].strip():
            errors.append(f"ship_manifest.artifacts.{group_name}.{field} must be a nonempty string")


def _verify_cli_package(manifest: dict[str, Any], checks: list[dict[str, Any]]) -> None:
    cli = manifest["artifacts"]["cli_package"]
    package_path = Path(cli["path"])
    sidecar_path = Path(cli["sha256_path"])
    _add_check(checks, "cli_package", package_path.is_file(), "CLI package zip exists.", {"path": str(package_path)})
    _add_check(checks, "cli_package_sha256_sidecar", sidecar_path.is_file(), "CLI package SHA256 sidecar exists.", {"path": str(sidecar_path)})
    _verify_digest(checks, "cli_package_sha256", package_path, cli["sha256"])
    _verify_sidecar(checks, "cli_package_sha256_sidecar_match", sidecar_path, cli["sha256"])
    _verify_cli_sidecar_contract(checks, sidecar_path, cli["sha256"], package_path)
    _verify_cli_package_entry_digests(checks, package_path, cli.get("package_entry_sha256"))
    _verify_cli_zip_boundary(checks, package_path)
    _verify_cli_zip_contract(checks, package_path, str(manifest["product"]["version"]))
    _verify_cli_package_import_smoke(checks, package_path, str(manifest["product"]["version"]))
    _verify_cli_package_install_smoke(checks, package_path, str(manifest["product"]["version"]))
    install_path = Path(cli["install_guide"])
    manifest_path = Path(cli["manifest"])
    _add_check(checks, "cli_install_guide", install_path.is_file(), "CLI install guide exists.", {"path": str(install_path)})
    _verify_cli_install_guide_contract(checks, install_path)
    _add_check(checks, "cli_manifest", manifest_path.is_file(), "CLI distribution manifest exists.", {"path": str(manifest_path)})
    _verify_cli_external_manifest_contract(checks, manifest_path, cli, str(manifest["product"]["version"]))


def _verify_kb_release(manifest: dict[str, Any], checks: list[dict[str, Any]]) -> None:
    kb = manifest["artifacts"]["kb_release"]
    bundle_zip = Path(kb["bundle_zip"])
    sidecar_path = Path(kb["bundle_zip_sha256_path"])
    _add_check(checks, "kb_release_zip", bundle_zip.is_file(), "KB release zip exists.", {"path": str(bundle_zip)})
    _add_check(checks, "kb_release_sha256_sidecar", sidecar_path.is_file(), "KB release SHA256 sidecar exists.", {"path": str(sidecar_path)})
    _verify_digest(checks, "kb_release_sha256", bundle_zip, kb["bundle_zip_sha256"])
    _verify_sidecar(checks, "kb_release_sha256_sidecar_match", sidecar_path, kb["bundle_zip_sha256"])
    _verify_sha256_sidecar_contract(
        checks,
        "kb_release_sidecar_contract",
        sidecar_path,
        kb["bundle_zip_sha256"],
        bundle_zip.name,
        "KB release",
    )
    prepare_report = Path(kb["prepare_report"])
    _add_check(checks, "kb_prepare_report", prepare_report.is_file(), "KB prepare report exists.", {"path": str(prepare_report)})
    _verify_prepare_report_contract(
        checks,
        prepare_report,
        expected_knowledge_base=str(manifest.get("knowledge_base", "")),
        expected_repo_root=str(manifest.get("repo_root", "")),
    )
    quickstart_json = Path(kb.get("quickstart_json", ""))
    quickstart_markdown = Path(kb.get("quickstart_markdown", ""))
    _add_check(
        checks,
        "kb_quickstart_guide",
        quickstart_json.is_file() and quickstart_markdown.is_file(),
        "KB release quickstart guide exists.",
        {"quickstart_json": str(quickstart_json), "quickstart_markdown": str(quickstart_markdown)},
    )
    _verify_kb_bundle_entries(checks, bundle_zip)
    _verify_kb_release_entry_digests(checks, bundle_zip, kb.get("bundle_entry_sha256"))
    _verify_kb_bundle_contract(
        checks,
        bundle_zip,
        expected_version=str(manifest.get("product", {}).get("version", "")),
        expected_knowledge_base=str(manifest.get("knowledge_base", "")),
        expected_repo_root=str(manifest.get("repo_root", "")),
        expected_readiness_ok=bool(manifest.get("readiness", {}).get("ok")),
    )
    _verify_kb_release_handoff_docs_contract(
        checks,
        bundle_zip,
        expected_version=str(manifest.get("product", {}).get("version", "")),
        expected_knowledge_base=str(manifest.get("knowledge_base", "")),
        expected_repo_root=str(manifest.get("repo_root", "")),
    )
    _verify_quickstart_guide_contract(
        checks,
        quickstart_json,
        quickstart_markdown,
        expected_version=str(manifest.get("product", {}).get("version", "")),
        expected_knowledge_base=str(manifest.get("knowledge_base", "")),
        expected_repo_root=str(manifest.get("repo_root", "")),
    )


def _verify_ui_artifacts(manifest: dict[str, Any], checks: list[dict[str, Any]]) -> None:
    ui = manifest.get("artifacts", {}).get("ui", {})
    index_html = Path(ui.get("index_html", ""))
    data_json = Path(ui.get("data_json", ""))
    _add_check(
        checks,
        "kb_ui_data",
        index_html.is_file() and data_json.is_file(),
        "KB local UI data artifacts exist.",
        {"index_html": str(index_html), "data_json": str(data_json)},
    )
    _verify_ui_data_contract(
        checks,
        data_json,
        expected_version=str(manifest.get("product", {}).get("version", "")),
        expected_repo_root=str(manifest.get("repo_root", "")),
    )
    _verify_answer_export_contracts(checks, data_json)
    _verify_comparison_export_contracts(checks, data_json)
    _verify_ui_release_actions(
        checks,
        data_json,
        expected_version=str(manifest.get("product", {}).get("version", "")),
        expected_repo_root=str(manifest.get("repo_root", "")),
    )


def _verify_ship_summary_handoff(manifest: dict[str, Any], checks: list[dict[str, Any]]) -> None:
    summary_path = Path(manifest.get("artifacts", {}).get("ship_summary", ""))
    expected_version = str(manifest.get("product", {}).get("version", ""))
    expected_command = (
        f"voicevault release verify --manifest {manifest.get('repo_root', '')}\\dist\\"
        f"voicevault-v{expected_version}-ship-manifest.json --json"
    )
    expected_audit_summary = str(manifest.get("artifacts", {}).get("release_audit_summary", ""))
    expected_audit_summary_check = str(manifest.get("artifacts", {}).get("release_audit_summary_check", ""))
    expected_artifact_index = str(manifest.get("artifacts", {}).get("release_artifact_index", ""))
    expected_artifact_index_sha256 = str(manifest.get("artifacts", {}).get("release_artifact_index_sha256", ""))
    expected_audit_command = (
        f"voicevault release audit --manifest {manifest.get('repo_root', '')}\\dist\\"
        f"voicevault-v{expected_version}-ship-manifest.json --summary --summary-out {expected_audit_summary}"
    )
    expected_audit_check_command = (
        f"voicevault release audit --manifest {manifest.get('repo_root', '')}\\dist\\"
        f"voicevault-v{expected_version}-ship-manifest.json --summary-check {expected_audit_summary} "
        f"--summary-check-out {expected_audit_summary_check} --json"
    )
    expected_report = str(manifest.get("artifacts", {}).get("verification_report", ""))
    required_fragments = [
        "## Local UI",
        "## Quickstart Guide",
        "## Release Artifact Index",
        "## Post-Handoff Verification",
        "## Release Audit",
        "## Release Attestation",
        expected_command,
        expected_audit_command,
        expected_audit_check_command,
        "Verification report",
        "ship_manifest_contract",
        "verification_report_contract",
        "release_attestation_contract",
        "release_attestation_archive_contract",
        "release_attestation_sidecar_contract",
        "release_artifact_index_contract",
        "release_artifact_index_sidecar_contract",
        "cli_package_sidecar_contract",
        "cli_package_manifest_contract",
        "cli_install_guide_contract",
        "cli_package_distribution_manifest_contract",
        "cli_package_entry_digests_contract",
        "cli_package_install_smoke",
        "kb_prepare_report_contract",
        "kb_release_sidecar_contract",
        "kb_ui_release_actions",
        "kb_release_bundle_contract",
        "kb_release_entry_digests_contract",
        "kb_release_handoff_docs_contract",
        "role_coverage",
        "kb_answer_export_contracts",
        "kb_comparison_export_contracts",
        "kb_quickstart_guide_contract",
        "kb_release_quickstart_entries",
    ]
    required_fragments.append(expected_report if expected_report else "artifacts.verification_report")
    required_fragments.append(expected_audit_summary if expected_audit_summary else "artifacts.release_audit_summary")
    required_fragments.append(
        expected_audit_summary_check if expected_audit_summary_check else "artifacts.release_audit_summary_check"
    )
    required_fragments.append(expected_artifact_index if expected_artifact_index else "artifacts.release_artifact_index")
    required_fragments.append(
        expected_artifact_index_sha256 if expected_artifact_index_sha256 else "artifacts.release_artifact_index_sha256"
    )
    if not summary_path.is_file():
        _add_check(
            checks,
            "ship_summary_handoff",
            False,
            "Ship summary handoff file is missing.",
            {"path": str(summary_path), "missing_fragments": required_fragments},
        )
        return
    try:
        summary = summary_path.read_text(encoding="utf-8")
    except OSError as exc:
        _add_check(
            checks,
            "ship_summary_handoff",
            False,
            "Ship summary handoff file is unreadable.",
            {"path": str(summary_path), "error": str(exc), "missing_fragments": required_fragments},
        )
        return
    missing = [fragment for fragment in required_fragments if fragment not in summary]
    _add_check(
        checks,
        "ship_summary_handoff",
        not missing,
        "Ship summary contains human handoff sections and verification command.",
        {"path": str(summary_path), "missing_fragments": missing},
    )


def _verify_release_artifact_index(manifest: dict[str, Any], manifest_path: Path, checks: list[dict[str, Any]]) -> None:
    artifacts = manifest.get("artifacts", {})
    index_path_text = str(artifacts.get("release_artifact_index", "")) if isinstance(artifacts, dict) else ""
    contract_errors: list[str] = []
    payload: dict[str, Any] | None = None
    if not index_path_text.strip():
        contract_errors.append("ship_manifest.artifacts.release_artifact_index must be a nonempty string")
        index_path = Path()
    else:
        index_path = Path(index_path_text)
        if not index_path.is_file():
            contract_errors.append("release artifact index file is missing")
        else:
            try:
                loaded = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                contract_errors.append(f"release artifact index must be valid JSON: {exc}")
            else:
                if isinstance(loaded, dict):
                    payload = loaded
                else:
                    contract_errors.append("release artifact index must contain a JSON object")
    if payload is not None:
        _verify_release_artifact_index_payload(payload, manifest, manifest_path, contract_errors)
    _add_check(
        checks,
        "release_artifact_index_contract",
        not contract_errors,
        (
            "Release artifact index contract is complete."
            if not contract_errors
            else "Release artifact index contract is incomplete."
        ),
        {"path": str(index_path), "contract_errors": contract_errors},
    )


def _verify_release_artifact_index_sidecar(manifest: dict[str, Any], checks: list[dict[str, Any]]) -> None:
    artifacts = manifest.get("artifacts", {})
    index_path = Path(str(artifacts.get("release_artifact_index", ""))) if isinstance(artifacts, dict) else Path()
    sidecar_path = (
        Path(str(artifacts.get("release_artifact_index_sha256", ""))) if isinstance(artifacts, dict) else Path()
    )
    contract_errors: list[str] = []
    if not index_path.is_file():
        contract_errors.append("release artifact index file is missing")
    if not sidecar_path.is_file():
        contract_errors.append("release artifact index SHA256 sidecar is missing")
    else:
        try:
            sidecar_text = sidecar_path.read_text(encoding="utf-8")
        except OSError as exc:
            contract_errors.append(f"release artifact index SHA256 sidecar is unreadable: {exc}")
            sidecar_text = ""
        if sidecar_text:
            line = sidecar_text.strip()
            parts = line.split()
            if len(parts) != 2:
                contract_errors.append("release artifact index SHA256 sidecar must contain '<sha256>  <filename>'")
            else:
                digest, filename = parts
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    contract_errors.append("release artifact index SHA256 sidecar digest must be a sha256 hex digest")
                elif index_path.is_file() and digest != file_sha256(index_path):
                    contract_errors.append("release artifact index SHA256 sidecar digest must match artifact index")
                if filename != index_path.name:
                    contract_errors.append("release artifact index SHA256 sidecar filename must match artifact index filename")
    _add_check(
        checks,
        "release_artifact_index_sidecar_contract",
        not contract_errors,
        (
            "Release artifact index SHA256 sidecar contract is complete."
            if not contract_errors
            else "Release artifact index SHA256 sidecar contract is incomplete."
        ),
        {
            "path": str(sidecar_path),
            "artifact_index": str(index_path),
            "contract_errors": contract_errors,
        },
    )


def _verify_release_artifact_index_payload(
    payload: dict[str, Any],
    manifest: dict[str, Any],
    manifest_path: Path,
    contract_errors: list[str],
) -> None:
    if payload.get("schema_version") != 1:
        contract_errors.append("release_artifact_index.schema_version must be 1")
    product = payload.get("product")
    expected_version = str(manifest.get("product", {}).get("version", ""))
    if not isinstance(product, dict):
        contract_errors.append("release_artifact_index.product must be an object")
    elif str(product.get("version", "")) != expected_version:
        contract_errors.append(f"release_artifact_index.product.version must be {expected_version}")
    if str(payload.get("ship_manifest", "")) != str(manifest_path):
        contract_errors.append("release_artifact_index.ship_manifest must match the verified manifest path")

    commands = payload.get("commands")
    expected_commands = _release_artifact_index_expected_commands(manifest)
    if not isinstance(commands, dict):
        contract_errors.append("release_artifact_index.commands must be an object")
    else:
        for command_id, expected in expected_commands.items():
            value = commands.get(command_id)
            if not isinstance(value, str) or not value.strip():
                contract_errors.append(f"release_artifact_index.commands.{command_id} must be a nonempty string")
            elif value != expected:
                contract_errors.append(f"release_artifact_index.commands.{command_id} must match the release handoff command")
            _reject_unresolved_release_placeholders(
                f"release_artifact_index.commands.{command_id}",
                str(value or ""),
                contract_errors,
            )

    entries = payload.get("artifacts")
    expected_paths = _release_artifact_index_expected_paths(manifest, manifest_path)
    entry_by_id: dict[str, dict[str, Any]] = {}
    if not isinstance(entries, list) or not entries:
        contract_errors.append("release_artifact_index.artifacts must be a nonempty list")
    else:
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                contract_errors.append(f"release_artifact_index.artifacts[{index}] must be an object")
                continue
            artifact_id = str(entry.get("id", ""))
            if not artifact_id:
                contract_errors.append(f"release_artifact_index.artifacts[{index}].id must be a nonempty string")
                continue
            entry_by_id[artifact_id] = entry
            for field in ["kind", "path", "phase", "description"]:
                if not isinstance(entry.get(field), str) or not str(entry[field]).strip():
                    contract_errors.append(f"release_artifact_index.artifacts.{artifact_id}.{field} must be a nonempty string")
            if not isinstance(entry.get("required"), bool):
                contract_errors.append(f"release_artifact_index.artifacts.{artifact_id}.required must be a boolean")
            sha256 = entry.get("sha256", "")
            if sha256 and (not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256)):
                contract_errors.append(f"release_artifact_index.artifacts.{artifact_id}.sha256 must be a sha256 hex digest")
            _reject_unresolved_release_placeholders(
                f"release_artifact_index.artifacts.{artifact_id}.path",
                str(entry.get("path", "")),
                contract_errors,
            )

    for artifact_id, expected_path in expected_paths.items():
        entry = entry_by_id.get(artifact_id)
        if entry is None:
            contract_errors.append(f"release_artifact_index.artifacts must include {artifact_id}")
        elif str(entry.get("path", "")) != expected_path:
            contract_errors.append(f"release_artifact_index.artifacts.{artifact_id}.path must match ship manifest")

    expected_hashes = _release_artifact_index_expected_hashes(manifest)
    for artifact_id, expected_hash in expected_hashes.items():
        entry = entry_by_id.get(artifact_id)
        if entry is not None and str(entry.get("sha256", "")) != expected_hash:
            contract_errors.append(f"release_artifact_index.artifacts.{artifact_id}.sha256 must match ship manifest")

    data_boundary = payload.get("data_boundary")
    if not isinstance(data_boundary, list) or not data_boundary:
        contract_errors.append("release_artifact_index.data_boundary must be a nonempty list")
    elif any(not isinstance(item, str) or not item.strip() for item in data_boundary):
        contract_errors.append("release_artifact_index.data_boundary must contain nonempty strings")


def _release_artifact_index_expected_commands(manifest: dict[str, Any]) -> dict[str, str]:
    product = manifest.get("product", {})
    version = str(product.get("version", ""))
    repo_root = str(manifest.get("repo_root", ""))
    knowledge_base = str(manifest.get("knowledge_base", ""))
    artifacts = manifest.get("artifacts", {})
    release_audit_summary = str(artifacts.get("release_audit_summary", "")) if isinstance(artifacts, dict) else ""
    release_audit_summary_check = (
        str(artifacts.get("release_audit_summary_check", "")) if isinstance(artifacts, dict) else ""
    )
    return {
        "prepare": f"voicevault release prepare --kb {knowledge_base} --root {repo_root} --json",
        "ship": f"voicevault release ship --root {repo_root} --kb {knowledge_base} --json",
        "verify": f"voicevault release verify --manifest {repo_root}\\dist\\voicevault-v{version}-ship-manifest.json --json",
        "audit_summary": (
            f"voicevault release audit --manifest {repo_root}\\dist\\voicevault-v{version}-ship-manifest.json "
            f"--summary --summary-out {release_audit_summary}"
        ),
        "audit_summary_check": (
            f"voicevault release audit --manifest {repo_root}\\dist\\voicevault-v{version}-ship-manifest.json "
            f"--summary-check {release_audit_summary} --summary-check-out {release_audit_summary_check} --json"
        ),
        "inspect": f"voicevault release inspect --manifest {repo_root}\\dist\\voicevault-v{version}-ship-manifest.json --json",
    }


def _release_artifact_index_expected_paths(manifest: dict[str, Any], manifest_path: Path) -> dict[str, str]:
    artifacts = manifest.get("artifacts", {})
    cli = artifacts.get("cli_package", {}) if isinstance(artifacts, dict) else {}
    kb = artifacts.get("kb_release", {}) if isinstance(artifacts, dict) else {}
    ui = artifacts.get("ui", {}) if isinstance(artifacts, dict) else {}
    return {
        "ship_manifest": str(manifest_path),
        "ship_summary": str(artifacts.get("ship_summary", "")) if isinstance(artifacts, dict) else "",
        "release_artifact_index": str(artifacts.get("release_artifact_index", "")) if isinstance(artifacts, dict) else "",
        "release_artifact_index_sha256": (
            str(artifacts.get("release_artifact_index_sha256", "")) if isinstance(artifacts, dict) else ""
        ),
        "cli_package": str(cli.get("path", "")) if isinstance(cli, dict) else "",
        "cli_package_sha256": str(cli.get("sha256_path", "")) if isinstance(cli, dict) else "",
        "cli_package_manifest": str(cli.get("manifest", "")) if isinstance(cli, dict) else "",
        "cli_install_guide": str(cli.get("install_guide", "")) if isinstance(cli, dict) else "",
        "kb_release_zip": str(kb.get("bundle_zip", "")) if isinstance(kb, dict) else "",
        "kb_release_sha256": str(kb.get("bundle_zip_sha256_path", "")) if isinstance(kb, dict) else "",
        "kb_prepare_report": str(kb.get("prepare_report", "")) if isinstance(kb, dict) else "",
        "kb_release_summary": str(kb.get("release_summary", "")) if isinstance(kb, dict) else "",
        "kb_release_plan": str(kb.get("release_plan", "")) if isinstance(kb, dict) else "",
        "kb_quickstart_json": str(kb.get("quickstart_json", "")) if isinstance(kb, dict) else "",
        "kb_quickstart_markdown": str(kb.get("quickstart_markdown", "")) if isinstance(kb, dict) else "",
        "ui_index": str(ui.get("index_html", "")) if isinstance(ui, dict) else "",
        "ui_data": str(ui.get("data_json", "")) if isinstance(ui, dict) else "",
        "verification_report": str(artifacts.get("verification_report", "")) if isinstance(artifacts, dict) else "",
        "release_attestation": str(artifacts.get("release_attestation", "")) if isinstance(artifacts, dict) else "",
        "release_attestation_sha256": (
            str(artifacts.get("release_attestation_sha256_path", "")) if isinstance(artifacts, dict) else ""
        ),
        "release_audit_summary": str(artifacts.get("release_audit_summary", "")) if isinstance(artifacts, dict) else "",
        "release_audit_summary_check": (
            str(artifacts.get("release_audit_summary_check", "")) if isinstance(artifacts, dict) else ""
        ),
    }


def _release_artifact_index_expected_hashes(manifest: dict[str, Any]) -> dict[str, str]:
    artifacts = manifest.get("artifacts", {})
    cli = artifacts.get("cli_package", {}) if isinstance(artifacts, dict) else {}
    kb = artifacts.get("kb_release", {}) if isinstance(artifacts, dict) else {}
    return {
        "cli_package": str(cli.get("sha256", "")) if isinstance(cli, dict) else "",
        "kb_release_zip": str(kb.get("bundle_zip_sha256", "")) if isinstance(kb, dict) else "",
    }


def _verify_prepare_report_contract(
    checks: list[dict[str, Any]],
    prepare_report: Path,
    *,
    expected_knowledge_base: str,
    expected_repo_root: str,
) -> None:
    contract_errors: list[str] = []
    payload: dict[str, Any] | None = None
    if not prepare_report.is_file():
        contract_errors.append("release-prepare.json is missing")
    else:
        try:
            loaded = json.loads(prepare_report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            contract_errors.append(f"release-prepare.json must be valid JSON: {exc}")
        else:
            if isinstance(loaded, dict):
                payload = loaded
            else:
                contract_errors.append("release-prepare.json must contain a JSON object")
    if payload is not None:
        _verify_prepare_report_payload(
            payload,
            contract_errors,
            prepare_report=prepare_report,
            expected_knowledge_base=expected_knowledge_base,
            expected_repo_root=expected_repo_root,
        )
    _add_check(
        checks,
        "kb_prepare_report_contract",
        not contract_errors,
        "KB prepare report contract is complete." if not contract_errors else "KB prepare report contract is incomplete.",
        {"prepare_report": str(prepare_report), "contract_errors": contract_errors},
    )


def _verify_prepare_report_payload(
    payload: dict[str, Any],
    errors: list[str],
    *,
    prepare_report: Path,
    expected_knowledge_base: str,
    expected_repo_root: str,
) -> None:
    if payload.get("schema_version") != 1:
        errors.append("prepare_report.schema_version must be 1")
    if str(payload.get("root", "")) != expected_knowledge_base:
        errors.append(f"prepare_report.root must be {expected_knowledge_base}")
    if str(payload.get("repo_root", "")) != expected_repo_root:
        errors.append(f"prepare_report.repo_root must be {expected_repo_root}")
    if not isinstance(payload.get("ok"), bool):
        errors.append("prepare_report.ok must be a boolean")
    if not isinstance(payload.get("execute_jobs"), bool):
        errors.append("prepare_report.execute_jobs must be a boolean")
    if str(payload.get("prepare_report", "")) != str(prepare_report):
        errors.append("prepare_report.prepare_report must match the checked report path")

    _verify_prepare_steps(payload, errors)
    _require_prepare_object(payload, "source_adapter_validation", errors)
    source_adapter_validation = payload.get("source_adapter_validation")
    if isinstance(source_adapter_validation, dict):
        if not isinstance(source_adapter_validation.get("ok"), bool):
            errors.append("prepare_report.source_adapter_validation.ok must be a boolean")
        if not isinstance(source_adapter_validation.get("summary"), dict):
            errors.append("prepare_report.source_adapter_validation.summary must be an object")

    for name in ["source_jobs_before", "source_job_drain"]:
        section = _require_prepare_object(payload, name, errors)
        if isinstance(section, dict) and not isinstance(section.get("summary"), dict):
            errors.append(f"prepare_report.{name}.summary must be an object")
    source_job_drain = payload.get("source_job_drain")
    if isinstance(source_job_drain, dict):
        if not isinstance(source_job_drain.get("dry_run"), bool):
            errors.append("prepare_report.source_job_drain.dry_run must be a boolean")
        if not isinstance(source_job_drain.get("skipped"), bool):
            errors.append("prepare_report.source_job_drain.skipped must be a boolean")

    _require_prepare_string(payload, "dashboard", errors)
    ui = _require_prepare_object(payload, "ui", errors)
    if isinstance(ui, dict):
        _require_prepare_string(ui, "index_html", errors, prefix="prepare_report.ui")
        _require_prepare_string(ui, "data_json", errors, prefix="prepare_report.ui")
    quickstart = _require_prepare_object(payload, "quickstart", errors)
    if isinstance(quickstart, dict):
        _require_prepare_string(quickstart, "guide_json", errors, prefix="prepare_report.quickstart")
        _require_prepare_string(quickstart, "guide_markdown", errors, prefix="prepare_report.quickstart")
    bundle = _require_prepare_object(payload, "bundle", errors)
    if isinstance(bundle, dict):
        _require_prepare_string(bundle, "bundle_dir", errors, prefix="prepare_report.bundle")
        _require_prepare_string(bundle, "bundle_zip", errors, prefix="prepare_report.bundle")
        files = _require_prepare_object(bundle, "files", errors, prefix="prepare_report.bundle")
        if isinstance(files, dict):
            _require_prepare_string(files, "release_prepare", errors, prefix="prepare_report.bundle.files")
    release_check = _require_prepare_object(payload, "release_check", errors)
    if isinstance(release_check, dict):
        if release_check.get("schema_version") != 1:
            errors.append("prepare_report.release_check.schema_version must be 1")
        if not isinstance(release_check.get("ok"), bool):
            errors.append("prepare_report.release_check.ok must be a boolean")
        if not isinstance(release_check.get("summary"), dict):
            errors.append("prepare_report.release_check.summary must be an object")


def _verify_prepare_steps(payload: dict[str, Any], errors: list[str]) -> None:
    required_step_ids = [
        "source_adapters",
        "source_jobs",
        "dashboard",
        "ui",
        "quickstart",
        "release_bundle",
        "release_check",
    ]
    steps = payload.get("steps")
    if not isinstance(steps, list):
        errors.append("prepare_report.steps must be a list")
        return
    step_ids = [str(item.get("id", "")) for item in steps if isinstance(item, dict)]
    if step_ids != required_step_ids:
        errors.append(f"prepare_report.steps must include {required_step_ids}")
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"prepare_report.steps[{index}] must be an object")
            continue
        if not isinstance(step.get("id"), str) or not step["id"].strip():
            errors.append(f"prepare_report.steps[{index}].id must be a nonempty string")
        if not isinstance(step.get("ok"), bool):
            errors.append(f"prepare_report.steps[{index}].ok must be a boolean")
        if not isinstance(step.get("details"), dict):
            errors.append(f"prepare_report.steps[{index}].details must be an object")


def _require_prepare_object(
    payload: dict[str, Any],
    field: str,
    errors: list[str],
    *,
    prefix: str = "prepare_report",
) -> dict[str, Any] | None:
    value = payload.get(field)
    if not isinstance(value, dict):
        errors.append(f"{prefix}.{field} must be an object")
        return None
    return value


def _require_prepare_string(
    payload: dict[str, Any],
    field: str,
    errors: list[str],
    *,
    prefix: str = "prepare_report",
) -> None:
    if not isinstance(payload.get(field), str) or not payload[field].strip():
        errors.append(f"{prefix}.{field} must be a nonempty string")


def _verify_quickstart_guide_contract(
    checks: list[dict[str, Any]],
    quickstart_json: Path,
    quickstart_markdown: Path,
    *,
    expected_version: str,
    expected_knowledge_base: str,
    expected_repo_root: str,
) -> None:
    contract_errors: list[str] = []
    payload: dict[str, Any] | None = None
    markdown = ""
    if not quickstart_json.is_file():
        contract_errors.append("quickstart.json is missing")
    else:
        try:
            loaded = json.loads(quickstart_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            contract_errors.append(f"quickstart.json must be valid JSON: {exc}")
        else:
            if isinstance(loaded, dict):
                payload = loaded
            else:
                contract_errors.append("quickstart.json must contain a JSON object")

    if not quickstart_markdown.is_file():
        contract_errors.append("quickstart.md is missing")
    else:
        try:
            markdown = quickstart_markdown.read_text(encoding="utf-8")
        except OSError as exc:
            contract_errors.append(f"quickstart.md must be readable: {exc}")

    if payload is not None:
        _verify_quickstart_json_contract(
            payload,
            contract_errors,
            expected_version=expected_version,
            expected_knowledge_base=expected_knowledge_base,
            expected_repo_root=expected_repo_root,
        )

    _require_text_fragments(
        markdown,
        "quickstart.md",
        [
            "# VoiceVault Quickstart Guide",
            expected_version,
            expected_knowledge_base,
            expected_repo_root,
            "## Next Actions",
            "## Repair and inspect the local knowledge base",
            "## Verify the final ship manifest",
            "## Data Boundary",
            "voicevault release verify --manifest",
        ],
        contract_errors,
    )

    _add_check(
        checks,
        "kb_quickstart_guide_contract",
        not contract_errors,
        "KB quickstart guide contract is complete." if not contract_errors else "KB quickstart guide contract is incomplete.",
        {
            "quickstart_json": str(quickstart_json),
            "quickstart_markdown": str(quickstart_markdown),
            "contract_errors": contract_errors,
        },
    )


def _verify_quickstart_json_contract(
    payload: dict[str, Any],
    errors: list[str],
    *,
    expected_version: str,
    expected_knowledge_base: str,
    expected_repo_root: str,
) -> None:
    if payload.get("schema_version") != 1:
        errors.append("quickstart.schema_version must be 1")
    product = payload.get("product")
    if not isinstance(product, dict):
        errors.append("quickstart.product must be an object")
    else:
        if str(product.get("english_name", "")) != "VoiceVault":
            errors.append("quickstart.product.english_name must be VoiceVault")
        if str(product.get("version", "")) != expected_version:
            errors.append(f"quickstart.product.version must be {expected_version}")
    if str(payload.get("knowledge_base", "")) != expected_knowledge_base:
        errors.append(f"quickstart.knowledge_base must be {expected_knowledge_base}")
    if str(payload.get("repo_root", "")) != expected_repo_root:
        errors.append(f"quickstart.repo_root must be {expected_repo_root}")
    if not isinstance(payload.get("release_ready"), bool):
        errors.append("quickstart.release_ready must be a boolean")

    readiness_summary = payload.get("readiness_summary")
    if not isinstance(readiness_summary, dict):
        errors.append("quickstart.readiness_summary must be an object")
    else:
        for field in [
            "roles",
            "statements",
            "events",
            "reports",
            "answer_exports",
            "deliverable_answer_exports",
            "comparison_exports",
            "adopted_comparison_exports",
            "analysis_exports",
            "source_configs",
            "source_jobs_pending",
            "capture_pending",
        ]:
            if field not in readiness_summary:
                errors.append(f"quickstart.readiness_summary.{field} is required")

    commands = _verify_quickstart_phases(payload, errors)
    commands_text = "\n".join(commands)
    for fragment in [
        "voicevault doctor --kb",
        "voicevault capture append",
        "voicevault sources drain",
        "voicevault analyze --kb",
        "voicevault answer --kb",
        "voicevault release ship",
        "voicevault release verify",
    ]:
        if fragment not in commands_text:
            errors.append(f"quickstart.commands must include {fragment}")
    if "<repo_root>" in commands_text:
        errors.append("quickstart.commands must not contain <repo_root>")
    expected_verify_command = (
        f"voicevault release verify --manifest {expected_repo_root}\\dist\\"
        f"voicevault-v{expected_version}-ship-manifest.json --json"
    )
    if expected_verify_command not in commands_text:
        errors.append("quickstart.commands must include the root-aware final release verify command")

    next_actions = payload.get("next_actions")
    if not isinstance(next_actions, list) or not next_actions:
        errors.append("quickstart.next_actions must be a nonempty list")
    elif next_actions:
        for index, item in enumerate(next_actions):
            if not isinstance(item, dict):
                errors.append(f"quickstart.next_actions[{index}] must be an object")
                continue
            for field in ["phase", "action", "command"]:
                if not isinstance(item.get(field), str) or not item[field].strip():
                    errors.append(f"quickstart.next_actions[{index}].{field} must be a nonempty string")
            command = str(item.get("command", ""))
            if "<repo_root>" in command:
                errors.append(f"quickstart.next_actions[{index}].command must not contain <repo_root>")

    data_boundary = payload.get("data_boundary")
    if not isinstance(data_boundary, list) or not data_boundary:
        errors.append("quickstart.data_boundary must be a nonempty list")
    elif any(not isinstance(item, str) or not item.strip() for item in data_boundary):
        errors.append("quickstart.data_boundary must contain nonempty strings")


def _verify_quickstart_phases(payload: dict[str, Any], errors: list[str]) -> list[str]:
    required_phase_ids = [
        "setup",
        "capture",
        "source_jobs",
        "research_outputs",
        "release_handoff",
        "post_handoff_verify",
    ]
    phases = payload.get("phases")
    commands: list[str] = []
    if not isinstance(phases, list):
        errors.append("quickstart.phases must be a list")
        return commands
    phase_ids = [str(item.get("id", "")) for item in phases if isinstance(item, dict)]
    if phase_ids != required_phase_ids:
        errors.append(f"quickstart.phases must be {required_phase_ids}")
    for index, phase in enumerate(phases):
        if not isinstance(phase, dict):
            errors.append(f"quickstart.phases[{index}] must be an object")
            continue
        for field in ["id", "title", "goal"]:
            if not isinstance(phase.get(field), str) or not phase[field].strip():
                errors.append(f"quickstart.phases[{index}].{field} must be a nonempty string")
        phase_commands = phase.get("commands")
        if not isinstance(phase_commands, list) or not phase_commands:
            errors.append(f"quickstart.phases[{index}].commands must be a nonempty list")
            continue
        for command_index, command in enumerate(phase_commands):
            if not isinstance(command, str) or not command.strip():
                errors.append(f"quickstart.phases[{index}].commands[{command_index}] must be a nonempty string")
                continue
            commands.append(command)
    return commands


def _verify_ui_data_contract(
    checks: list[dict[str, Any]],
    data_json: Path,
    *,
    expected_version: str,
    expected_repo_root: str,
) -> None:
    if not data_json.is_file():
        _add_check(
            checks,
            "kb_ui_data_contract",
            False,
            "Cannot verify UI data contract because data.json is missing.",
            {
                "data_json": str(data_json),
                "missing_fields": ["data_json"],
                "contract_errors": ["data_json is missing"],
            },
        )
        return
    try:
        data = json.loads(data_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _add_check(
            checks,
            "kb_ui_data_contract",
            False,
            "Cannot verify UI data contract because data.json is unreadable or invalid JSON.",
            {
                "data_json": str(data_json),
                "error": str(exc),
                "missing_fields": [],
                "contract_errors": ["data_json must be valid JSON"],
            },
        )
        return
    if not isinstance(data, dict):
        _add_check(
            checks,
            "kb_ui_data_contract",
            False,
            "KB local UI data contract is invalid.",
            {
                "data_json": str(data_json),
                "missing_fields": [],
                "contract_errors": ["data.json must contain a JSON object"],
            },
        )
        return

    required_dicts = [
        "product",
        "summary",
        "capture_status",
        "sync_status",
        "release_readiness",
        "role_coverage",
        "role_skills",
        "role_skill_coverage",
        "role_agent_audit",
        "role_agent_readiness",
        "role_agent_runtime",
        "source_adapter_validation",
        "source_status",
        "source_import_status",
        "source_job_status",
        "action_runs",
        "remediation_queue",
        "answer_quality",
        "answer_regression",
        "next_action_audit",
    ]
    required_lists = [
        "roles",
        "events",
        "reports",
        "analysis_exports",
        "source_configs",
        "answer_exports",
        "comparison_exports",
        "role_agent_exports",
        "statements",
        "release_actions",
        "next_actions",
    ]
    required_summary = [
        "roles",
        "statements",
        "events",
        "reports",
        "analysis_exports",
        "answer_exports",
        "comparison_exports",
        "adopted_comparison_exports",
        "reviewed_roles_with_statements",
        "min_reviewed_roles",
        "release_ready",
        "release_blockers",
        "next_actions",
        "completed_next_actions",
        "capture_pending",
        "capture_failed",
        "source_configs",
        "source_jobs",
        "action_runs",
        "action_run_failed",
        "action_run_retryable_failed",
        "remediation_items",
        "remediation_ready",
        "remediation_blocked",
        "answer_quality_passed",
        "answer_quality_review",
        "answer_quality_failed",
        "answer_regression_passed",
        "answer_regression_review",
        "answer_regression_failed",
        "answer_regression_questions",
        "answer_regression_min_questions",
        "answer_regression_missing_provenance",
        "role_skills",
        "role_skills_ready",
        "role_skills_missing",
        "role_agent_exports",
        "role_agent_completed",
        "role_agent_prompt_only",
        "role_agent_deliverable",
        "role_agent_failed",
        "role_agent_invalid_completed",
        "role_agent_roles_prompt_ready",
        "role_agent_roles_live_ready",
        "role_agent_roles_missing_prompt",
        "role_agent_roles_missing_live",
    ]

    missing_fields: list[str] = []
    contract_errors: list[str] = []
    if data.get("schema_version") != 1:
        contract_errors.append("schema_version must be 1")

    product = data.get("product")
    if isinstance(product, dict):
        actual_version = str(product.get("version", ""))
        if actual_version != expected_version:
            contract_errors.append(f"product.version must be {expected_version}")

    actual_repo_root = str(data.get("repo_root", ""))
    if actual_repo_root != expected_repo_root:
        contract_errors.append(f"repo_root must be {expected_repo_root}")

    for field in required_dicts:
        if not isinstance(data.get(field), dict):
            missing_fields.append(field)
    for field in required_lists:
        if not isinstance(data.get(field), list):
            missing_fields.append(field)

    summary = data.get("summary")
    if isinstance(summary, dict):
        for field in required_summary:
            if field not in summary:
                missing_fields.append(f"summary.{field}")

    next_actions = data.get("next_actions")
    if isinstance(next_actions, list):
        for index, item in enumerate(next_actions):
            if not isinstance(item, dict):
                contract_errors.append(f"next_actions[{index}] must be an object")
                continue
            for field in ["id", "phase", "action_type", "label", "action", "payload", "audit"]:
                if field == "payload":
                    if not isinstance(item.get(field), dict):
                        contract_errors.append(f"next_actions[{index}].payload must be an object")
                elif field == "audit":
                    audit = item.get(field)
                    if not isinstance(audit, dict):
                        contract_errors.append(f"next_actions[{index}].audit must be an object")
                    else:
                        _verify_next_action_audit_payload(
                            audit,
                            prefix=f"next_actions[{index}].audit",
                            contract_errors=contract_errors,
                        )
                elif not str(item.get(field) or "").strip():
                    contract_errors.append(f"next_actions[{index}].{field} must be a nonempty string")
            endpoint = str(item.get("endpoint") or "").strip()
            command = str(item.get("command") or "").strip()
            if not endpoint and not command:
                contract_errors.append(f"next_actions[{index}] must include endpoint or command")

    action_audit = data.get("next_action_audit")
    if isinstance(action_audit, dict):
        if action_audit.get("schema_version") != 1:
            contract_errors.append("next_action_audit.schema_version must be 1")
        if not isinstance(action_audit.get("summary"), dict):
            contract_errors.append("next_action_audit.summary must be an object")
        completed_actions = action_audit.get("completed_actions")
        if not isinstance(completed_actions, list):
            contract_errors.append("next_action_audit.completed_actions must be a list")
        else:
            for index, item in enumerate(completed_actions):
                if not isinstance(item, dict):
                    contract_errors.append(f"next_action_audit.completed_actions[{index}] must be an object")
                    continue
                for field in ["id", "status", "phase", "action_type", "label", "action", "reason", "payload"]:
                    if field == "payload":
                        if not isinstance(item.get(field), dict):
                            contract_errors.append(
                                f"next_action_audit.completed_actions[{index}].payload must be an object"
                            )
                    elif not str(item.get(field) or "").strip():
                        contract_errors.append(f"next_action_audit.completed_actions[{index}].{field} must be a nonempty string")
                completed_by = item.get("completed_by")
                if not isinstance(completed_by, dict):
                    contract_errors.append(f"next_action_audit.completed_actions[{index}].completed_by must be an object")
                elif not str(completed_by.get("kind") or "").strip() or not str(completed_by.get("path") or "").strip():
                    contract_errors.append(
                        f"next_action_audit.completed_actions[{index}].completed_by must include kind and path"
                    )
                audit = item.get("audit")
                if not isinstance(audit, dict):
                    contract_errors.append(f"next_action_audit.completed_actions[{index}].audit must be an object")
                else:
                    _verify_next_action_audit_payload(
                        audit,
                        prefix=f"next_action_audit.completed_actions[{index}].audit",
                        contract_errors=contract_errors,
                    )

    action_runs = data.get("action_runs")
    if isinstance(action_runs, dict):
        if action_runs.get("schema_version") != 1:
            contract_errors.append("action_runs.schema_version must be 1")
        if not isinstance(action_runs.get("summary"), dict):
            contract_errors.append("action_runs.summary must be an object")
        runs = action_runs.get("runs")
        if not isinstance(runs, list):
            contract_errors.append("action_runs.runs must be a list")
        else:
            for index, run in enumerate(runs):
                if not isinstance(run, dict):
                    contract_errors.append(f"action_runs.runs[{index}] must be an object")
                    continue
                for field in ["run_id", "action_type", "status", "source", "started_at", "completed_at"]:
                    if not str(run.get(field) or "").strip():
                        contract_errors.append(f"action_runs.runs[{index}].{field} must be a nonempty string")
                if run.get("status") not in {"completed", "failed"}:
                    contract_errors.append(f"action_runs.runs[{index}].status must be completed or failed")
                if not isinstance(run.get("retryable"), bool):
                    contract_errors.append(f"action_runs.runs[{index}].retryable must be a boolean")
                if not isinstance(run.get("payload"), dict):
                    contract_errors.append(f"action_runs.runs[{index}].payload must be an object")
                if not isinstance(run.get("result"), dict):
                    contract_errors.append(f"action_runs.runs[{index}].result must be an object")
                if run.get("status") == "failed" and not str(run.get("error") or "").strip():
                    contract_errors.append(f"action_runs.runs[{index}].error must be nonempty for failed runs")
                if str(run.get("resolved_by") or "").strip() and not str(run.get("resolved_at") or "").strip():
                    contract_errors.append(f"action_runs.runs[{index}].resolved_at must be nonempty when resolved_by is set")

    remediation_queue = data.get("remediation_queue")
    if isinstance(remediation_queue, dict):
        if remediation_queue.get("schema_version") != 1:
            contract_errors.append("remediation_queue.schema_version must be 1")
        if not isinstance(remediation_queue.get("summary"), dict):
            contract_errors.append("remediation_queue.summary must be an object")
        remediation_items = remediation_queue.get("items")
        if not isinstance(remediation_items, list):
            contract_errors.append("remediation_queue.items must be a list")
        else:
            for index, item in enumerate(remediation_items):
                if not isinstance(item, dict):
                    contract_errors.append(f"remediation_queue.items[{index}] must be an object")
                    continue
                for field in ["id", "status", "severity", "phase", "action_type", "label", "action", "reason"]:
                    if not str(item.get(field) or "").strip():
                        contract_errors.append(f"remediation_queue.items[{index}].{field} must be a nonempty string")
                if item.get("status") not in {"ready", "blocked"}:
                    contract_errors.append(f"remediation_queue.items[{index}].status must be ready or blocked")
                if item.get("severity") not in {"high", "medium", "low"}:
                    contract_errors.append(f"remediation_queue.items[{index}].severity must be high, medium, or low")
                if not isinstance(item.get("payload"), dict):
                    contract_errors.append(f"remediation_queue.items[{index}].payload must be an object")
                if not isinstance(item.get("source"), dict):
                    contract_errors.append(f"remediation_queue.items[{index}].source must be an object")
                endpoint = str(item.get("endpoint") or "").strip()
                command = str(item.get("command") or "").strip()
                if item.get("status") == "ready" and not endpoint and not command:
                    contract_errors.append(f"remediation_queue.items[{index}] ready item must include endpoint or command")

    answer_quality = data.get("answer_quality")
    if isinstance(answer_quality, dict):
        if answer_quality.get("schema_version") != 1:
            contract_errors.append("answer_quality.schema_version must be 1")
        if not isinstance(answer_quality.get("summary"), dict):
            contract_errors.append("answer_quality.summary must be an object")
        quality_items = answer_quality.get("items")
        if not isinstance(quality_items, list):
            contract_errors.append("answer_quality.items must be a list")
        else:
            for index, item in enumerate(quality_items):
                if not isinstance(item, dict):
                    contract_errors.append(f"answer_quality.items[{index}] must be an object")
                    continue
                for field in ["status", "query", "answer_json"]:
                    if not str(item.get(field) or "").strip():
                        contract_errors.append(f"answer_quality.items[{index}].{field} must be a nonempty string")
                if item.get("status") not in {"pass", "review", "fail"}:
                    contract_errors.append(f"answer_quality.items[{index}].status must be pass, review, or fail")
                if not isinstance(item.get("score"), int):
                    contract_errors.append(f"answer_quality.items[{index}].score must be an integer")
                if not isinstance(item.get("checks"), list):
                    contract_errors.append(f"answer_quality.items[{index}].checks must be a list")
                if not isinstance(item.get("failed_checks"), list):
                    contract_errors.append(f"answer_quality.items[{index}].failed_checks must be a list")
                if not isinstance(item.get("payload"), dict):
                    contract_errors.append(f"answer_quality.items[{index}].payload must be an object")
                if item.get("status") != "pass" and not str(item.get("recommended_endpoint") or "").strip():
                    contract_errors.append(
                        f"answer_quality.items[{index}].recommended_endpoint must be set for non-pass answers"
                    )

    answer_regression = data.get("answer_regression")
    if isinstance(answer_regression, dict):
        if answer_regression.get("schema_version") != 1:
            contract_errors.append("answer_regression.schema_version must be 1")
        if not isinstance(answer_regression.get("summary"), dict):
            contract_errors.append("answer_regression.summary must be an object")
        if not str(answer_regression.get("suite_path") or "").strip():
            contract_errors.append("answer_regression.suite_path must be a nonempty string")
        if not isinstance(answer_regression.get("errors"), list):
            contract_errors.append("answer_regression.errors must be a list")
        regression_items = answer_regression.get("items")
        if not isinstance(regression_items, list):
            contract_errors.append("answer_regression.items must be a list")
        else:
            for index, item in enumerate(regression_items):
                if not isinstance(item, dict):
                    contract_errors.append(f"answer_regression.items[{index}] must be an object")
                    continue
                for field in ["id", "status", "query"]:
                    if not str(item.get(field) or "").strip():
                        contract_errors.append(f"answer_regression.items[{index}].{field} must be a nonempty string")
                if item.get("status") not in {"pass", "review", "fail"}:
                    contract_errors.append(f"answer_regression.items[{index}].status must be pass, review, or fail")
                if not isinstance(item.get("score"), int):
                    contract_errors.append(f"answer_regression.items[{index}].score must be an integer")
                if not isinstance(item.get("checks"), list):
                    contract_errors.append(f"answer_regression.items[{index}].checks must be a list")
                if not isinstance(item.get("failed_checks"), list):
                    contract_errors.append(f"answer_regression.items[{index}].failed_checks must be a list")
                if not isinstance(item.get("payload"), dict):
                    contract_errors.append(f"answer_regression.items[{index}].payload must be an object")
                if item.get("status") != "pass" and not str(item.get("recommended_endpoint") or "").strip():
                    contract_errors.append(
                        f"answer_regression.items[{index}].recommended_endpoint must be set for non-pass questions"
                    )

    ok = not missing_fields and not contract_errors
    message = "KB local UI data contract is complete." if ok else "KB local UI data contract is incomplete."
    _add_check(
        checks,
        "kb_ui_data_contract",
        ok,
        message,
        {
            "data_json": str(data_json),
            "expected_version": expected_version,
            "expected_repo_root": expected_repo_root,
            "missing_fields": missing_fields,
            "contract_errors": contract_errors,
        },
    )


def _verify_next_action_audit_payload(
    audit: dict[str, Any],
    *,
    prefix: str,
    contract_errors: list[str],
) -> None:
    for field in ["state", "trigger", "reason", "completion_key"]:
        if not str(audit.get(field) or "").strip():
            contract_errors.append(f"{prefix}.{field} must be a nonempty string")
    completion = audit.get("completion")
    if not isinstance(completion, dict):
        contract_errors.append(f"{prefix}.completion must be an object")
    elif not str(completion.get("status") or "").strip():
        contract_errors.append(f"{prefix}.completion.status must be a nonempty string")


def _verify_ui_release_actions(
    checks: list[dict[str, Any]],
    data_json: Path,
    *,
    expected_version: str,
    expected_repo_root: str,
) -> None:
    if not data_json.is_file():
        _add_check(
            checks,
            "kb_ui_release_actions",
            False,
            "Cannot verify UI release actions because data.json is missing.",
            {
                "data_json": str(data_json),
                "expected_version": expected_version,
                "expected_repo_root": expected_repo_root,
            },
        )
        return
    try:
        data = json.loads(data_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _add_check(
            checks,
            "kb_ui_release_actions",
            False,
            "Cannot verify UI release actions because data.json is unreadable or invalid JSON.",
            {
                "data_json": str(data_json),
                "error": str(exc),
                "expected_version": expected_version,
                "expected_repo_root": expected_repo_root,
            },
        )
        return

    actual_version = str(data.get("product", {}).get("version", ""))
    actual_repo_root = str(data.get("repo_root", ""))
    actions = data.get("release_actions", [])
    placeholder_commands = (
        [
            str(action.get("command", ""))
            for action in actions
            if "<repo_root>" in str(action.get("command", ""))
        ]
        if isinstance(actions, list)
        else []
    )
    ok = (
        actual_version == expected_version
        and actual_repo_root == expected_repo_root
        and isinstance(actions, list)
        and len(actions) > 0
        and not placeholder_commands
    )
    _add_check(
        checks,
        "kb_ui_release_actions",
        ok,
        "KB local UI release actions are versioned and root-aware.",
        {
            "data_json": str(data_json),
            "expected_version": expected_version,
            "actual_version": actual_version,
            "expected_repo_root": expected_repo_root,
            "actual_repo_root": actual_repo_root,
            "action_count": len(actions) if isinstance(actions, list) else 0,
            "placeholder_commands": placeholder_commands[:6],
        },
    )


def _verify_answer_export_contracts(checks: list[dict[str, Any]], data_json: Path) -> None:
    if not data_json.is_file():
        _add_check(
            checks,
            "kb_answer_export_contracts",
            False,
            "Cannot verify answer export contracts because UI data.json is missing.",
            {"data_json": str(data_json), "invalid_paths": [], "invalid_exports": []},
        )
        return
    try:
        data = json.loads(data_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _add_check(
            checks,
            "kb_answer_export_contracts",
            False,
            "Cannot verify answer export contracts because UI data.json is unreadable or invalid JSON.",
            {"data_json": str(data_json), "error": str(exc), "invalid_paths": [], "invalid_exports": []},
        )
        return
    if not isinstance(data, dict) or not isinstance(data.get("answer_exports"), list):
        _add_check(
            checks,
            "kb_answer_export_contracts",
            False,
            "KB answer export contract list is missing from UI data.",
            {
                "data_json": str(data_json),
                "total_exports": 0,
                "deliverable_answer_exports": 0,
                "invalid_paths": [],
                "invalid_exports": [
                    {
                        "answer_json": "",
                        "status": "malformed",
                        "schema_version": 0,
                        "contract_errors": ["answer_exports must be a list"],
                        "error": "",
                    }
                ],
            },
        )
        return

    inspected: list[dict[str, Any]] = []
    invalid_exports: list[dict[str, Any]] = []
    for index, item in enumerate(data["answer_exports"]):
        if not isinstance(item, dict) or not str(item.get("answer_json", "")).strip():
            invalid_exports.append(
                {
                    "answer_json": "",
                    "status": "malformed",
                    "schema_version": 0,
                    "contract_errors": [f"answer_exports[{index}].answer_json must be a path"],
                    "error": "",
                }
            )
            continue
        answer_item = inspect_answer_export(Path(str(item["answer_json"])))
        inspected.append(answer_item)
        if not is_deliverable_answer_export(answer_item):
            invalid_exports.append(_answer_export_problem(answer_item))

    deliverable_answer_exports = [item for item in inspected if is_deliverable_answer_export(item)]
    ok = bool(deliverable_answer_exports) and not invalid_exports
    message = (
        "KB answer export contracts are complete."
        if ok
        else "KB answer export contracts are missing or contain invalid exports."
    )
    _add_check(
        checks,
        "kb_answer_export_contracts",
        ok,
        message,
        {
            "data_json": str(data_json),
            "total_exports": len(data["answer_exports"]),
            "deliverable_answer_exports": len(deliverable_answer_exports),
            "invalid_paths": [item["answer_json"] for item in invalid_exports if item.get("answer_json")],
            "invalid_exports": invalid_exports,
        },
    )


def _verify_comparison_export_contracts(checks: list[dict[str, Any]], data_json: Path) -> None:
    if not data_json.is_file():
        _add_check(
            checks,
            "kb_comparison_export_contracts",
            False,
            "Cannot verify comparison export contracts because UI data.json is missing.",
            {"data_json": str(data_json), "invalid_paths": [], "invalid_exports": []},
        )
        return
    try:
        data = json.loads(data_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _add_check(
            checks,
            "kb_comparison_export_contracts",
            False,
            "Cannot verify comparison export contracts because UI data.json is unreadable or invalid JSON.",
            {"data_json": str(data_json), "error": str(exc), "invalid_paths": [], "invalid_exports": []},
        )
        return
    if not isinstance(data, dict) or not isinstance(data.get("comparison_exports"), list):
        _add_check(
            checks,
            "kb_comparison_export_contracts",
            False,
            "KB comparison export contract list is missing from UI data.",
            {
                "data_json": str(data_json),
                "total_exports": 0,
                "adopted_comparison_exports": 0,
                "draft_comparison_exports": 0,
                "invalid_paths": [],
                "invalid_exports": [
                    {
                        "comparison_json": "",
                        "status": "malformed",
                        "review_status": "draft",
                        "schema_version": 0,
                        "contract_errors": ["comparison_exports must be a list"],
                        "error": "",
                    }
                ],
            },
        )
        return

    inspected: list[dict[str, Any]] = []
    invalid_exports: list[dict[str, Any]] = []
    draft_exports: list[dict[str, Any]] = []
    for index, item in enumerate(data["comparison_exports"]):
        if not isinstance(item, dict) or not str(item.get("comparison_json", "")).strip():
            invalid_exports.append(
                {
                    "comparison_json": "",
                    "status": "malformed",
                    "review_status": "draft",
                    "schema_version": 0,
                    "contract_errors": [f"comparison_exports[{index}].comparison_json must be a path"],
                    "error": "",
                }
            )
            continue
        comparison_item = inspect_comparison_export(Path(str(item["comparison_json"])))
        inspected.append(comparison_item)
        if comparison_item.get("review_status") == "draft":
            draft_exports.append(comparison_item)
        if not is_deliverable_comparison_export(comparison_item) and comparison_item.get("review_status") != "rejected":
            invalid_exports.append(_comparison_export_problem(comparison_item))

    adopted_comparison_exports = [item for item in inspected if is_adopted_comparison_export(item)]
    ok = (
        len(data["comparison_exports"]) == 0
        or (bool(adopted_comparison_exports) and not draft_exports and not invalid_exports)
    )
    message = (
        "KB comparison export contracts are complete."
        if ok
        else "KB comparison export contracts are missing review adoption or contain invalid exports."
    )
    _add_check(
        checks,
        "kb_comparison_export_contracts",
        ok,
        message,
        {
            "data_json": str(data_json),
            "total_exports": len(data["comparison_exports"]),
            "adopted_comparison_exports": len(adopted_comparison_exports),
            "draft_comparison_exports": len(draft_exports),
            "invalid_paths": [item["comparison_json"] for item in invalid_exports if item.get("comparison_json")],
            "draft_paths": [item["comparison_json"] for item in draft_exports[:6]],
            "invalid_exports": invalid_exports,
        },
    )


def _answer_export_problem(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "query": item.get("query", ""),
        "answer_json": item.get("answer_json", ""),
        "status": item.get("status", ""),
        "schema_version": item.get("schema_version", 0),
        "contract_errors": item.get("contract_errors", []),
        "error": item.get("error", ""),
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


def _verify_digest(checks: list[dict[str, Any]], check_id: str, path: Path, expected: str) -> None:
    if not path.is_file():
        _add_check(checks, check_id, False, "Cannot compute SHA256 because file is missing.", {"path": str(path)})
        return
    actual = file_sha256(path)
    _add_check(
        checks,
        check_id,
        actual == expected,
        "SHA256 matches manifest." if actual == expected else "SHA256 does not match manifest.",
        {"path": str(path), "expected": expected, "actual": actual},
    )


def _verify_sidecar(checks: list[dict[str, Any]], check_id: str, path: Path, expected: str) -> None:
    if not path.is_file():
        _add_check(checks, check_id, False, "Cannot verify SHA256 sidecar because file is missing.", {"path": str(path)})
        return
    parts = path.read_text(encoding="utf-8").split()
    actual = parts[0] if parts else ""
    _add_check(
        checks,
        check_id,
        actual == expected,
        "SHA256 sidecar matches manifest." if actual == expected else "SHA256 sidecar does not match manifest.",
        {"path": str(path), "expected": expected, "actual": actual},
    )


def _verify_cli_sidecar_contract(
    checks: list[dict[str, Any]],
    sidecar_path: Path,
    expected_digest: str,
    package_path: Path,
) -> None:
    _verify_sha256_sidecar_contract(
        checks,
        "cli_package_sidecar_contract",
        sidecar_path,
        expected_digest,
        package_path.name,
        "CLI package",
    )


def _verify_sha256_sidecar_contract(
    checks: list[dict[str, Any]],
    check_id: str,
    sidecar_path: Path,
    expected_digest: str,
    expected_filename: str,
    label: str,
) -> None:
    contract_errors: list[str] = []
    if not sidecar_path.is_file():
        contract_errors.append(f"{label} sidecar file is missing")
    else:
        parts = sidecar_path.read_text(encoding="utf-8").split()
        actual_digest = parts[0] if parts else ""
        actual_filename = parts[1] if len(parts) > 1 else ""
        if actual_digest != expected_digest:
            contract_errors.append(f"{label} sidecar digest must match ship manifest")
        if actual_filename != expected_filename:
            contract_errors.append(f"{label} sidecar filename must be {expected_filename}")
    _add_check(
        checks,
        check_id,
        not contract_errors,
        (
            f"{label} SHA256 sidecar contract is complete."
            if not contract_errors
            else f"{label} SHA256 sidecar contract is incomplete."
        ),
        {"path": str(sidecar_path), "contract_errors": contract_errors},
    )


def _verify_cli_external_manifest_contract(
    checks: list[dict[str, Any]],
    manifest_path: Path,
    cli: dict[str, Any],
    expected_version: str,
) -> None:
    contract_errors: list[str] = []
    payload: dict[str, Any] | None = None
    if not manifest_path.is_file():
        contract_errors.append("cli_package_manifest file is missing")
    else:
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            contract_errors.append(f"cli_package_manifest must be readable JSON: {exc}")
        else:
            if not isinstance(loaded, dict):
                contract_errors.append("cli_package_manifest must be an object")
            else:
                payload = loaded

    if payload is not None:
        if payload.get("ok") is not True:
            contract_errors.append("cli_package_manifest.ok must be true")
        for field, expected in [
            ("package_zip", cli["path"]),
            ("package_zip_sha256", cli["sha256"]),
            ("package_zip_sha256_path", cli["sha256_path"]),
            ("manifest_path", cli["manifest"]),
            ("install_guide", cli["install_guide"]),
        ]:
            if str(payload.get(field, "")) != str(expected):
                contract_errors.append(f"cli_package_manifest.{field} must match ship manifest")
        package = payload.get("package")
        if not isinstance(package, dict):
            contract_errors.append("cli_package_manifest.package must be an object")
        else:
            if package.get("schema_version") != 1:
                contract_errors.append("cli_package_manifest.package.schema_version must be 1")
            product = package.get("product")
            if not isinstance(product, dict):
                contract_errors.append("cli_package_manifest.package.product must be an object")
            else:
                if str(product.get("english_name", "")) != "VoiceVault":
                    contract_errors.append("cli_package_manifest.package.product.english_name must be VoiceVault")
                if str(product.get("version", "")) != expected_version:
                    contract_errors.append(f"cli_package_manifest.package.product.version must be {expected_version}")
            expected_package_name = f"voicevault-cli-v{expected_version}"
            if str(package.get("package_name", "")) != expected_package_name:
                contract_errors.append(f"cli_package_manifest.package.package_name must be {expected_package_name}")
            files = package.get("files")
            generated_files = package.get("generated_files")
            if isinstance(files, list) and isinstance(generated_files, list):
                expected_file_count = len(files) + len(generated_files)
                if payload.get("file_count") != expected_file_count:
                    contract_errors.append(f"cli_package_manifest.file_count must be {expected_file_count}")
            else:
                contract_errors.append("cli_package_manifest.package files and generated_files must be lists")
    _add_check(
        checks,
        "cli_package_manifest_contract",
        not contract_errors,
        (
            "CLI package external manifest contract is complete."
            if not contract_errors
            else "CLI package external manifest contract is incomplete."
        ),
        {"path": str(manifest_path), "contract_errors": contract_errors},
    )


def _verify_cli_install_guide_contract(checks: list[dict[str, Any]], install_path: Path) -> None:
    required_fragments = [
        "# VoiceVault CLI Install Guide",
        "## Requirements",
        "Python 3.11 or newer",
        "## Install",
        "python -m pip install .",
        "python -m voicevault --version",
        "## Verify A Knowledge Base",
        "python -m voicevault release prepare --kb E:\\knowledge-base\\voicevault --json",
        "## Data Boundary",
        "private knowledge-base content",
        "secrets",
        "cookies",
        "audio samples",
        "platform caches",
    ]
    contract_errors: list[str] = []
    if not install_path.is_file():
        contract_errors.append("cli_install_guide file is missing")
    else:
        try:
            text = install_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            contract_errors.append(f"cli_install_guide must be readable text: {exc}")
        else:
            for fragment in required_fragments:
                if fragment not in text:
                    contract_errors.append(f"cli_install_guide missing required fragment: {fragment}")
            for placeholder in ["<repo_root>", "<kb>"]:
                if placeholder in text:
                    contract_errors.append(f"cli_install_guide must not contain {placeholder}")
    _add_check(
        checks,
        "cli_install_guide_contract",
        not contract_errors,
        "CLI install guide contract is complete." if not contract_errors else "CLI install guide contract is incomplete.",
        {"path": str(install_path), "contract_errors": contract_errors},
    )


def _verify_cli_zip_boundary(checks: list[dict[str, Any]], package_path: Path) -> None:
    if not package_path.is_file():
        _add_check(checks, "cli_package_boundary", False, "Cannot inspect CLI package because zip is missing.", {"path": str(package_path)})
        return
    try:
        with ZipFile(package_path) as archive:
            names = archive.namelist()
    except BadZipFile as exc:
        _add_check(checks, "cli_package_boundary", False, "CLI package is not a readable zip.", {"path": str(package_path), "error": str(exc)})
        return
    forbidden = [
        name
        for name in names
        if any(part in name for part in [".voicevault/", ".git/", "__pycache__/", "/dist/"])
        or name.endswith(".pyc")
        or ("prototype/" in name and name.endswith(".zip"))
    ]
    _add_check(
        checks,
        "cli_package_boundary",
        not forbidden,
        "CLI package excludes private state and generated cache paths.",
        {"forbidden_entries": forbidden[:10], "entry_count": len(names)},
    )


def _verify_cli_package_entry_digests(
    checks: list[dict[str, Any]],
    package_path: Path,
    expected_digests: Any,
) -> None:
    contract_errors: list[str] = []
    normalized_expected: dict[str, str] = {}
    if not isinstance(expected_digests, dict) or not expected_digests:
        contract_errors.append("cli_package.package_entry_sha256 must be a nonempty object")
    else:
        for raw_name, raw_digest in expected_digests.items():
            name = str(raw_name)
            digest = str(raw_digest)
            if not name.strip():
                contract_errors.append("cli_package.package_entry_sha256 keys must be nonempty strings")
                continue
            if not digest.strip():
                contract_errors.append(f"cli_package.package_entry_sha256.{name} must be a nonempty digest")
                continue
            normalized_expected[name] = digest
    if not package_path.is_file():
        contract_errors.append("package zip is missing")
        _add_check(
            checks,
            "cli_package_entry_digests_contract",
            False,
            "CLI package entry digest contract is incomplete.",
            {"path": str(package_path), "contract_errors": contract_errors},
        )
        return
    try:
        with ZipFile(package_path) as archive:
            current_names = sorted(name for name in archive.namelist() if not name.endswith("/"))
            expected_names = sorted(normalized_expected)
            missing = [name for name in expected_names if name not in current_names]
            unexpected = [name for name in current_names if name not in normalized_expected]
            duplicate_names: list[str] = []
            seen: set[str] = set()
            for name in current_names:
                if name in seen and name not in duplicate_names:
                    duplicate_names.append(name)
                seen.add(name)
            for name in missing[:10]:
                contract_errors.append(f"{name} is missing from CLI package zip")
            for name in unexpected[:10]:
                contract_errors.append(f"{name} is unexpected in CLI package zip")
            for name in duplicate_names[:10]:
                contract_errors.append(f"{name} must exist exactly once in CLI package zip")
            if len(missing) > 10:
                contract_errors.append(f"{len(missing) - 10} more expected CLI package entries are missing")
            if len(unexpected) > 10:
                contract_errors.append(f"{len(unexpected) - 10} more unexpected CLI package entries are present")
            for name in current_names:
                expected = normalized_expected.get(name)
                if not expected or name in duplicate_names:
                    continue
                actual = hashlib.sha256(archive.read(name)).hexdigest()
                if actual != expected:
                    contract_errors.append(f"{name} digest must match ship manifest")
    except (BadZipFile, OSError) as exc:
        contract_errors.append(f"CLI package zip is not readable: {exc}")
    _add_check(
        checks,
        "cli_package_entry_digests_contract",
        not contract_errors,
        (
            "CLI package entry digest contract is complete."
            if not contract_errors
            else "CLI package entry digest contract is incomplete."
        ),
        {"path": str(package_path), "contract_errors": contract_errors},
    )


def _verify_cli_zip_contract(checks: list[dict[str, Any]], package_path: Path, expected_version: str) -> None:
    if not package_path.is_file():
        _add_cli_contract_unreadable_checks(checks, "CLI package is missing.", {"path": str(package_path)})
        return
    try:
        with ZipFile(package_path) as archive:
            names = archive.namelist()
            required_suffixes = [
                "pyproject.toml",
                "src/voicevault/__main__.py",
                "src/voicevault/cli.py",
            ]
            missing = [suffix for suffix in required_suffixes if not _find_zip_member(names, suffix)]
            _add_check(
                checks,
                "cli_package_entry_points",
                not missing,
                "CLI package contains install metadata and entry-point modules.",
                {"missing": missing},
            )

            init_name = _find_zip_member(names, "src/voicevault/__init__.py")
            if not init_name:
                _add_check(
                    checks,
                    "cli_package_version",
                    False,
                    "CLI package version file is missing.",
                    {"expected": expected_version},
                )
            else:
                init_text = archive.read(init_name).decode("utf-8")
                actual_version = _extract_init_version(init_text)
                _add_check(
                    checks,
                    "cli_package_version",
                    actual_version == expected_version,
                    "CLI package __version__ matches ship manifest.",
                    {"expected": expected_version, "actual": actual_version, "member": init_name},
                )

            manifest_name = _find_zip_member(names, "distribution-manifest.json")
            if not manifest_name:
                _add_check(
                    checks,
                    "cli_package_manifest_version",
                    False,
                    "CLI package distribution manifest is missing.",
                    {"expected": expected_version},
                )
                _add_check(
                    checks,
                    "cli_package_distribution_manifest_contract",
                    False,
                    "CLI package distribution manifest is missing.",
                    {"member": "", "contract_errors": ["distribution-manifest.json is missing"]},
                )
            else:
                package_manifest = json.loads(archive.read(manifest_name).decode("utf-8"))
                actual_manifest_version = str(package_manifest.get("product", {}).get("version", ""))
                _add_check(
                    checks,
                    "cli_package_manifest_version",
                    actual_manifest_version == expected_version,
                    "CLI package distribution manifest version matches ship manifest.",
                    {"expected": expected_version, "actual": actual_manifest_version, "member": manifest_name},
                )
                _verify_distribution_manifest_contract(
                    checks,
                    package_manifest,
                    names,
                    manifest_name,
                    expected_version=expected_version,
                )
    except (BadZipFile, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _add_cli_contract_unreadable_checks(
            checks,
            "Cannot inspect CLI package contract.",
            {"path": str(package_path), "error": str(exc)},
        )


def _add_cli_contract_unreadable_checks(checks: list[dict[str, Any]], message: str, details: dict[str, Any]) -> None:
    _add_check(checks, "cli_package_entry_points", False, message, details)
    _add_check(checks, "cli_package_version", False, message, details)
    _add_check(checks, "cli_package_manifest_version", False, message, details)
    _add_check(checks, "cli_package_distribution_manifest_contract", False, message, {"contract_errors": [message], **details})


def _verify_distribution_manifest_contract(
    checks: list[dict[str, Any]],
    package_manifest: dict[str, Any],
    archive_names: list[str],
    manifest_name: str,
    *,
    expected_version: str,
) -> None:
    contract_errors: list[str] = []
    if package_manifest.get("schema_version") != 1:
        contract_errors.append("distribution_manifest.schema_version must be 1")
    product = package_manifest.get("product")
    if not isinstance(product, dict):
        contract_errors.append("distribution_manifest.product must be an object")
    else:
        if str(product.get("english_name", "")) != "VoiceVault":
            contract_errors.append("distribution_manifest.product.english_name must be VoiceVault")
        if str(product.get("version", "")) != expected_version:
            contract_errors.append(f"distribution_manifest.product.version must be {expected_version}")
    expected_package_name = f"voicevault-cli-v{expected_version}"
    if str(package_manifest.get("package_name", "")) != expected_package_name:
        contract_errors.append(f"distribution_manifest.package_name must be {expected_package_name}")

    entry_points = package_manifest.get("entry_points")
    if not isinstance(entry_points, list):
        contract_errors.append("distribution_manifest.entry_points must be a list")
    else:
        for entry_point in ["python -m voicevault", "voicevault"]:
            if entry_point not in entry_points:
                contract_errors.append(f"distribution_manifest.entry_points must include {entry_point}")

    files = package_manifest.get("files")
    if not isinstance(files, list) or not files:
        contract_errors.append("distribution_manifest.files must be a nonempty list")
        files = []
    elif any(not isinstance(item, str) or not item.strip() for item in files):
        contract_errors.append("distribution_manifest.files must contain nonempty strings")
        files = [str(item) for item in files if isinstance(item, str)]
    else:
        for required_file in [
            "pyproject.toml",
            "README.md",
            "src/voicevault/__init__.py",
        ]:
            if required_file not in files:
                contract_errors.append(f"distribution_manifest.files must include {required_file}")
        expected_release_note = f"docs/release/voicevault-v{expected_version}.md"
        if expected_release_note not in files:
            contract_errors.append(f"distribution_manifest.files must include {expected_release_note}")
        forbidden = [
            item
            for item in files
            if ".voicevault" in item or "__pycache__" in item or "/dist/" in item or item.endswith(".pyc")
        ]
        if forbidden:
            contract_errors.append("distribution_manifest.files must not include private state, dist, or cache paths")
        missing_archive_entries = [
            item
            for item in files
            if not _find_zip_member(archive_names, item)
        ]
        if missing_archive_entries:
            contract_errors.append("distribution_manifest.files must match package zip entries")

    generated_files = package_manifest.get("generated_files")
    if not isinstance(generated_files, list) or not generated_files:
        contract_errors.append("distribution_manifest.generated_files must be a nonempty list")
    else:
        for generated_file in ["INSTALL.md", "distribution-manifest.json"]:
            if generated_file not in generated_files:
                contract_errors.append(f"distribution_manifest.generated_files must include {generated_file}")

    data_boundary = package_manifest.get("data_boundary")
    if not isinstance(data_boundary, list) or not data_boundary:
        contract_errors.append("distribution_manifest.data_boundary must be a nonempty list")
    elif any(not isinstance(item, str) or not item.strip() for item in data_boundary):
        contract_errors.append("distribution_manifest.data_boundary must contain nonempty strings")

    _add_check(
        checks,
        "cli_package_distribution_manifest_contract",
        not contract_errors,
        (
            "CLI package distribution manifest contract is complete."
            if not contract_errors
            else "CLI package distribution manifest contract is incomplete."
        ),
        {"member": manifest_name, "contract_errors": contract_errors},
    )


def _verify_cli_package_import_smoke(checks: list[dict[str, Any]], package_path: Path, expected_version: str) -> None:
    if not package_path.is_file():
        _add_check(checks, "cli_package_import_smoke", False, "Cannot run CLI package smoke because zip is missing.", {"path": str(package_path)})
        return
    try:
        with tempfile.TemporaryDirectory(prefix="voicevault-cli-smoke-") as temp_dir:
            temp_root = Path(temp_dir)
            with ZipFile(package_path) as archive:
                names = archive.namelist()
                src_member = _find_zip_member(names, "src/voicevault/__main__.py")
                if not src_member:
                    _add_check(
                        checks,
                        "cli_package_import_smoke",
                        False,
                        "Cannot run CLI package smoke because __main__.py is missing.",
                        {"path": str(package_path)},
                    )
                    return
                _extract_zip_safely(archive, temp_root)
            src_dir = temp_root / Path(src_member).parent.parent
            env = os.environ.copy()
            env["PYTHONPATH"] = str(src_dir)
            completed = subprocess.run(
                [sys.executable, "-m", "voicevault", "--version"],
                cwd=str(temp_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            stdout = completed.stdout.strip()
            ok = completed.returncode == 0 and stdout == expected_version
            _add_check(
                checks,
                "cli_package_import_smoke",
                ok,
                "CLI package runs `python -m voicevault --version` from extracted zip.",
                {
                    "path": str(package_path),
                    "source_path": str(src_dir),
                    "expected": expected_version,
                    "stdout": stdout,
                    "stderr": completed.stderr.strip()[:1000],
                    "returncode": completed.returncode,
                },
            )
    except (BadZipFile, OSError, subprocess.SubprocessError, UnicodeDecodeError, ValueError) as exc:
        _add_check(
            checks,
            "cli_package_import_smoke",
            False,
            "CLI package runtime smoke failed before version output.",
            {"path": str(package_path), "expected": expected_version, "error": str(exc)},
        )


def _verify_cli_package_install_smoke(checks: list[dict[str, Any]], package_path: Path, expected_version: str) -> None:
    if not package_path.is_file():
        _add_check(
            checks,
            "cli_package_install_smoke",
            False,
            "Cannot run CLI package install smoke because zip is missing.",
            {"path": str(package_path)},
        )
        return
    try:
        with tempfile.TemporaryDirectory(prefix="voicevault-cli-install-") as temp_dir:
            temp_root = Path(temp_dir)
            with ZipFile(package_path) as archive:
                names = archive.namelist()
                pyproject_member = _find_zip_member(names, "pyproject.toml")
                if not pyproject_member:
                    _add_check(
                        checks,
                        "cli_package_install_smoke",
                        False,
                        "Cannot run CLI package install smoke because pyproject.toml is missing.",
                        {"path": str(package_path)},
                    )
                    return
                _extract_zip_safely(archive, temp_root)
            package_root = (temp_root / Path(pyproject_member).parent).resolve()
            install_target = temp_root / "installed"
            install_target.mkdir()
            install_env = os.environ.copy()
            install_env.pop("PYTHONPATH", None)
            install_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    str(package_root),
                    "--target",
                    str(install_target),
                    "--no-build-isolation",
                    "--disable-pip-version-check",
                    "--no-input",
                ],
                cwd=str(temp_root),
                env=install_env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if install_result.returncode != 0:
                if _is_missing_wheel_backend(install_result):
                    fallback = _install_source_layout_fallback(package_root, install_target)
                    if fallback["ok"]:
                        runtime_env = os.environ.copy()
                        runtime_env["PYTHONPATH"] = str(install_target)
                        completed = subprocess.run(
                            [sys.executable, "-m", "voicevault", "--version"],
                            cwd=str(temp_root),
                            env=runtime_env,
                            capture_output=True,
                            text=True,
                            timeout=20,
                            check=False,
                        )
                        stdout = completed.stdout.strip()
                        ok = completed.returncode == 0 and stdout == expected_version
                        _add_check(
                            checks,
                            "cli_package_install_smoke",
                            ok,
                            "CLI package runs from a temporary install target with offline source-layout fallback.",
                            {
                                "path": str(package_path),
                                "package_root": str(package_root),
                                "install_target": str(install_target),
                                "expected": expected_version,
                                "stdout": stdout,
                                "stderr": completed.stderr.strip()[:1000],
                                "returncode": completed.returncode,
                                "install_returncode": install_result.returncode,
                                "fallback": fallback,
                            },
                        )
                        return
                _add_check(
                    checks,
                    "cli_package_install_smoke",
                    False,
                    "CLI package failed pip install smoke.",
                    {
                        "path": str(package_path),
                        "package_root": str(package_root),
                        "install_target": str(install_target),
                        "expected": expected_version,
                        "install_stdout": install_result.stdout.strip()[:1000],
                        "install_stderr": install_result.stderr.strip()[:1000],
                        "install_returncode": install_result.returncode,
                    },
                )
                return
            runtime_env = os.environ.copy()
            runtime_env["PYTHONPATH"] = str(install_target)
            completed = subprocess.run(
                [sys.executable, "-m", "voicevault", "--version"],
                cwd=str(temp_root),
                env=runtime_env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            stdout = completed.stdout.strip()
            ok = completed.returncode == 0 and stdout == expected_version
            _add_check(
                checks,
                "cli_package_install_smoke",
                ok,
                "CLI package installs into a temporary target and runs `python -m voicevault --version`.",
                {
                    "path": str(package_path),
                    "package_root": str(package_root),
                    "install_target": str(install_target),
                    "expected": expected_version,
                    "stdout": stdout,
                    "stderr": completed.stderr.strip()[:1000],
                    "returncode": completed.returncode,
                    "install_returncode": install_result.returncode,
                },
            )
    except (BadZipFile, OSError, subprocess.SubprocessError, UnicodeDecodeError, ValueError) as exc:
        _add_check(
            checks,
            "cli_package_install_smoke",
            False,
            "CLI package install smoke failed before version output.",
            {"path": str(package_path), "expected": expected_version, "error": str(exc)},
        )


def _is_missing_wheel_backend(result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stdout}\n{result.stderr}"
    return "invalid command 'bdist_wheel'" in output


def _install_source_layout_fallback(package_root: Path, install_target: Path) -> dict[str, Any]:
    package_src = package_root / "src" / "voicevault"
    target = install_target / "voicevault"
    if not package_src.is_dir():
        return {
            "ok": False,
            "mode": "source_layout_without_wheel",
            "message": "src/voicevault package directory is missing.",
            "source": str(package_src),
            "target": str(target),
        }
    try:
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(package_src, target)
    except OSError as exc:
        return {
            "ok": False,
            "mode": "source_layout_without_wheel",
            "message": str(exc),
            "source": str(package_src),
            "target": str(target),
        }
    return {
        "ok": True,
        "mode": "source_layout_without_wheel",
        "message": "Copied src/voicevault because local wheel is unavailable.",
        "source": str(package_src),
        "target": str(target),
    }


def _extract_zip_safely(archive: ZipFile, target_dir: Path) -> None:
    root = target_dir.resolve()
    for info in archive.infolist():
        destination = (root / info.filename).resolve()
        if destination != root and root not in destination.parents:
            raise ValueError(f"Unsafe zip member path: {info.filename}")
        if info.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(archive.read(info.filename))


def _find_zip_member(names: list[str], suffix: str) -> str:
    matches = [name for name in names if name.endswith(suffix)]
    return sorted(matches)[0] if matches else ""


def _extract_init_version(text: str) -> str:
    match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", text, flags=re.MULTILINE)
    return match.group(1) if match else ""


def _verify_kb_bundle_entries(checks: list[dict[str, Any]], bundle_zip: Path) -> None:
    if not bundle_zip.is_file():
        _add_check(checks, "kb_release_prepare_entry", False, "Cannot inspect KB release zip because it is missing.", {"path": str(bundle_zip)})
        _add_check(checks, "kb_release_quickstart_entries", False, "Cannot inspect KB release zip because it is missing.", {"path": str(bundle_zip)})
        return
    try:
        with ZipFile(bundle_zip) as archive:
            names = archive.namelist()
    except BadZipFile as exc:
        _add_check(checks, "kb_release_prepare_entry", False, "KB release bundle is not a readable zip.", {"path": str(bundle_zip), "error": str(exc)})
        _add_check(checks, "kb_release_quickstart_entries", False, "KB release bundle is not a readable zip.", {"path": str(bundle_zip), "error": str(exc)})
        return
    _add_check(
        checks,
        "kb_release_prepare_entry",
        names.count("release-prepare.json") == 1,
        "KB release zip contains exactly one release-prepare.json.",
        {"release_prepare_entries": names.count("release-prepare.json")},
    )
    _add_check(
        checks,
        "kb_release_quickstart_entries",
        names.count("quickstart.json") == 1 and names.count("quickstart.md") == 1,
        "KB release zip contains quickstart guide files.",
        {"quickstart_json_entries": names.count("quickstart.json"), "quickstart_markdown_entries": names.count("quickstart.md")},
    )


def _verify_kb_release_entry_digests(
    checks: list[dict[str, Any]],
    bundle_zip: Path,
    expected_digests: Any,
) -> None:
    contract_errors: list[str] = []
    if not isinstance(expected_digests, dict) or not expected_digests:
        contract_errors.append("kb_release.bundle_entry_sha256 must be a nonempty object")
        expected_digests = {}
    if not bundle_zip.is_file():
        contract_errors.append("bundle zip is missing")
        _add_check(
            checks,
            "kb_release_entry_digests_contract",
            False,
            "KB release entry digest contract is incomplete.",
            {"bundle_zip": str(bundle_zip), "contract_errors": contract_errors},
        )
        return
    try:
        with ZipFile(bundle_zip) as archive:
            names = archive.namelist()
            for entry in KB_RELEASE_DIGEST_ENTRIES:
                expected = str(expected_digests.get(entry, ""))
                if not expected:
                    contract_errors.append(f"kb_release.bundle_entry_sha256.{entry} is required")
                    continue
                if names.count(entry) != 1:
                    contract_errors.append(f"{entry} must exist exactly once in KB release zip")
                    continue
                actual = hashlib.sha256(archive.read(entry)).hexdigest()
                if actual != expected:
                    contract_errors.append(f"{entry} digest must match ship manifest")
    except BadZipFile as exc:
        contract_errors.append(f"KB release bundle is not a readable zip: {exc}")
    _add_check(
        checks,
        "kb_release_entry_digests_contract",
        not contract_errors,
        (
            "KB release entry digest contract is complete."
            if not contract_errors
            else "KB release entry digest contract is incomplete."
        ),
        {"bundle_zip": str(bundle_zip), "contract_errors": contract_errors},
    )


def _verify_kb_bundle_contract(
    checks: list[dict[str, Any]],
    bundle_zip: Path,
    *,
    expected_version: str,
    expected_knowledge_base: str,
    expected_repo_root: str,
    expected_readiness_ok: bool,
) -> None:
    required_entries = [
        "readiness.json",
        "manifest.json",
        "release-summary.md",
        "release-plan.md",
        "release-prepare.json",
        "quickstart.json",
        "quickstart.md",
    ]
    missing_entries: list[str] = []
    contract_errors: list[str] = []
    if not bundle_zip.is_file():
        _add_check(
            checks,
            "kb_release_bundle_contract",
            False,
            "Cannot verify KB release bundle contract because zip is missing.",
            {"bundle_zip": str(bundle_zip), "missing_entries": required_entries, "contract_errors": ["bundle zip is missing"]},
        )
        return
    try:
        with ZipFile(bundle_zip) as archive:
            names = archive.namelist()
            missing_entries = [entry for entry in required_entries if names.count(entry) != 1]
            readiness = _read_zip_json_object(archive, "readiness.json", contract_errors)
            release_manifest = _read_zip_json_object(archive, "manifest.json", contract_errors)
            release_summary = _read_zip_text(archive, "release-summary.md", contract_errors)
            release_plan = _read_zip_text(archive, "release-plan.md", contract_errors)
    except BadZipFile as exc:
        _add_check(
            checks,
            "kb_release_bundle_contract",
            False,
            "KB release bundle contract is unreadable.",
            {"bundle_zip": str(bundle_zip), "missing_entries": [], "contract_errors": [str(exc)]},
        )
        return

    if isinstance(readiness, dict):
        if readiness.get("schema_version") != 1:
            contract_errors.append("readiness.schema_version must be 1")
        if not isinstance(readiness.get("ok"), bool):
            contract_errors.append("readiness.ok must be a boolean")
        if bool(readiness.get("ok")) != expected_readiness_ok:
            contract_errors.append("readiness.ok must match ship manifest readiness.ok")
        _verify_kb_readiness_contract(readiness, contract_errors)

    if isinstance(release_manifest, dict):
        if release_manifest.get("schema_version") != 1:
            contract_errors.append("manifest.schema_version must be 1")
        actual_version = str(release_manifest.get("product", {}).get("version", ""))
        if actual_version != expected_version:
            contract_errors.append(f"manifest.product.version must be {expected_version}")
        actual_kb = str(release_manifest.get("knowledge_base", ""))
        if actual_kb != expected_knowledge_base:
            contract_errors.append(f"manifest.knowledge_base must be {expected_knowledge_base}")
        actual_repo_root = str(release_manifest.get("repo_root", ""))
        if actual_repo_root != expected_repo_root:
            contract_errors.append(f"manifest.repo_root must be {expected_repo_root}")
        if actual_repo_root in {"<repo>", "<repo_root>"}:
            contract_errors.append("manifest.repo_root must not be an unresolved release placeholder")
        manifest_readiness = release_manifest.get("readiness")
        if not isinstance(manifest_readiness, dict):
            contract_errors.append("manifest.readiness must be an object")
        elif isinstance(readiness, dict) and bool(manifest_readiness.get("ok")) != bool(readiness.get("ok")):
            contract_errors.append("manifest.readiness.ok must match readiness.json ok")
        _verify_kb_release_manifest_contract(release_manifest, contract_errors)

    _require_text_fragments(
        release_summary,
        "release-summary.md",
        [
            "# 声迹 VoiceVault 发布交付包",
            f"版本：{expected_version}",
            "## 验收摘要",
            "## 检查项",
            "## 关键产物",
            "## 数据边界",
        ],
        contract_errors,
    )
    _require_text_fragments(
        release_plan,
        "release-plan.md",
        [
            "# 发布上线计划",
            f"版本：{expected_version}",
            "## 发布前",
            "## 发布",
            "## 发布后",
            "release check",
            "release package",
            "answers list",
        ],
        contract_errors,
    )

    ok = not missing_entries and not contract_errors
    _add_check(
        checks,
        "kb_release_bundle_contract",
        ok,
        "KB release bundle contract is complete." if ok else "KB release bundle contract is incomplete.",
        {
            "bundle_zip": str(bundle_zip),
            "missing_entries": missing_entries,
            "contract_errors": contract_errors,
        },
    )


def _verify_kb_release_handoff_docs_contract(
    checks: list[dict[str, Any]],
    bundle_zip: Path,
    *,
    expected_version: str,
    expected_knowledge_base: str,
    expected_repo_root: str,
) -> None:
    contract_errors: list[str] = []
    if not bundle_zip.is_file():
        _add_check(
            checks,
            "kb_release_handoff_docs_contract",
            False,
            "Cannot verify KB release handoff docs contract because zip is missing.",
            {"bundle_zip": str(bundle_zip), "contract_errors": ["bundle zip is missing"]},
        )
        return
    try:
        with ZipFile(bundle_zip) as archive:
            release_summary = _read_zip_text(archive, "release-summary.md", contract_errors)
            release_plan = _read_zip_text(archive, "release-plan.md", contract_errors)
    except BadZipFile as exc:
        _add_check(
            checks,
            "kb_release_handoff_docs_contract",
            False,
            "KB release handoff docs contract is unreadable.",
            {"bundle_zip": str(bundle_zip), "contract_errors": [str(exc)]},
        )
        return

    _require_text_fragments(
        release_summary,
        "release-summary.md",
        [
            "# 声迹 VoiceVault 发布交付包",
            f"版本：{expected_version}",
            f"知识库：{expected_knowledge_base}",
            "## 数据边界",
            "真实知识库",
            "密钥",
        ],
        contract_errors,
    )
    _require_text_fragments(
        release_plan,
        "release-plan.md",
        [
            "# 发布上线计划",
            f"版本：{expected_version}",
            f"voicevault release prepare --kb {expected_knowledge_base} --root {expected_repo_root} --json",
            f"voicevault release ship --root {expected_repo_root} --kb {expected_knowledge_base} --json",
            f"voicevault release verify --manifest {expected_repo_root}\\dist\\voicevault-v{expected_version}-ship-manifest.json --json",
            f"dist\\voicevault-cli-v{expected_version}.zip",
            "## 数据边界",
            "不提交真实知识库内容",
            "不复制密钥、cookie、音频样本或平台缓存",
        ],
        contract_errors,
    )
    _reject_unresolved_release_placeholders("release-summary.md", release_summary, contract_errors)
    _reject_unresolved_release_placeholders("release-plan.md", release_plan, contract_errors)
    _add_check(
        checks,
        "kb_release_handoff_docs_contract",
        not contract_errors,
        (
            "KB release handoff docs contract is complete."
            if not contract_errors
            else "KB release handoff docs contract is incomplete."
        ),
        {"bundle_zip": str(bundle_zip), "contract_errors": contract_errors},
    )


def _read_zip_json_object(archive: ZipFile, name: str, errors: list[str]) -> dict[str, Any]:
    try:
        payload = json.loads(archive.read(name).decode("utf-8"))
    except KeyError:
        return {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"{name} must be valid JSON: {exc}")
        return {}
    if not isinstance(payload, dict):
        errors.append(f"{name} must contain a JSON object")
        return {}
    return payload


def _read_zip_text(archive: ZipFile, name: str, errors: list[str]) -> str:
    try:
        return archive.read(name).decode("utf-8")
    except KeyError:
        return ""
    except UnicodeDecodeError as exc:
        errors.append(f"{name} must be UTF-8 text: {exc}")
        return ""


def _verify_kb_readiness_contract(readiness: dict[str, Any], errors: list[str]) -> None:
    summary = readiness.get("summary")
    if not isinstance(summary, dict):
        errors.append("readiness.summary must be an object")
    else:
        for field in [
            "roles",
            "statements",
            "events",
            "reports",
            "reviewed_roles",
            "roles_with_statements",
            "reviewed_roles_with_statements",
            "min_reviewed_roles",
            "role_skills",
            "role_skills_ready",
            "role_skills_missing",
            "role_agent_exports",
            "role_agent_deliverable",
            "role_agent_failed",
            "role_agent_invalid_completed",
            "role_agent_roles_prompt_ready",
            "role_agent_roles_live_ready",
            "role_agent_roles_missing_prompt",
            "role_agent_roles_missing_live",
            "answer_exports",
            "deliverable_answer_exports",
            "answer_regression_questions",
            "answer_regression_min_questions",
            "answer_regression_passed",
            "answer_regression_review",
            "answer_regression_failed",
            "answer_regression_missing_provenance",
            "comparison_exports",
            "adopted_comparison_exports",
            "analysis_exports",
            "analysis_export_ready",
            "analysis_export_malformed",
            "source_configs",
            "source_jobs_pending",
            "capture_pending",
        ]:
            if field not in summary:
                errors.append(f"readiness.summary.{field} is required")
    checks = readiness.get("checks")
    required_checks = [
        "required_dirs",
        "index",
        "roles",
        "profiles_reviewed",
        "role_coverage",
        "role_skills",
        "role_agent_quality",
        "role_agent_readiness",
        "events",
        "sync_status",
        "capture_status",
        "sources",
        "source_adapters",
        "source_runs",
        "source_jobs",
        "analysis_exports",
        "answer_exports",
        "answer_regression",
        "comparison_exports",
        "reports",
        "dashboard",
        "ui",
        "sample_content",
    ]
    if not isinstance(checks, list):
        errors.append("readiness.checks must be a list")
        return
    check_ids = {str(item.get("id", "")) for item in checks if isinstance(item, dict)}
    for check_id in required_checks:
        if check_id not in check_ids:
            errors.append(f"readiness.checks must include {check_id}")
    for index, item in enumerate(checks):
        if not isinstance(item, dict):
            errors.append(f"readiness.checks[{index}] must be an object")
            continue
        if not isinstance(item.get("id"), str) or not item["id"].strip():
            errors.append(f"readiness.checks[{index}].id must be a nonempty string")
        if not isinstance(item.get("ok"), bool):
            errors.append(f"readiness.checks[{index}].ok must be a boolean")
        if not isinstance(item.get("message"), str) or not item["message"].strip():
            errors.append(f"readiness.checks[{index}].message must be a nonempty string")


def _verify_kb_release_manifest_contract(manifest: dict[str, Any], errors: list[str]) -> None:
    product = manifest.get("product")
    if not isinstance(product, dict):
        errors.append("manifest.product must be an object")
    elif str(product.get("english_name", "")) != "VoiceVault":
        errors.append("manifest.product.english_name must be VoiceVault")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        errors.append("manifest.artifacts must be an object")
        return
    for field in [
        "dashboard",
        "ui",
        "ui_data",
        "answer_regression_suite",
        "answer_regression_changelog",
        "source_adapter_validation",
        "source_status",
        "source_import_status",
        "source_jobs",
        "index",
    ]:
        if not isinstance(artifacts.get(field), str) or not artifacts[field].strip():
            errors.append(f"manifest.artifacts.{field} must be a nonempty string")
    for field in [
        "answer_exports",
        "evidence_answer_exports",
        "deliverable_answer_exports",
        "role_skills",
        "comparison_exports",
        "deliverable_comparison_exports",
        "adopted_comparison_exports",
        "analysis_exports",
        "reports",
        "source_configs",
    ]:
        if not isinstance(artifacts.get(field), list):
            errors.append(f"manifest.artifacts.{field} must be a list")
    if not isinstance(artifacts.get("role_agent_readiness"), dict):
        errors.append("manifest.artifacts.role_agent_readiness must be an object")
    status = artifacts.get("analysis_export_status")
    if not isinstance(status, dict):
        errors.append("manifest.artifacts.analysis_export_status must be an object")
    elif not isinstance(status.get("summary"), dict):
        errors.append("manifest.artifacts.analysis_export_status.summary must be an object")


def _require_text_fragments(text: str, name: str, fragments: list[str], errors: list[str]) -> None:
    if not text:
        return
    for fragment in fragments:
        if fragment not in text:
            errors.append(f"{name} must contain {fragment}")


def _reject_unresolved_release_placeholders(name: str, text: str, errors: list[str]) -> None:
    if not text:
        return
    for placeholder in ["<repo>", "<repo_root>", "<version>"]:
        if placeholder in text:
            errors.append(f"{name} must not contain unresolved release placeholder {placeholder}")


def _add_check(
    checks: list[dict[str, Any]],
    check_id: str,
    ok: bool,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    check = {"id": check_id, "ok": bool(ok), "message": message}
    if details:
        check["details"] = details
    checks.append(check)


def _check_ok(checks: list[dict[str, Any]], check_id: str) -> bool:
    return any(check.get("id") == check_id and check.get("ok") for check in checks)
