from __future__ import annotations

import ipaddress
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .app_db import resolve_data_dir


RUNTIME_FILENAME = "runtime.json"
RUNTIME_SCHEMA_VERSION = 1
_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


class RuntimeRegistryError(Exception):
    """Base class for stable runtime-registry failures."""


class RuntimeDiscoveryError(RuntimeRegistryError):
    pass


@dataclass(frozen=True)
class RuntimeRecord:
    schema_version: int
    instance_id: str
    base_url: str
    pid: int
    started_at: str


class RuntimeRegistry:
    def __init__(
        self, *, data_dir: str | Path | None = None, path: str | Path | None = None
    ) -> None:
        if data_dir is not None and path is not None:
            raise ValueError("Specify either data_dir or path, not both.")
        self.path = Path(path).expanduser() if path is not None else resolve_data_dir(data_dir) / RUNTIME_FILENAME
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")

    def publish(self, record: RuntimeRecord) -> None:
        _validate_record(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._exclusive_lock():
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    newline="\n",
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    dir=self.path.parent,
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    json.dump(asdict(record), temporary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    temporary.write("\n")
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temporary_path, self.path)
                temporary_path = None
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)

    def discover(self) -> RuntimeRecord:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise RuntimeDiscoveryError(f"VoiceVault runtime record not found: {self.path}") from exc
        except OSError as exc:
            raise RuntimeDiscoveryError(f"Could not read VoiceVault runtime record: {self.path}") from exc
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != {
                "schema_version", "instance_id", "base_url", "pid", "started_at"
            }:
                raise ValueError("Runtime record fields are invalid.")
            record = RuntimeRecord(**payload)
            _validate_record(record)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeDiscoveryError(f"VoiceVault runtime record is invalid: {self.path}") from exc
        return record

    def clear(self, instance_id: str) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._exclusive_lock():
            try:
                record = self.discover()
            except RuntimeDiscoveryError:
                return False
            if record.instance_id != instance_id:
                return False
            try:
                self.path.unlink()
            except FileNotFoundError:
                return False
            return True

    @contextmanager
    def _exclusive_lock(self):
        key = str(self.lock_path.resolve()).casefold()
        with _LOCAL_LOCKS_GUARD:
            local_lock = _LOCAL_LOCKS.setdefault(key, threading.RLock())
        with local_lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a+b") as lock_file:
                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                _lock_file(lock_file)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    _unlock_file(lock_file)


def _validate_record(record: RuntimeRecord) -> None:
    if record.schema_version != RUNTIME_SCHEMA_VERSION:
        raise ValueError("Unsupported runtime schema version.")
    if not isinstance(record.instance_id, str) or not record.instance_id.strip():
        raise ValueError("Runtime instance ID is required.")
    if not isinstance(record.pid, int) or isinstance(record.pid, bool) or record.pid <= 0:
        raise ValueError("Runtime process ID must be positive.")
    if not isinstance(record.started_at, str) or not record.started_at.strip():
        raise ValueError("Runtime start time is required.")
    if not isinstance(record.base_url, str):
        raise ValueError("Runtime base URL is invalid.")
    parsed = urlsplit(record.base_url)
    if parsed.scheme != "http" or parsed.username is not None or parsed.password is not None:
        raise ValueError("Runtime base URL must be loopback HTTP.")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Runtime base URL must not include a path, query, or fragment.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Runtime base URL port is invalid.") from exc
    if parsed.hostname is None or port is None or not 1 <= port <= 65535:
        raise ValueError("Runtime base URL must include a valid port.")
    hostname = parsed.hostname.lower()
    if hostname != "localhost":
        try:
            if not ipaddress.ip_address(hostname).is_loopback:
                raise ValueError("Runtime base URL must use a loopback host.")
        except ValueError as exc:
            raise ValueError("Runtime base URL must use a loopback host.") from exc


def _lock_file(lock_file) -> None:
    if os.name == "nt":
        import msvcrt

        while True:
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.01)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def _unlock_file(lock_file) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
