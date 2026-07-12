from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters import build_record_from_source_item
from .kb import KnowledgeBase
from .sources import get_source, run_source, validate_source_adapters


DEFAULT_FIXTURE_DIR = "inbox/adapter-fixtures"
DEFAULT_EXPORT_DIR = "inbox/exports"
SOURCE_IMPORT_STATUS_FILENAME = "source-import-status.json"
SOURCE_IMPORT_STATUS_LIMIT = 100
SOURCE_TEMPLATE_FIELDS = [
    "source_url",
    "title",
    "text",
    "author",
    "username",
    "published_at",
    "symbols",
    "topics",
    "stance",
    "time_horizon",
    "confidence",
    "notes",
]


def normalize_source_input(
    kb: KnowledgeBase,
    source_id: str,
    input_path: Path,
    *,
    out: Path | None = None,
    dry_run: bool = False,
    update_source: bool = False,
) -> dict[str, Any]:
    source = get_source(kb, source_id)
    input_path = Path(input_path)
    output_path = out or _default_output_path(kb, source_id)
    raw_items = _read_input_records(input_path)
    records = [build_record_from_source_item(source, item) for item in raw_items]
    if not records:
        raise ValueError(f"Source input has no records: {input_path}")
    if not dry_run:
        _write_jsonl(output_path, records)
        if update_source:
            _update_source_for_local_jsonl(source, output_path, kb)
    return {
        "root": str(kb.root),
        "source_id": source_id,
        "input_path": str(input_path),
        "output_path": str(output_path),
        "record_count": len(records),
        "records": records,
        "dry_run": dry_run,
        "updated_source": bool(update_source and not dry_run),
        "adapter_config": {"input_path": _relative_adapter_path(kb, output_path)},
    }


def write_source_input_template(
    kb: KnowledgeBase,
    source_id: str,
    *,
    output_format: str = "csv",
    out: Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    source = get_source(kb, source_id)
    template_format = _normalize_template_format(output_format)
    template_path = out or _default_template_path(kb, source_id, template_format)
    if template_path.exists() and not overwrite:
        raise FileExistsError(f"Source input template already exists: {template_path}")
    template_path.parent.mkdir(parents=True, exist_ok=True)
    record = _source_template_record(source)
    if template_format == "csv":
        _write_template_csv(template_path, record)
    elif template_format == "jsonl":
        _write_jsonl(template_path, [record])
    else:
        template_path.write_text(
            json.dumps({"records": [record]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
    normalize_command = f"voicevault sources normalize --kb {kb.root} --source {source_id} --input {template_path} --update-source --json"
    return {
        "ok": True,
        "root": str(kb.root),
        "source_id": source_id,
        "format": template_format,
        "template_path": str(template_path),
        "fields": SOURCE_TEMPLATE_FIELDS,
        "record": record,
        "next_command": normalize_command,
        "normalize_command": normalize_command,
    }


def import_source_input(
    kb: KnowledgeBase,
    source_id: str,
    input_path: Path,
    *,
    out: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    normalized = normalize_source_input(
        kb,
        source_id,
        input_path,
        out=out,
        dry_run=dry_run,
        update_source=True,
    )
    source_validation = None
    preflight_run = None
    if not dry_run:
        source_validation = validate_source_adapters(kb)
        preflight_run = run_source(kb, source_id, dry_run=True)
    next_commands = _source_import_next_commands(kb, source_id)
    ok = bool(normalized["record_count"] > 0)
    if source_validation is not None:
        ok = bool(ok and source_validation["ok"])
    if preflight_run is not None:
        ok = bool(ok and preflight_run["status"] == "dry_run")
    import_status = None
    if not dry_run:
        _record_source_import(
            kb,
            source_id=source_id,
            input_path=input_path,
            normalized=normalized,
            source_validation=source_validation,
            preflight_run=preflight_run,
            ok=ok,
            next_commands=next_commands,
        )
        import_status = read_source_import_status(kb)
    return {
        "ok": ok,
        "root": str(kb.root),
        "source_id": source_id,
        "input_path": str(input_path),
        "dry_run": dry_run,
        "normalized": normalized,
        "source_validation": source_validation,
        "preflight_run": preflight_run,
        "import_status": import_status,
        "next_commands": next_commands,
    }


def read_source_import_status(kb: KnowledgeBase) -> dict[str, Any]:
    imports, errors = _read_source_imports(kb)
    summary = _summarize_source_imports(imports, errors)
    return {
        "ok": bool(summary["failed"] == 0 and summary["malformed"] == 0),
        "status_path": str(_source_import_status_path(kb)),
        "summary": summary,
        "imports": imports,
        "errors": errors,
    }


def _read_input_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"Source input not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _read_csv_records(path)
    if suffix == ".jsonl":
        return _read_jsonl_records(path)
    if suffix == ".json":
        return _read_json_records(path)
    raise ValueError(f"Unsupported source input format: {path}")


def _read_csv_records(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    return [{key: value for key, value in row.items() if key is not None} for row in rows]


def _read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid source JSONL record at {path}:{line_number}: {exc}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"Source JSONL record must be an object at {path}:{line_number}")
        records.append(item)
    return records


def _read_json_records(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict) and isinstance(payload.get("records"), list):
        records = payload["records"]
    elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
        records = payload["items"]
    elif isinstance(payload, dict):
        records = [payload]
    else:
        raise ValueError(f"Source JSON must be an object, object list, records object, or items object: {path}")
    if not all(isinstance(item, dict) for item in records):
        raise ValueError(f"Source JSON records must contain objects: {path}")
    return records


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def _write_template_csv(path: Path, record: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SOURCE_TEMPLATE_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerow({field: record.get(field, "") for field in SOURCE_TEMPLATE_FIELDS})


def _update_source_for_local_jsonl(source: dict[str, Any], output_path: Path, kb: KnowledgeBase) -> None:
    config_path = Path(str(source["config_path"]))
    payload = dict(source)
    payload["adapter"] = "local-jsonl"
    payload["adapter_config"] = {"input_path": _relative_adapter_path(kb, output_path)}
    payload["updated_at"] = _now_utc()
    payload.pop("config_path", None)
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def _default_output_path(kb: KnowledgeBase, source_id: str) -> Path:
    return kb.root / DEFAULT_FIXTURE_DIR / f"{source_id}.jsonl"


def _default_template_path(kb: KnowledgeBase, source_id: str, output_format: str) -> Path:
    return kb.root / DEFAULT_EXPORT_DIR / f"{source_id}-public-feed.{output_format}"


def _normalize_template_format(output_format: str) -> str:
    value = output_format.strip().lower()
    if value not in {"csv", "jsonl", "json"}:
        raise ValueError("Source input template format must be csv, jsonl, or json.")
    return value


def _source_template_record(source: dict[str, Any]) -> dict[str, str]:
    symbols = ",".join(str(item) for item in source.get("symbols", []) if str(item).strip()) or "NVDA"
    topics = ",".join(str(item) for item in source.get("topics", []) if str(item).strip()) or "ai-infrastructure"
    return {
        "source_url": str(source.get("source_url") or "https://example.com/public/status/1"),
        "title": "Example public statement",
        "text": "Replace this with one public statement from the source.",
        "author": str(source.get("display_name") or source.get("source_id") or "Public Source"),
        "username": str(source.get("source_id") or "public_source"),
        "published_at": "2026-05-31T10:00:00Z",
        "symbols": symbols,
        "topics": topics,
        "stance": "unclear",
        "time_horizon": "unknown",
        "confidence": "low",
        "notes": "Template row. Replace with public-source context before importing.",
    }


def _source_import_next_commands(kb: KnowledgeBase, source_id: str) -> list[str]:
    return [
        f"voicevault sources imports --kb {kb.root} --json",
        f"voicevault sources validate --kb {kb.root} --json",
        f"voicevault sources enqueue --kb {kb.root} --source {source_id} --json",
        f"voicevault sources drain --kb {kb.root} --dry-run --json",
        f"voicevault sync --kb {kb.root} --archive --json",
    ]


def _relative_adapter_path(kb: KnowledgeBase, path: Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(kb.root).as_posix()
    except ValueError:
        return str(resolved)


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _source_import_status_path(kb: KnowledgeBase) -> Path:
    return kb.state_dir / SOURCE_IMPORT_STATUS_FILENAME


def _record_source_import(
    kb: KnowledgeBase,
    *,
    source_id: str,
    input_path: Path,
    normalized: dict[str, Any],
    source_validation: dict[str, Any] | None,
    preflight_run: dict[str, Any] | None,
    ok: bool,
    next_commands: list[str],
) -> dict[str, Any]:
    imported_at = _now_utc()
    error = ""
    if source_validation is not None and not source_validation.get("ok", False):
        error = "Source adapter validation failed."
    if preflight_run is not None and preflight_run.get("status") != "dry_run":
        error = "Source import preflight did not complete as a dry run."
    record = {
        "import_id": f"{source_id}:{imported_at}",
        "source_id": source_id,
        "status": "ready" if ok else "failed",
        "input_path": str(input_path),
        "output_path": str(normalized.get("output_path") or ""),
        "record_count": int(normalized.get("record_count") or 0),
        "dry_run": bool(normalized.get("dry_run", False)),
        "updated_source": bool(normalized.get("updated_source", False)),
        "source_validation_ok": bool((source_validation or {}).get("ok", False)),
        "preflight_status": str((preflight_run or {}).get("status") or ""),
        "preflight_run_id": str(((preflight_run or {}).get("run") or {}).get("run_id") or ""),
        "imported_at": imported_at,
        "error": error,
        "next_commands": next_commands,
    }
    existing, _ = _read_source_imports(kb)
    imports = [record, *existing][:SOURCE_IMPORT_STATUS_LIMIT]
    path = _source_import_status_path(kb)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": imported_at,
                "imports": imports,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return record


def _read_source_imports(kb: KnowledgeBase) -> tuple[list[dict[str, Any]], list[str]]:
    path = _source_import_status_path(kb)
    if not path.is_file():
        return [], []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [], [f"Invalid source import status JSON: {exc}"]
    if not isinstance(payload, dict):
        return [], [f"Source import status must be a JSON object: {path}"]
    raw_imports = payload.get("imports", [])
    if not isinstance(raw_imports, list):
        return [], [f"Source import status imports must be a list: {path}"]
    imports: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, raw_import in enumerate(raw_imports):
        if not isinstance(raw_import, dict):
            errors.append(f"Source import status row {index} must be a JSON object: {path}")
            continue
        imports.append(_normalize_source_import(raw_import))
    return imports[:SOURCE_IMPORT_STATUS_LIMIT], errors


def _normalize_source_import(payload: dict[str, Any]) -> dict[str, Any]:
    status = str(payload.get("status") or "")
    if status not in {"ready", "failed"}:
        status = "failed"
    return {
        "import_id": str(payload.get("import_id") or ""),
        "source_id": str(payload.get("source_id") or ""),
        "status": status,
        "input_path": str(payload.get("input_path") or ""),
        "output_path": str(payload.get("output_path") or ""),
        "record_count": int(payload.get("record_count") or 0),
        "dry_run": bool(payload.get("dry_run", False)),
        "updated_source": bool(payload.get("updated_source", False)),
        "source_validation_ok": bool(payload.get("source_validation_ok", False)),
        "preflight_status": str(payload.get("preflight_status") or ""),
        "preflight_run_id": str(payload.get("preflight_run_id") or ""),
        "imported_at": str(payload.get("imported_at") or ""),
        "error": str(payload.get("error") or ""),
        "next_commands": [str(item) for item in payload.get("next_commands", []) if str(item).strip()]
        if isinstance(payload.get("next_commands", []), list)
        else [],
    }


def _summarize_source_imports(imports: list[dict[str, Any]], errors: list[str]) -> dict[str, int]:
    return {
        "total": len(imports),
        "ready": sum(1 for item in imports if item["status"] == "ready"),
        "failed": sum(1 for item in imports if item["status"] == "failed"),
        "records": sum(int(item.get("record_count") or 0) for item in imports),
        "malformed": len(errors),
    }
