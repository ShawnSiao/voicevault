from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .checksums import file_sha256


def inspect_release_handoff(manifest_path: Path) -> dict[str, Any]:
    resolved_manifest = manifest_path.resolve()
    errors: list[str] = []
    manifest = _read_json_object(resolved_manifest, "ship manifest", errors)
    product = manifest.get("product", {}) if manifest else {}
    artifacts = manifest.get("artifacts", {}) if manifest else {}
    artifact_index_path = _artifact_index_path(artifacts, errors)
    artifact_index = _read_json_object(artifact_index_path, "release artifact index", errors) if artifact_index_path else {}
    commands = artifact_index.get("commands", {}) if artifact_index else {}
    if artifact_index and not isinstance(commands, dict):
        errors.append("release artifact index commands must be an object")
        commands = {}

    inspected_artifacts = _inspect_artifacts(artifact_index.get("artifacts", []) if artifact_index else [], errors)
    missing_required_ids = [
        str(artifact["id"])
        for artifact in inspected_artifacts
        if artifact["required"] and not artifact["exists"]
    ]
    sha256_mismatched_ids = [
        str(artifact["id"])
        for artifact in inspected_artifacts
        if artifact["sha256_ok"] is False
    ]
    summary = {
        "total": len(inspected_artifacts),
        "existing": sum(1 for artifact in inspected_artifacts if artifact["exists"]),
        "missing_required": len(missing_required_ids),
        "sha256_checked": sum(1 for artifact in inspected_artifacts if artifact["sha256_checked"]),
        "sha256_mismatched": len(sha256_mismatched_ids),
        "missing_required_ids": missing_required_ids,
        "sha256_mismatched_ids": sha256_mismatched_ids,
    }
    return {
        "schema_version": 1,
        "ok": not errors and summary["missing_required"] == 0 and summary["sha256_mismatched"] == 0,
        "manifest_path": str(resolved_manifest),
        "artifact_index_path": str(artifact_index_path) if artifact_index_path else "",
        "product": product if isinstance(product, dict) else {},
        "summary": summary,
        "commands": commands,
        "artifacts": inspected_artifacts,
        "errors": errors,
    }


def _read_json_object(path: Path, label: str, errors: list[str]) -> dict[str, Any]:
    if not path.is_file():
        errors.append(f"{label} is missing: {path}")
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{label} is unreadable: {exc}")
        return {}
    if not isinstance(loaded, dict):
        errors.append(f"{label} must contain a JSON object")
        return {}
    return loaded


def _artifact_index_path(artifacts: Any, errors: list[str]) -> Path | None:
    if not isinstance(artifacts, dict):
        errors.append("ship manifest artifacts must be an object")
        return None
    value = artifacts.get("release_artifact_index")
    if not isinstance(value, str) or not value.strip():
        errors.append("ship manifest artifacts.release_artifact_index must be a nonempty string")
        return None
    return Path(value)


def _inspect_artifacts(entries: Any, errors: list[str]) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        errors.append("release artifact index artifacts must be a list")
        return []

    inspected: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"release artifact index artifacts[{index}] must be an object")
            continue
        inspected.append(_inspect_artifact(index, entry, errors))
    return inspected


def _inspect_artifact(index: int, entry: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    artifact_id = str(entry.get("id") or f"artifact_{index}")
    path_text = str(entry.get("path") or "")
    artifact_path = Path(path_text) if path_text else Path()
    required_value = entry.get("required", False)
    required = required_value if isinstance(required_value, bool) else False
    if not isinstance(required_value, bool):
        errors.append(f"release artifact index artifacts.{artifact_id}.required must be a boolean")

    exists = bool(path_text and artifact_path.is_file())
    expected_sha256 = entry.get("sha256", "")
    if not isinstance(expected_sha256, str):
        errors.append(f"release artifact index artifacts.{artifact_id}.sha256 must be a string")
        expected_sha256 = ""
    actual_sha256 = ""
    sha256_checked = False
    sha256_ok: bool | None = None
    if expected_sha256 and exists:
        sha256_checked = True
        try:
            actual_sha256 = file_sha256(artifact_path)
        except OSError as exc:
            errors.append(f"release artifact index artifacts.{artifact_id} is unreadable for sha256: {exc}")
            sha256_ok = False
        else:
            sha256_ok = actual_sha256 == expected_sha256

    return {
        "id": artifact_id,
        "kind": str(entry.get("kind") or ""),
        "phase": str(entry.get("phase") or ""),
        "path": path_text,
        "required": required,
        "exists": exists,
        "expected_sha256": expected_sha256,
        "actual_sha256": actual_sha256,
        "sha256_checked": sha256_checked,
        "sha256_ok": sha256_ok,
        "description": str(entry.get("description") or ""),
    }
