from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from .capture import append_capture_record, build_capture_record
from .kb import KnowledgeBase
from .sources import create_source
from .sync import sync_once


ACCOUNT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
ACCOUNT_STATUS_FILENAME = "account-status.json"
ACCOUNT_STATUS_LIMIT = 100
ACCOUNT_COLLECTION_MODES = {"auto", "rss", "local-export", "custom-api", "blocked"}
SUPPORTED_PLATFORMS = {"weibo", "wechat", "xueqiu", "rss", "local-export", "custom-api"}
RESTRICTED_PLATFORMS = {"weibo", "wechat", "xueqiu"}
FetchPayload = Callable[[dict[str, Any]], Any]


def create_account(
    kb: KnowledgeBase,
    *,
    account_id: str,
    platform: str,
    platform_account_id: str,
    role_id: str,
    display_name: str = "",
    source_id: str = "",
    source_url: str = "",
    collection_mode: str = "auto",
    feed_url: str = "",
    input_path: str = "",
    api_url: str = "",
    adapter_config: dict[str, Any] | None = None,
    symbols: list[str] | None = None,
    topics: list[str] | None = None,
    tags: list[str] | None = None,
    notes: str = "",
    enabled: bool = True,
    overwrite: bool = False,
) -> dict[str, Any]:
    normalized_account_id = _validate_account_id(account_id)
    normalized_platform = _normalize_platform(platform)
    if not platform_account_id.strip():
        raise ValueError("Platform account ID is required.")
    if not role_id.strip():
        raise ValueError("Role ID is required.")
    config = dict(adapter_config or {})
    _merge_config(config, "feed_url", feed_url)
    _merge_config(config, "input_path", input_path)
    _merge_config(config, "api_url", api_url)
    mode = _resolve_collection_mode(normalized_platform, collection_mode, config)
    normalized_source_id = source_id.strip() or _default_source_id(normalized_account_id)
    path = _account_path(kb, normalized_account_id)
    if path.exists() and not overwrite:
        raise FileExistsError(f"Account config already exists: {path}")
    kb.content_dir.mkdir(parents=True, exist_ok=True)
    _accounts_dir(kb).mkdir(parents=True, exist_ok=True)
    now = _now_utc()
    payload = {
        "schema_version": 1,
        "account_id": normalized_account_id,
        "platform": normalized_platform,
        "platform_account_id": platform_account_id.strip(),
        "role_id": role_id.strip(),
        "source_id": normalized_source_id,
        "display_name": display_name.strip() or platform_account_id.strip(),
        "source_url": source_url.strip(),
        "collection_mode": mode,
        "adapter_config": config,
        "enabled": enabled,
        "status": "active" if enabled else "disabled",
        "symbols": symbols or [],
        "topics": topics or [],
        "tags": tags or [],
        "notes": notes.strip(),
        "cursor": {},
        "created_at": now,
        "updated_at": now,
        "config_path": str(path),
    }
    path.write_text(json.dumps(_persisted_account(payload), ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    create_source(
        kb,
        source_id=normalized_source_id,
        role_id=role_id.strip(),
        platform=normalized_platform,
        source_url=source_url.strip() or str(config.get("feed_url") or config.get("api_url") or ""),
        display_name=payload["display_name"],
        adapter="manual",
        adapter_config={},
        symbols=symbols,
        topics=topics,
        tags=tags,
        notes=notes,
        enabled=enabled,
        overwrite=overwrite,
    )
    return payload


def list_accounts(kb: KnowledgeBase) -> list[dict[str, Any]]:
    if not _accounts_dir(kb).exists():
        return []
    accounts: list[dict[str, Any]] = []
    for path in sorted(_accounts_dir(kb).glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            accounts.append(_malformed_account(path, "Invalid account JSON."))
            continue
        if not isinstance(payload, dict):
            accounts.append(_malformed_account(path, "Account JSON must contain an object."))
            continue
        payload["config_path"] = str(path)
        payload.setdefault("status", "active" if payload.get("enabled", True) else "disabled")
        payload.setdefault("adapter_config", {})
        payload.setdefault("cursor", {})
        payload.setdefault("symbols", [])
        payload.setdefault("topics", [])
        payload.setdefault("tags", [])
        accounts.append(payload)
    return sorted(accounts, key=lambda item: str(item.get("account_id") or ""))


def get_account(kb: KnowledgeBase, account_id: str) -> dict[str, Any]:
    normalized_account_id = _validate_account_id(account_id)
    path = _account_path(kb, normalized_account_id)
    if not path.is_file():
        raise FileNotFoundError(f"Account config not found: {normalized_account_id}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Account config must contain an object: {path}")
    payload["config_path"] = str(path)
    payload.setdefault("adapter_config", {})
    payload.setdefault("cursor", {})
    return payload


def read_account_status(kb: KnowledgeBase) -> dict[str, Any]:
    accounts = list_accounts(kb)
    runs, errors = _read_account_runs(kb)
    latest_by_account = _latest_runs_by_account(runs)
    rows = [
        {
            "account_id": str(account.get("account_id") or ""),
            "platform": str(account.get("platform") or ""),
            "platform_account_id": str(account.get("platform_account_id") or ""),
            "role_id": str(account.get("role_id") or ""),
            "source_id": str(account.get("source_id") or ""),
            "status": str(account.get("status") or ""),
            "collection_mode": str(account.get("collection_mode") or ""),
            "cursor": account.get("cursor") if isinstance(account.get("cursor"), dict) else {},
            "latest_run": latest_by_account.get(str(account.get("account_id") or "")),
            "config_path": str(account.get("config_path") or ""),
        }
        for account in accounts
    ]
    summary = {
        "total": len(accounts),
        "active": len([item for item in accounts if item.get("status") == "active"]),
        "disabled": len([item for item in accounts if item.get("status") == "disabled"]),
        "blocked": len([item for item in accounts if item.get("collection_mode") == "blocked"]),
        "malformed": len([item for item in accounts if item.get("status") == "malformed"]),
        "runs": len(runs),
        "failed_runs": len([item for item in runs if not item.get("ok", False)]),
    }
    return {
        "ok": bool(summary["malformed"] == 0 and not errors),
        "status_path": str(_account_status_path(kb)),
        "summary": summary,
        "accounts": rows,
        "runs": runs,
        "errors": errors,
    }


def collect_account(
    kb: KnowledgeBase,
    account_id: str,
    *,
    dry_run: bool = False,
    sync: bool = False,
    archive: bool = False,
    fetcher: FetchPayload | None = None,
) -> dict[str, Any]:
    account = get_account(kb, account_id)
    account["kb_root"] = str(kb.root)
    capture_path = kb.inbox_captures_dir / f"account-{account['account_id']}.jsonl"
    if account.get("status") != "active":
        result = _collection_failure(account, capture_path, "Account is not active.", "disabled", dry_run=dry_run)
        _record_account_run(kb, result)
        return result
    mode = str(account.get("collection_mode") or "blocked")
    try:
        if mode == "blocked":
            raise _BlockedCollectionError(_blocked_message(account))
        records = _collect_records(account, fetcher=fetcher)
        existing_ids = _existing_statement_ids(capture_path)
        written = 0
        duplicates = 0
        for record in records:
            statement_id = str(record.get("statement_id") or "")
            if statement_id in existing_ids:
                duplicates += 1
                continue
            if not dry_run:
                append_capture_record(capture_path, record)
                existing_ids.add(statement_id)
            written += 0 if dry_run else 1
        cursor = _cursor_for_records(records)
        cursor["record_count"] = len(records)
        cursor["written"] = written
        cursor["duplicates_skipped"] = duplicates
        if not dry_run:
            _update_account_cursor(kb, account, cursor)
        sync_payload = None
        if sync and not dry_run:
            sync_payload = _sync_payload(kb, archive=archive)
        result = {
            "ok": True,
            "status": "dry_run" if dry_run else "collected",
            "account_id": str(account["account_id"]),
            "platform": str(account["platform"]),
            "platform_account_id": str(account["platform_account_id"]),
            "role_id": str(account["role_id"]),
            "source_id": str(account["source_id"]),
            "collection_mode": mode,
            "dry_run": dry_run,
            "record_count": len(records),
            "written": written,
            "duplicates_skipped": duplicates,
            "capture_path": str(capture_path),
            "cursor": cursor,
            "sync": sync_payload,
            "error": "",
        }
    except _BlockedCollectionError as exc:
        result = _collection_failure(account, capture_path, str(exc), "blocked", dry_run=dry_run)
    except Exception as exc:
        result = _collection_failure(account, capture_path, str(exc), "failed", dry_run=dry_run)
    _record_account_run(kb, result)
    return result


def _collect_records(account: dict[str, Any], *, fetcher: FetchPayload | None) -> list[dict[str, Any]]:
    mode = str(account.get("collection_mode") or "blocked")
    if mode == "rss":
        raw_items = _read_rss_items(account)
    elif mode == "local-export":
        raw_items = _read_local_export_items(account)
    elif mode == "custom-api":
        raw_items = _read_custom_api_items(account, fetcher=fetcher)
    else:
        raise ValueError(f"Unsupported account collection mode: {mode}")
    records = [normalize_account_item(account, item) for item in raw_items]
    return [record for record in records if str(record.get("text") or "").strip()]


def normalize_account_item(account: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    text = _first_text(item, "text", "body", "content", "full_text", "description", "summary")
    source_url = _first_text(item, "source_url", "url", "permalink", "link")
    published_at = _first_text(item, "published_at", "created_at", "posted_at", "pubDate", "updated")
    title = _first_text(item, "title") or _title_from_text(text)
    statement_id = _safe_statement_id(
        _first_text(item, "statement_id", "id", "post_id", "external_id", "guid")
        or _hash_id(account, source_url, published_at, text)
    )
    return build_capture_record(
        role_id=str(account["role_id"]),
        platform=str(account["platform"]),
        text=text,
        url=source_url or str(account.get("source_url") or ""),
        title=title,
        author=_first_text(item, "author", "source_author", "display_name", "name") or str(account.get("display_name") or ""),
        platform_user_id=str(account.get("platform_account_id") or ""),
        published_at=published_at,
        symbols=_list_value(item.get("symbols")) or list(account.get("symbols") or []),
        topics=_list_value(item.get("topics")) or list(account.get("topics") or []),
        statement_id=statement_id,
        stance=_first_text(item, "stance") or "unclear",
        time_horizon=_first_text(item, "time_horizon") or "unknown",
        confidence=_first_text(item, "confidence") or "low",
        notes=_first_text(item, "notes") or str(account.get("notes") or ""),
    )


def _read_rss_items(account: dict[str, Any]) -> list[dict[str, Any]]:
    feed_url = str((account.get("adapter_config") or {}).get("feed_url") or "")
    if not feed_url.strip():
        raise ValueError("RSS account requires adapter_config.feed_url.")
    text = _read_text_location(feed_url)
    root = ElementTree.fromstring(text)
    if _strip_namespace(root.tag) == "rss":
        return _rss_channel_items(root)
    if _strip_namespace(root.tag) == "feed":
        return _atom_feed_items(root)
    raise ValueError(f"Unsupported RSS/Atom document root: {_strip_namespace(root.tag)}")


def _rss_channel_items(root: ElementTree.Element) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        items.append(
            {
                "id": _child_text(item, "guid") or _child_text(item, "link"),
                "title": _child_text(item, "title"),
                "link": _child_text(item, "link"),
                "published_at": _child_text(item, "pubDate"),
                "description": _clean_text(_child_text(item, "description") or _namespaced_child_text(item, "encoded")),
                "author": _child_text(item, "author"),
            }
        )
    return items


def _atom_feed_items(root: ElementTree.Element) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for entry in [child for child in root if _strip_namespace(child.tag) == "entry"]:
        link = ""
        for child in entry:
            if _strip_namespace(child.tag) == "link":
                link = str(child.attrib.get("href") or "")
                break
        items.append(
            {
                "id": _child_text(entry, "id") or link,
                "title": _child_text(entry, "title"),
                "link": link,
                "published_at": _child_text(entry, "published") or _child_text(entry, "updated"),
                "description": _clean_text(_child_text(entry, "summary") or _child_text(entry, "content")),
                "author": _atom_author(entry),
            }
        )
    return items


def _read_local_export_items(account: dict[str, Any]) -> list[dict[str, Any]]:
    input_path = str((account.get("adapter_config") or {}).get("input_path") or "")
    if not input_path.strip():
        raise ValueError("Local export account requires adapter_config.input_path.")
    path = Path(input_path).expanduser()
    if not path.is_absolute():
        path = Path(str(account.get("kb_root") or ".")) / path
    return _read_input_records(path.resolve())


def _read_custom_api_items(account: dict[str, Any], *, fetcher: FetchPayload | None) -> list[dict[str, Any]]:
    if fetcher is not None:
        payload = fetcher(account)
    else:
        payload = _fetch_custom_api_payload(account)
    return _payload_records(payload)


def _fetch_custom_api_payload(account: dict[str, Any]) -> Any:
    config = account.get("adapter_config") or {}
    api_url = str(config.get("api_url") or "")
    if not api_url:
        raise ValueError("Custom API account requires adapter_config.api_url.")
    headers = {"Accept": "application/json"}
    configured_headers = config.get("headers")
    if isinstance(configured_headers, dict):
        headers.update({str(key): str(value) for key, value in configured_headers.items()})
    token_env = str(config.get("token_env") or "").strip()
    if token_env:
        import os

        token = os.environ.get(token_env, "").strip()
        if token:
            headers.setdefault("Authorization", f"Bearer {token}")
    request = Request(api_url, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        excerpt = exc.read(512).decode("utf-8", errors="replace")
        raise ValueError(f"Custom API request failed with HTTP {exc.code}: {excerpt}") from exc
    return json.loads(body)


def _payload_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
        records = payload["items"]
    elif isinstance(payload, dict) and isinstance(payload.get("records"), list):
        records = payload["records"]
    elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
        records = payload["data"]
    elif isinstance(payload, dict):
        records = [payload]
    else:
        raise ValueError("Custom API payload must be an object, object list, or object with items/records/data.")
    if not all(isinstance(item, dict) for item in records):
        raise ValueError("Account collection records must be JSON objects.")
    return records


def _read_input_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"Local export not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        return _read_jsonl_records(path)
    if suffix == ".json":
        return _payload_records(json.loads(path.read_text(encoding="utf-8")))
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if suffix in {".html", ".htm", ".txt"}:
        text = _clean_text(path.read_text(encoding="utf-8"))
        return [{"text": text, "title": path.stem, "id": path.stem}]
    raise ValueError(f"Unsupported local export format: {path}")


def _read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid local export JSONL record at {path}:{line_number}: {exc}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"Local export JSONL record must be an object at {path}:{line_number}")
        records.append(item)
    return records


def _read_text_location(value: str) -> str:
    path = Path(value).expanduser()
    if path.is_file():
        return path.read_text(encoding="utf-8")
    parsed = urlparse(value)
    if parsed.scheme == "file":
        return Path(parsed.path).read_text(encoding="utf-8")
    if parsed.scheme in {"http", "https"}:
        request = Request(value, headers={"Accept": "application/rss+xml, application/atom+xml, text/xml, application/xml"})
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    raise FileNotFoundError(f"Feed location not found: {value}")


def _existing_statement_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("statement_id"):
            ids.add(str(payload["statement_id"]))
    return ids


def _cursor_for_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {"last_seen_id": "", "last_collected_at": _now_utc()}
    last = records[-1]
    return {
        "last_seen_id": str(last.get("statement_id") or ""),
        "last_collected_at": _now_utc(),
    }


def _update_account_cursor(kb: KnowledgeBase, account: dict[str, Any], cursor: dict[str, Any]) -> None:
    payload = dict(account)
    payload["cursor"] = cursor
    payload["updated_at"] = _now_utc()
    path = _account_path(kb, str(account["account_id"]))
    path.write_text(json.dumps(_persisted_account(payload), ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def _sync_payload(kb: KnowledgeBase, *, archive: bool) -> dict[str, Any]:
    result = sync_once(kb, archive_processed=archive)
    return {
        "captures_seen": result.captures_seen,
        "notes_written": result.notes_written,
        "duplicates_skipped": result.duplicates_skipped,
        "statements_indexed": result.statements_indexed,
        "errors": result.errors,
        "archived_files": result.archived_files,
    }


def _record_account_run(kb: KnowledgeBase, result: dict[str, Any]) -> dict[str, Any]:
    run = {
        "run_id": f"{result.get('account_id', 'account')}:{_now_utc()}",
        "account_id": str(result.get("account_id") or ""),
        "platform": str(result.get("platform") or ""),
        "collection_mode": str(result.get("collection_mode") or ""),
        "status": str(result.get("status") or ""),
        "ok": bool(result.get("ok", False)),
        "record_count": int(result.get("record_count") or 0),
        "written": int(result.get("written") or 0),
        "duplicates_skipped": int(result.get("duplicates_skipped") or 0),
        "capture_path": str(result.get("capture_path") or ""),
        "ran_at": _now_utc(),
        "error": str(result.get("error") or ""),
    }
    existing, _ = _read_account_runs(kb)
    runs = [run, *existing][:ACCOUNT_STATUS_LIMIT]
    path = _account_status_path(kb)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "updated_at": _now_utc(), "runs": runs}, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return run


def _read_account_runs(kb: KnowledgeBase) -> tuple[list[dict[str, Any]], list[str]]:
    path = _account_status_path(kb)
    if not path.is_file():
        return [], []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [], [f"{path}: invalid account status JSON: {exc}"]
    runs = payload.get("runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        return [], [f"{path}: account status must contain runs list"]
    return [run for run in runs if isinstance(run, dict)], []


def _latest_runs_by_account(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        account_id = str(run.get("account_id") or "")
        if account_id and account_id not in latest:
            latest[account_id] = run
    return latest


def _collection_failure(account: dict[str, Any], capture_path: Path, error: str, status: str, *, dry_run: bool) -> dict[str, Any]:
    return {
        "ok": False,
        "status": status,
        "account_id": str(account.get("account_id") or ""),
        "platform": str(account.get("platform") or ""),
        "platform_account_id": str(account.get("platform_account_id") or ""),
        "role_id": str(account.get("role_id") or ""),
        "source_id": str(account.get("source_id") or ""),
        "collection_mode": str(account.get("collection_mode") or ""),
        "dry_run": dry_run,
        "record_count": 0,
        "written": 0,
        "duplicates_skipped": 0,
        "capture_path": str(capture_path),
        "cursor": account.get("cursor") if isinstance(account.get("cursor"), dict) else {},
        "sync": None,
        "error": error,
    }


def _resolve_collection_mode(platform: str, mode: str, config: dict[str, Any]) -> str:
    normalized_mode = mode.strip().lower() or "auto"
    if normalized_mode not in ACCOUNT_COLLECTION_MODES:
        raise ValueError(f"Unsupported account collection mode: {mode}")
    if normalized_mode != "auto":
        return normalized_mode
    if str(config.get("feed_url") or "").strip():
        return "rss"
    if str(config.get("input_path") or "").strip():
        return "local-export"
    if str(config.get("api_url") or "").strip():
        return "custom-api"
    if platform in RESTRICTED_PLATFORMS:
        return "blocked"
    if platform == "rss":
        return "blocked"
    if platform == "local-export":
        return "blocked"
    if platform == "custom-api":
        return "blocked"
    return "blocked"


def _blocked_message(account: dict[str, Any]) -> str:
    return (
        f"Account collection is blocked for platform '{account.get('platform')}'. "
        "Provide a legal feed_url, input_path, or authorized api_url and recreate the account with mode auto/rss/local-export/custom-api."
    )


def _merge_config(config: dict[str, Any], key: str, value: str) -> None:
    if value.strip():
        config[key] = value.strip()


def _normalize_platform(platform: str) -> str:
    value = platform.strip().lower()
    if value not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported account platform: {platform}")
    return value


def _validate_account_id(account_id: str) -> str:
    value = account_id.strip()
    if not ACCOUNT_ID_PATTERN.match(value):
        raise ValueError(f"Invalid account ID: {account_id}")
    return value


def _default_source_id(account_id: str) -> str:
    value = f"account-{account_id}"
    if len(value) <= 80:
        return value
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    return f"{value[:71]}-{digest}"


def _account_path(kb: KnowledgeBase, account_id: str) -> Path:
    return _accounts_dir(kb) / f"{account_id}.json"


def _accounts_dir(kb: KnowledgeBase) -> Path:
    return kb.content_dir / "accounts"


def _account_status_path(kb: KnowledgeBase) -> Path:
    return kb.state_dir / ACCOUNT_STATUS_FILENAME


def _persisted_account(account: dict[str, Any]) -> dict[str, Any]:
    payload = dict(account)
    payload.pop("config_path", None)
    payload.pop("kb_root", None)
    return payload


def _malformed_account(path: Path, error: str) -> dict[str, Any]:
    return {
        "account_id": path.stem,
        "platform": "",
        "platform_account_id": "",
        "role_id": "",
        "source_id": "",
        "display_name": path.stem,
        "collection_mode": "",
        "adapter_config": {},
        "status": "malformed",
        "cursor": {},
        "config_path": str(path),
        "error": error,
    }


def _child_text(parent: ElementTree.Element, local_name: str) -> str:
    for child in parent:
        if _strip_namespace(child.tag) == local_name:
            return _clean_text(child.text or "")
    return ""


def _namespaced_child_text(parent: ElementTree.Element, local_name: str) -> str:
    for child in parent:
        if _strip_namespace(child.tag) == local_name:
            return _clean_text(child.text or "")
    return ""


def _atom_author(entry: ElementTree.Element) -> str:
    for child in entry:
        if _strip_namespace(child.tag) == "author":
            return _child_text(child, "name")
    return ""


def _strip_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _clean_text(value: str) -> str:
    text = unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = _clean_text(str(value))
        if text:
            return text
    return ""


def _title_from_text(text: str) -> str:
    value = " ".join(text.strip().split())
    return value[:60] or "Public statement"


def _hash_id(account: dict[str, Any], source_url: str, published_at: str, text: str) -> str:
    digest = hashlib.sha1(
        "|".join(
            [
                str(account.get("platform") or ""),
                str(account.get("platform_account_id") or ""),
                source_url,
                published_at,
                text,
            ]
        ).encode("utf-8")
    ).hexdigest()[:12]
    return f"stmt_{digest}"


def _safe_statement_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")
    return normalized[:120] or "statement"


def _list_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).replace(";", ",").split(",") if part.strip()]


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class _BlockedCollectionError(ValueError):
    pass
