from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from types import MappingProxyType
from typing import Any, Mapping

from . import post_archive
from .coverage import UtcInterval, serialize_utc


MAX_MANIFEST_BYTES = 256 * 1024
MAX_POSTS_BYTES = 32 * 1024 * 1024
MAX_CHECKPOINTS_BYTES = 32 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 1024 * 1024
MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024
MAX_TASK_BYTES = 256 * 1024 * 1024
MAX_POSTS = 10_000
MAX_CHECKPOINTS = 5_000

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_DIGITS_PATTERN = re.compile(r"[0-9]+\Z")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_UTC = timezone.utc
_FUTURE_GRACE = timedelta(minutes=5)
_COLLAPSED_PREVIEW_SUFFIX = re.compile(r"(?:…|\.\.\.)\s*\Z")

_TOP_KEYS = frozenset(
    {
        "schema_version",
        "submission_id",
        "mode",
        "job",
        "target",
        "execution",
        "outcome",
        "segments",
        "artifacts",
    }
)
_JOB_KEYS = frozenset({"job_id", "handoff_version", "collector_id"})
_TARGET_KEYS = frozenset(
    {"person_id", "account_id", "platform", "external_user_id", "requested_interval"}
)
_INTERVAL_KEYS = frozenset({"start_at", "end_at"})
_EXECUTION_KEYS = frozenset(
    {"started_at", "finished_at", "last_heartbeat_at", "remote_action_count"}
)
_OUTCOME_KEYS = frozenset(
    {"kind", "completion_reason", "stop_reason", "last_checkpoint_id"}
)
_SEGMENT_KEYS = frozenset(
    {
        "segment_id",
        "ordinal",
        "interval",
        "result",
        "completion_reason",
        "first_checkpoint_id",
        "last_checkpoint_id",
        "checkpoint_count",
    }
)
_ARTIFACTS_KEYS = frozenset({"posts", "checkpoints", "screenshots"})
_JSONL_ARTIFACT_KEYS = frozenset({"name", "sha256", "bytes", "records"})
_SCREENSHOT_KEYS = frozenset(
    {
        "evidence_key",
        "name",
        "sha256",
        "bytes",
        "media_type",
        "purpose",
        "segment_id",
    }
)
_POST_KEYS = frozenset(
    {
        "record_id",
        "segment_id",
        "checkpoint_ids",
        "external_post_id",
        "author_external_user_id",
        "author_display_name",
        "published_at",
        "captured_at",
        "source_url",
        "visible_text",
        "body_capture",
        "is_pinned",
        "observation_status",
        "content_sha256",
        "evidence_keys",
    }
)
_CHECKPOINT_KEYS = frozenset(
    {
        "checkpoint_id",
        "segment_id",
        "sequence",
        "observed_at",
        "action_type",
        "triggered_remote_load",
        "remote_action_ordinal",
        "visible_post_ids",
        "earliest_non_pinned_at",
        "latest_non_pinned_at",
        "anchor_post_id",
        "start_kind",
        "completion_reason",
        "boundary_post_id",
        "reached_end",
        "evidence_keys",
    }
)

_MODES = frozenset({"normal", "recheck"})
_OUTCOME_KINDS = frozenset({"complete", "partial"})
_COMPLETION_REASONS = frozenset({"lower_bound_crossed", "end_of_history"})
_STOP_REASONS = frozenset(
    {
        "login_required",
        "verification_required",
        "rate_limited",
        "platform_layout_changed",
        "network_error",
        "remote_action_budget_exhausted",
        "time_budget_exhausted",
        "cancel_requested",
    }
)
_START_KINDS = frozenset({"timeline_top", "stable_entry", "resume_checkpoint"})
_OBSERVATION_STATUSES = frozenset({"available", "deleted", "unavailable"})
_BODY_CAPTURE_STATES = frozenset({"full", "expanded", "not_applicable"})
_SCREENSHOT_MEDIA_TYPES = frozenset({"image/png", "image/jpeg"})
_SCREENSHOT_PURPOSES = frozenset(
    {"segment_start", "segment_end", "checkpoint", "post_status"}
)
_RESERVED_EVIDENCE_KEYS = frozenset(
    {"job-manifest", "job-posts", "job-checkpoints"}
)
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)


class CollectionResultError(Exception):
    """Base class for stable collection-result failures."""


class CollectionManifestInvalid(CollectionResultError):
    """The staged result is malformed, unsafe, or does not match its target."""


class CoverageUnproven(CollectionResultError):
    """A complete result lacks a continuous, non-pinned coverage proof."""


@dataclass(frozen=True)
class ResultSegmentTarget:
    segment_id: str
    ordinal: int
    interval: UtcInterval


@dataclass(frozen=True)
class CollectionTargetSnapshot:
    job_id: str
    handoff_version: int
    collector_id: str
    person_id: str
    account_id: str
    platform: str
    external_user_id: str
    mode: str
    requested_interval: UtcInterval
    segments: tuple[ResultSegmentTarget, ...]
    last_heartbeat_at: datetime | None
    stored_remote_action_count: int
    known_external_post_ids: frozenset[str]
    resume_checkpoint_id: str | None
    now: datetime


@dataclass(frozen=True)
class ValidatedArtifact:
    evidence_key: str
    source_path: Path
    sha256: str
    byte_size: int
    media_type: str
    purpose: str
    segment_id: str | None


@dataclass(frozen=True)
class ValidatedCheckpoint:
    checkpoint_id: str
    segment_id: str
    sequence: int
    observed_at: datetime
    action_type: str
    triggered_remote_load: bool
    remote_action_ordinal: int | None
    visible_post_ids: tuple[str, ...]
    earliest_non_pinned_at: datetime | None
    latest_non_pinned_at: datetime | None
    anchor_post_id: str | None
    start_kind: str | None
    completion_reason: str | None
    boundary_post_id: str | None
    reached_end: bool
    evidence_keys: tuple[str, ...]


@dataclass(frozen=True)
class ValidatedCollectionResult:
    submission_id: str
    manifest_sha256: str
    outcome_kind: str
    completion_reason: str | None
    stop_reason: str | None
    remote_action_count: int
    archive_records: tuple[post_archive.ArchiveRecord, ...]
    checkpoints: tuple[ValidatedCheckpoint, ...]
    artifacts: tuple[ValidatedArtifact, ...]
    accepted_manifest: Mapping[str, Any]
    coverage_intervals: tuple[UtcInterval, ...] = ()


@dataclass(frozen=True)
class _StagedArtifact:
    relative_name: str
    source_path: Path
    sha256: str
    byte_size: int


@dataclass(frozen=True)
class StagedCollectionResult:
    job_id: str
    out_dir: Path
    manifest_sha256: str
    manifest: Mapping[str, Any]
    posts: tuple[Mapping[str, Any], ...]
    checkpoints: tuple[Mapping[str, Any], ...]
    artifacts: tuple[_StagedArtifact, ...]


@dataclass(frozen=True)
class _ValidatedPost:
    record_id: str
    segment_id: str
    checkpoint_ids: tuple[str, ...]
    external_post_id: str
    published_at: datetime
    captured_at: datetime
    visible_text: str | None
    is_pinned: bool
    observation_status: str
    evidence_keys: tuple[str, ...]
    canonical_url: str


@dataclass(frozen=True)
class _ManifestSegment:
    segment_id: str
    ordinal: int
    interval: UtcInterval
    result: str
    completion_reason: str | None
    first_checkpoint_id: str | None
    last_checkpoint_id: str | None
    checkpoint_count: int


class CollectionResultLoader:
    """Read a bounded result from the server-derived exchange directory."""

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        try:
            self.data_dir = Path(data_dir).absolute()
        except (TypeError, ValueError) as error:
            raise CollectionManifestInvalid("The collection data directory is invalid.") from error

    def load(
        self, job_id: str, *, expected_manifest_sha256: str
    ) -> StagedCollectionResult:
        try:
            return self._load(job_id, expected_manifest_sha256=expected_manifest_sha256)
        except CollectionResultError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as error:
            raise CollectionManifestInvalid(
                "Collection result files could not be read safely."
            ) from error

    def _load(
        self, job_id: str, *, expected_manifest_sha256: str
    ) -> StagedCollectionResult:
        canonical_job_id = _require_canonical_uuid(job_id, "Job ID")
        expected_digest = _require_sha256(
            expected_manifest_sha256, "Expected manifest SHA-256"
        )
        jobs_dir = self.data_dir / "jobs"
        job_dir = jobs_dir / canonical_job_id
        out_dir = job_dir / "out"
        screenshots_dir = out_dir / "screenshots"
        for directory, label in (
            (self.data_dir, "data directory"),
            (jobs_dir, "jobs directory"),
            (job_dir, "job directory"),
            (out_dir, "out directory"),
            (screenshots_dir, "screenshots directory"),
        ):
            _require_safe_directory(directory, label)
        resolved_out = out_dir.resolve(strict=True)

        manifest_raw, manifest_path = _read_safe_file(
            out_dir / "manifest.json",
            out_dir=resolved_out,
            limit=MAX_MANIFEST_BYTES,
            label="manifest.json",
        )
        manifest_digest = _sha256(manifest_raw)
        if manifest_digest != expected_digest:
            raise CollectionManifestInvalid("Manifest SHA-256 does not match the submission.")
        total_bytes = len(manifest_raw)
        _require_total_limit(total_bytes)
        manifest = _parse_json_object(manifest_raw, "manifest.json")

        artifacts_value = manifest.get("artifacts")
        artifacts = _require_mapping(artifacts_value, "Manifest artifacts")
        posts_meta = _require_mapping(artifacts.get("posts"), "Posts artifact")
        checkpoints_meta = _require_mapping(
            artifacts.get("checkpoints"), "Checkpoints artifact"
        )
        screenshots_meta = _require_list(
            artifacts.get("screenshots"), "Screenshot artifacts"
        )

        posts_name, posts_declared_hash, posts_declared_bytes, posts_declared_records = (
            _parse_jsonl_artifact(posts_meta, expected_name="posts.jsonl", label="Posts")
        )
        checkpoints_name, checkpoints_declared_hash, checkpoints_declared_bytes, checkpoints_declared_records = (
            _parse_jsonl_artifact(
                checkpoints_meta,
                expected_name="checkpoints.jsonl",
                label="Checkpoints",
            )
        )

        posts_raw, posts_path = _read_safe_file(
            out_dir / posts_name,
            out_dir=resolved_out,
            limit=MAX_POSTS_BYTES,
            label=posts_name,
        )
        total_bytes += len(posts_raw)
        _require_total_limit(total_bytes)
        posts = _parse_jsonl(posts_raw, label=posts_name, max_records=MAX_POSTS)
        _match_declared_file(
            posts_raw,
            posts,
            declared_hash=posts_declared_hash,
            declared_bytes=posts_declared_bytes,
            declared_records=posts_declared_records,
            label="Posts artifact",
        )

        checkpoints_raw, checkpoints_path = _read_safe_file(
            out_dir / checkpoints_name,
            out_dir=resolved_out,
            limit=MAX_CHECKPOINTS_BYTES,
            label=checkpoints_name,
        )
        total_bytes += len(checkpoints_raw)
        _require_total_limit(total_bytes)
        checkpoints = _parse_jsonl(
            checkpoints_raw,
            label=checkpoints_name,
            max_records=MAX_CHECKPOINTS,
        )
        _match_declared_file(
            checkpoints_raw,
            checkpoints,
            declared_hash=checkpoints_declared_hash,
            declared_bytes=checkpoints_declared_bytes,
            declared_records=checkpoints_declared_records,
            label="Checkpoints artifact",
        )

        staged_artifacts = [
            _StagedArtifact("manifest.json", manifest_path, manifest_digest, len(manifest_raw)),
            _StagedArtifact(posts_name, posts_path, _sha256(posts_raw), len(posts_raw)),
            _StagedArtifact(
                checkpoints_name,
                checkpoints_path,
                _sha256(checkpoints_raw),
                len(checkpoints_raw),
            ),
        ]
        artifact_identities = {
            _file_identity(manifest_path),
            _file_identity(posts_path),
            _file_identity(checkpoints_path),
        }
        if len(artifact_identities) != 3:
            raise CollectionManifestInvalid(
                "Fixed artifacts must identify distinct regular files."
            )
        expected_files = {"manifest.json", posts_name, checkpoints_name}
        normalized_names = {os.path.normcase(name) for name in expected_files}
        for index, raw_screenshot in enumerate(screenshots_meta):
            screenshot = _require_mapping(raw_screenshot, f"Screenshot artifact {index}")
            _expect_keys(screenshot, _SCREENSHOT_KEYS, f"Screenshot artifact {index}")
            name = _validate_screenshot_name(screenshot.get("name"))
            normalized = os.path.normcase(name)
            if normalized in normalized_names:
                raise CollectionManifestInvalid("Artifact names must be unique.")
            normalized_names.add(normalized)
            declared_hash = _require_sha256(
                screenshot.get("sha256"), "Screenshot SHA-256"
            )
            declared_bytes = _require_nonnegative_int(
                screenshot.get("bytes"), "Screenshot byte size"
            )
            screenshot_raw, screenshot_path = _read_safe_file(
                out_dir.joinpath(*name.split("/")),
                out_dir=resolved_out,
                limit=MAX_SCREENSHOT_BYTES,
                label=name,
            )
            total_bytes += len(screenshot_raw)
            _require_total_limit(total_bytes)
            if len(screenshot_raw) != declared_bytes or _sha256(screenshot_raw) != declared_hash:
                raise CollectionManifestInvalid(
                    "Screenshot bytes or SHA-256 do not match the manifest."
                )
            identity = _file_identity(screenshot_path)
            if identity in artifact_identities:
                raise CollectionManifestInvalid(
                    "Artifact names must identify distinct regular files."
                )
            artifact_identities.add(identity)
            expected_files.add(name)
            staged_artifacts.append(
                _StagedArtifact(name, screenshot_path, declared_hash, declared_bytes)
            )

        _scan_expected_tree(resolved_out, expected_files)
        return StagedCollectionResult(
            job_id=canonical_job_id,
            out_dir=resolved_out,
            manifest_sha256=manifest_digest,
            manifest=_freeze_json(manifest),
            posts=tuple(_freeze_json(record) for record in posts),
            checkpoints=tuple(_freeze_json(record) for record in checkpoints),
            artifacts=tuple(staged_artifacts),
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_total_limit(total_bytes: int) -> None:
    if total_bytes > MAX_TASK_BYTES:
        raise CollectionManifestInvalid("Collection result exceeds the task byte limit.")


def _require_safe_directory(path: Path, label: str) -> None:
    _require_no_reparse_chain(path)
    try:
        info = os.lstat(path)
    except OSError as error:
        raise CollectionManifestInvalid(f"Required {label} is unavailable.") from error
    if not stat.S_ISDIR(info.st_mode):
        raise CollectionManifestInvalid(f"Required {label} is not a directory.")


def _require_no_reparse_chain(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in parts:
        current /= part
        try:
            info = os.lstat(current)
        except OSError as error:
            raise CollectionManifestInvalid("A required result path is unavailable.") from error
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise CollectionManifestInvalid("Result paths must not contain links or reparse points.")


def _read_safe_file(
    path: Path, *, out_dir: Path, limit: int, label: str
) -> tuple[bytes, Path]:
    _require_no_reparse_chain(path)
    try:
        before = os.lstat(path)
    except OSError as error:
        raise CollectionManifestInvalid(f"Required artifact is unavailable: {label}") from error
    if stat.S_ISLNK(before.st_mode) or (
        getattr(before, "st_file_attributes", 0) & _REPARSE_POINT
    ):
        raise CollectionManifestInvalid("Artifacts must not be links or reparse points.")
    if not stat.S_ISREG(before.st_mode):
        raise CollectionManifestInvalid("Artifacts must be regular files.")
    if before.st_size > limit:
        raise CollectionManifestInvalid(f"Artifact exceeds its byte limit: {label}")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(out_dir)
    except ValueError as error:
        raise CollectionManifestInvalid("Artifact resolved outside the result directory.") from error

    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (
            getattr(opened, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise CollectionManifestInvalid("Artifacts must be regular files.")
        if before.st_dev != opened.st_dev or before.st_ino != opened.st_ino:
            raise CollectionManifestInvalid("Artifact changed while it was opened.")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise CollectionManifestInvalid(f"Artifact exceeds its byte limit: {label}")
    after = os.lstat(path)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise CollectionManifestInvalid("Artifact changed while it was read.")
    return data, resolved


def _scan_expected_tree(out_dir: Path, expected_files: set[str]) -> None:
    expected_dirs = {"screenshots"}
    for name in expected_files:
        parts = name.split("/")
        for end in range(1, len(parts)):
            expected_dirs.add("/".join(parts[:end]))
    stack = [out_dir]
    while stack:
        directory = stack.pop()
        try:
            entries = tuple(os.scandir(directory))
        except OSError as error:
            raise CollectionManifestInvalid("Result directory could not be inspected.") from error
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(out_dir).as_posix()
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
            ):
                raise CollectionManifestInvalid(
                    "Result paths must not contain links or reparse points."
                )
            if stat.S_ISDIR(info.st_mode):
                if relative not in expected_dirs:
                    raise CollectionManifestInvalid("Result directory contains an undeclared entry.")
                stack.append(path)
            elif stat.S_ISREG(info.st_mode):
                if relative not in expected_files:
                    raise CollectionManifestInvalid("Result directory contains an undeclared file.")
            else:
                raise CollectionManifestInvalid("Result directory contains a non-regular entry.")


def _parse_json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CollectionManifestInvalid(f"Artifact is not strict UTF-8 JSON: {label}") from error
    if not isinstance(value, dict):
        raise CollectionManifestInvalid(f"JSON artifact must contain an object: {label}")
    _reject_invalid_json_strings(value)
    return value


def _parse_jsonl(
    data: bytes, *, label: str, max_records: int
) -> list[dict[str, Any]]:
    if not data:
        return []
    lines = data.split(b"\n")
    if lines[-1] == b"":
        lines.pop()
    if len(lines) > max_records:
        raise CollectionManifestInvalid(f"Artifact exceeds its record limit: {label}")
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            raise CollectionManifestInvalid(f"JSONL artifact contains a blank line: {label}")
        if len(line) + 1 > MAX_JSONL_LINE_BYTES:
            raise CollectionManifestInvalid(f"JSONL line exceeds its byte limit: {label}")
        records.append(_parse_json_object(line, label))
    return records


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _reject_invalid_json_strings(value: Any) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise CollectionManifestInvalid("JSON strings must contain valid Unicode values.")
    elif isinstance(value, Mapping):
        for key, nested in value.items():
            _reject_invalid_json_strings(key)
            _reject_invalid_json_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_invalid_json_strings(nested)


def _parse_jsonl_artifact(
    value: Mapping[str, Any], *, expected_name: str, label: str
) -> tuple[str, str, int, int]:
    _expect_keys(value, _JSONL_ARTIFACT_KEYS, f"{label} artifact")
    name = _require_text(value.get("name"), f"{label} artifact name")
    if name != expected_name:
        raise CollectionManifestInvalid(f"{label} artifact name is not fixed.")
    return (
        name,
        _require_sha256(value.get("sha256"), f"{label} artifact SHA-256"),
        _require_nonnegative_int(value.get("bytes"), f"{label} artifact byte size"),
        _require_nonnegative_int(value.get("records"), f"{label} artifact record count"),
    )


def _match_declared_file(
    data: bytes,
    records: list[dict[str, Any]],
    *,
    declared_hash: str,
    declared_bytes: int,
    declared_records: int,
    label: str,
) -> None:
    if (
        _sha256(data) != declared_hash
        or len(data) != declared_bytes
        or len(records) != declared_records
    ):
        raise CollectionManifestInvalid(
            f"{label} hash, byte size, or record count does not match."
        )


def _validate_screenshot_name(value: Any) -> str:
    name = _validate_relative_artifact_name(value)
    if not name.startswith("screenshots/") or len(name.split("/")) < 2:
        raise CollectionManifestInvalid("Screenshot artifacts must be inside screenshots/.")
    return name


def _validate_relative_artifact_name(value: Any) -> str:
    name = _require_text(value, "Artifact name")
    if "\\" in name or "\x00" in name:
        raise CollectionManifestInvalid("Artifact names must use canonical POSIX separators.")
    if name.startswith("/") or name.endswith("/"):
        raise CollectionManifestInvalid("Artifact names must be relative without a trailing slash.")
    windows = PureWindowsPath(name)
    if windows.drive or windows.root:
        raise CollectionManifestInvalid("Artifact names must be relative.")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise CollectionManifestInvalid("Artifact names contain a non-canonical path segment.")
    for part in parts:
        if part.endswith((".", " ")) or ":" in part:
            raise CollectionManifestInvalid(
                "Artifact names contain a Windows-ambiguous path segment."
            )
        device_stem = part.split(".", 1)[0].upper()
        if device_stem in _WINDOWS_RESERVED_NAMES:
            raise CollectionManifestInvalid(
                "Artifact names contain a reserved Windows path segment."
            )
    return name


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(nested) for key, nested in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(nested) for nested in value)
    return value


def _file_identity(path: Path) -> tuple[Any, ...]:
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise CollectionManifestInvalid("Artifact identity could not be verified.") from error
    if info.st_ino:
        return ("inode", info.st_dev, info.st_ino)
    return ("path", os.path.normcase(os.path.realpath(path)))


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(nested) for nested in value]
    return value


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CollectionManifestInvalid(f"{label} must be an object.")
    return value


def _require_list(value: Any, label: str) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise CollectionManifestInvalid(f"{label} must be an array.")
    return tuple(value)


def _expect_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    if set(value) != expected:
        raise CollectionManifestInvalid(f"{label} does not match the closed schema.")


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CollectionManifestInvalid(f"{label} must be non-empty text.")
    return value


def _optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, label)


def _require_nonnegative_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CollectionManifestInvalid(f"{label} must be a non-negative integer.")
    return value


def _require_positive_int(value: Any, label: str) -> int:
    result = _require_nonnegative_int(value, label)
    if result == 0:
        raise CollectionManifestInvalid(f"{label} must be positive.")
    return result


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise CollectionManifestInvalid(
            f"{label} must be 64 lowercase hexadecimal characters."
        )
    return value


def _require_canonical_uuid(value: Any, label: str) -> str:
    text = _require_text(value, label)
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError) as error:
        raise CollectionManifestInvalid(f"{label} must be a canonical UUID.") from error
    if str(parsed) != text:
        raise CollectionManifestInvalid(f"{label} must be a canonical UUID.")
    return text


def _require_digits(value: Any, label: str) -> str:
    text = _require_text(value, label)
    if _DIGITS_PATTERN.fullmatch(text) is None:
        raise CollectionManifestInvalid(f"{label} must contain only decimal digits.")
    return text


class CollectionResultValidator:
    """Validate staged collection observations without writing business state."""

    def validate(
        self,
        staged: StagedCollectionResult,
        *,
        snapshot: CollectionTargetSnapshot,
    ) -> ValidatedCollectionResult:
        if not isinstance(staged, StagedCollectionResult):
            raise CollectionManifestInvalid("Staged result has an unsupported type.")
        snapshot_now, snapshot_heartbeat = _validate_snapshot(snapshot)
        manifest = _require_mapping(staged.manifest, "Manifest")
        _expect_keys(manifest, _TOP_KEYS, "Manifest")
        schema_version = manifest.get("schema_version")
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version != 1
        ):
            raise CollectionManifestInvalid("Manifest schema_version must be 1.")
        submission_id = _require_canonical_uuid(
            manifest.get("submission_id"), "Submission ID"
        )
        mode = _require_text(manifest.get("mode"), "Collection mode")
        if mode not in _MODES:
            raise CollectionManifestInvalid("Collection mode is unsupported.")
        if mode != snapshot.mode:
            raise CollectionManifestInvalid("Collection mode does not match the job.")

        self._validate_job_and_target(manifest, staged=staged, snapshot=snapshot)
        (
            started_at,
            finished_at,
            last_heartbeat_at,
            remote_action_count,
        ) = self._validate_execution(
            manifest,
            snapshot=snapshot,
            snapshot_now=snapshot_now,
            snapshot_heartbeat=snapshot_heartbeat,
        )
        outcome_kind, completion_reason, stop_reason, last_checkpoint_id = (
            self._validate_outcome(manifest)
        )
        segments = self._validate_segments(
            manifest,
            snapshot=snapshot,
            outcome_kind=outcome_kind,
        )
        segment_by_id = {segment.segment_id: segment for segment in segments}
        artifacts, screenshot_by_key = self._validate_artifacts(
            staged,
            manifest,
            segment_by_id=segment_by_id,
        )
        posts, post_by_external_id = self._validate_posts(
            staged,
            snapshot=snapshot,
            segment_by_id=segment_by_id,
            screenshot_by_key=screenshot_by_key,
            started_at=started_at,
            finished_at=finished_at,
        )
        checkpoints = self._validate_checkpoints(
            staged,
            segment_by_id=segment_by_id,
            posts=posts,
            post_by_external_id=post_by_external_id,
            screenshot_by_key=screenshot_by_key,
            started_at=started_at,
            finished_at=finished_at,
            outcome_kind=outcome_kind,
        )
        checkpoints_by_segment = self._validate_continuity_and_remote_actions(
            checkpoints,
            posts=posts,
            segments=segments,
            outcome_kind=outcome_kind,
            last_checkpoint_id=last_checkpoint_id,
            remote_action_count=remote_action_count,
            stored_remote_action_count=snapshot.stored_remote_action_count,
        )

        coverage_intervals: tuple[UtcInterval, ...] = ()
        if outcome_kind == "complete":
            self._validate_complete_coverage(
                segments=segments,
                checkpoints_by_segment=checkpoints_by_segment,
                post_by_external_id=post_by_external_id,
                screenshot_by_key=screenshot_by_key,
                outcome_completion_reason=completion_reason,
                snapshot=snapshot,
            )
            if mode == "normal":
                coverage_intervals = tuple(segment.interval for segment in segments)

        archive_records = tuple(
            post_archive.ArchiveRecord(
                external_post_id=post.external_post_id,
                published_at=post.published_at,
                captured_at=post.captured_at,
                canonical_url=post.canonical_url,
                content_text=post.visible_text,
                observation_status=post.observation_status,
                evidence_keys=post.evidence_keys,
            )
            for post in posts
            if (
                segment_by_id[post.segment_id].interval.start_at
                <= post.published_at
                < segment_by_id[post.segment_id].interval.end_at
            )
        )
        accepted_manifest = _canonical_accepted_manifest(
            manifest,
            started_at=started_at,
            finished_at=finished_at,
            last_heartbeat_at=last_heartbeat_at,
            requested_interval=snapshot.requested_interval,
            segments=segments,
        )
        return ValidatedCollectionResult(
            submission_id=submission_id,
            manifest_sha256=staged.manifest_sha256,
            outcome_kind=outcome_kind,
            completion_reason=completion_reason,
            stop_reason=stop_reason,
            remote_action_count=remote_action_count,
            archive_records=archive_records,
            checkpoints=checkpoints,
            artifacts=artifacts,
            accepted_manifest=accepted_manifest,
            coverage_intervals=coverage_intervals,
        )

    @staticmethod
    def _validate_job_and_target(
        manifest: Mapping[str, Any],
        *,
        staged: StagedCollectionResult,
        snapshot: CollectionTargetSnapshot,
    ) -> None:
        job = _require_mapping(manifest.get("job"), "Manifest job")
        _expect_keys(job, _JOB_KEYS, "Manifest job")
        job_id = _require_canonical_uuid(job.get("job_id"), "Manifest job ID")
        handoff_version = _require_positive_int(
            job.get("handoff_version"), "Handoff version"
        )
        collector_id = _require_text(job.get("collector_id"), "Collector ID")
        if (
            job_id != snapshot.job_id
            or job_id != staged.job_id
            or handoff_version != snapshot.handoff_version
            or collector_id != snapshot.collector_id
        ):
            raise CollectionManifestInvalid("Manifest job identity does not match the job.")

        target = _require_mapping(manifest.get("target"), "Manifest target")
        _expect_keys(target, _TARGET_KEYS, "Manifest target")
        person_id = _require_canonical_uuid(target.get("person_id"), "Person ID")
        account_id = _require_canonical_uuid(target.get("account_id"), "Account ID")
        platform = _require_text(target.get("platform"), "Platform")
        external_user_id = _require_digits(
            target.get("external_user_id"), "External user ID"
        )
        requested = _parse_interval(target.get("requested_interval"), "Requested interval")
        if (
            person_id != snapshot.person_id
            or account_id != snapshot.account_id
            or platform != snapshot.platform
            or external_user_id != snapshot.external_user_id
            or requested != snapshot.requested_interval
        ):
            raise CollectionManifestInvalid("Manifest target does not match the job target.")
        if platform != "xueqiu":
            raise CollectionManifestInvalid("Only xueqiu collection results are supported.")

    @staticmethod
    def _validate_execution(
        manifest: Mapping[str, Any],
        *,
        snapshot: CollectionTargetSnapshot,
        snapshot_now: datetime,
        snapshot_heartbeat: datetime | None,
    ) -> tuple[datetime, datetime, datetime | None, int]:
        execution = _require_mapping(manifest.get("execution"), "Manifest execution")
        _expect_keys(execution, _EXECUTION_KEYS, "Manifest execution")
        started_at = _parse_time(execution.get("started_at"), "Execution start")
        finished_at = _parse_time(execution.get("finished_at"), "Execution finish")
        last_heartbeat_at = _parse_optional_time(
            execution.get("last_heartbeat_at"), "Last heartbeat"
        )
        remote_action_count = _require_nonnegative_int(
            execution.get("remote_action_count"), "Remote action count"
        )
        if started_at > finished_at:
            raise CollectionManifestInvalid("Execution start must not follow its finish.")
        if finished_at > snapshot_now + _FUTURE_GRACE:
            raise CollectionManifestInvalid("Execution finish is too far in the future.")
        if last_heartbeat_at is not None and not (
            started_at <= last_heartbeat_at <= finished_at
        ):
            raise CollectionManifestInvalid("Last heartbeat is outside the execution window.")
        if last_heartbeat_at != snapshot_heartbeat:
            raise CollectionManifestInvalid("Last heartbeat does not match stored job state.")
        if remote_action_count < snapshot.stored_remote_action_count:
            raise CollectionManifestInvalid(
                "Remote action count is below the stored job count."
            )
        return started_at, finished_at, last_heartbeat_at, remote_action_count

    @staticmethod
    def _validate_outcome(
        manifest: Mapping[str, Any],
    ) -> tuple[str, str | None, str | None, str | None]:
        outcome = _require_mapping(manifest.get("outcome"), "Manifest outcome")
        _expect_keys(outcome, _OUTCOME_KEYS, "Manifest outcome")
        kind = _require_text(outcome.get("kind"), "Outcome kind")
        if kind not in _OUTCOME_KINDS:
            raise CollectionManifestInvalid("Outcome kind is unsupported.")
        completion_reason = _optional_text(
            outcome.get("completion_reason"), "Completion reason"
        )
        stop_reason = _optional_text(outcome.get("stop_reason"), "Stop reason")
        last_checkpoint_id = _optional_text(
            outcome.get("last_checkpoint_id"), "Last checkpoint ID"
        )
        if completion_reason is not None and completion_reason not in _COMPLETION_REASONS:
            raise CollectionManifestInvalid("Completion reason is unsupported.")
        if kind == "complete":
            if completion_reason is None:
                raise CoverageUnproven("Complete result has no completion reason.")
            if stop_reason is not None:
                raise CoverageUnproven("Complete result must not contain a stop reason.")
        else:
            if completion_reason is not None:
                raise CollectionManifestInvalid(
                    "Partial result must not claim a completion reason."
                )
            if stop_reason not in _STOP_REASONS:
                raise CollectionManifestInvalid("Partial result stop reason is unsupported.")
        return kind, completion_reason, stop_reason, last_checkpoint_id

    @staticmethod
    def _validate_segments(
        manifest: Mapping[str, Any],
        *,
        snapshot: CollectionTargetSnapshot,
        outcome_kind: str,
    ) -> tuple[_ManifestSegment, ...]:
        raw_segments = _require_list(manifest.get("segments"), "Manifest segments")
        if len(raw_segments) != len(snapshot.segments):
            raise CollectionManifestInvalid("Manifest segment set does not match the job.")
        segments: list[_ManifestSegment] = []
        seen_ids: set[str] = set()
        for index, (raw_segment, expected) in enumerate(
            zip(raw_segments, snapshot.segments, strict=True)
        ):
            segment = _require_mapping(raw_segment, f"Manifest segment {index}")
            _expect_keys(segment, _SEGMENT_KEYS, f"Manifest segment {index}")
            segment_id = _require_canonical_uuid(
                segment.get("segment_id"), "Segment ID"
            )
            if segment_id in seen_ids:
                raise CollectionManifestInvalid("Manifest segment IDs must be unique.")
            seen_ids.add(segment_id)
            ordinal = _require_nonnegative_int(segment.get("ordinal"), "Segment ordinal")
            interval = _parse_interval(segment.get("interval"), "Segment interval")
            result = _require_text(segment.get("result"), "Segment result")
            if result not in _OUTCOME_KINDS:
                raise CollectionManifestInvalid("Segment result is unsupported.")
            completion_reason = _optional_text(
                segment.get("completion_reason"), "Segment completion reason"
            )
            if completion_reason is not None and completion_reason not in _COMPLETION_REASONS:
                raise CollectionManifestInvalid("Segment completion reason is unsupported.")
            first_checkpoint_id = _optional_text(
                segment.get("first_checkpoint_id"), "First checkpoint ID"
            )
            last_checkpoint_id = _optional_text(
                segment.get("last_checkpoint_id"), "Last checkpoint ID"
            )
            checkpoint_count = _require_nonnegative_int(
                segment.get("checkpoint_count"), "Segment checkpoint count"
            )
            if checkpoint_count == 0:
                if first_checkpoint_id is not None or last_checkpoint_id is not None:
                    raise CollectionManifestInvalid(
                        "Empty segment checkpoint metadata is inconsistent."
                    )
            elif first_checkpoint_id is None or last_checkpoint_id is None:
                _raise_proof_or_invalid(
                    outcome_kind, "Segment checkpoint endpoints are missing."
                )
            if (
                segment_id != expected.segment_id
                or ordinal != expected.ordinal
                or interval != expected.interval
            ):
                raise CollectionManifestInvalid("Manifest segment does not match the job.")
            if outcome_kind == "complete" and result != "complete":
                raise CoverageUnproven("Complete outcome contains a partial segment.")
            if result == "partial" and completion_reason is not None:
                raise CollectionManifestInvalid(
                    "Partial segment must not claim a completion reason."
                )
            segments.append(
                _ManifestSegment(
                    segment_id=segment_id,
                    ordinal=ordinal,
                    interval=interval,
                    result=result,
                    completion_reason=completion_reason,
                    first_checkpoint_id=first_checkpoint_id,
                    last_checkpoint_id=last_checkpoint_id,
                    checkpoint_count=checkpoint_count,
                )
            )
        return tuple(segments)

    @staticmethod
    def _validate_artifacts(
        staged: StagedCollectionResult,
        manifest: Mapping[str, Any],
        *,
        segment_by_id: Mapping[str, _ManifestSegment],
    ) -> tuple[tuple[ValidatedArtifact, ...], dict[str, ValidatedArtifact]]:
        artifact_manifest = _require_mapping(manifest.get("artifacts"), "Manifest artifacts")
        _expect_keys(artifact_manifest, _ARTIFACTS_KEYS, "Manifest artifacts")
        posts_meta = _require_mapping(artifact_manifest.get("posts"), "Posts artifact")
        checkpoints_meta = _require_mapping(
            artifact_manifest.get("checkpoints"), "Checkpoints artifact"
        )
        screenshots_meta = _require_list(
            artifact_manifest.get("screenshots"), "Screenshot artifacts"
        )
        posts_name, posts_hash, posts_bytes, _ = _parse_jsonl_artifact(
            posts_meta, expected_name="posts.jsonl", label="Posts"
        )
        checkpoints_name, checkpoints_hash, checkpoints_bytes, _ = _parse_jsonl_artifact(
            checkpoints_meta,
            expected_name="checkpoints.jsonl",
            label="Checkpoints",
        )
        staged_by_name = {artifact.relative_name: artifact for artifact in staged.artifacts}
        if len(staged_by_name) != len(staged.artifacts):
            raise CollectionManifestInvalid("Staged artifact names are not unique.")
        staged_identities = {
            _file_identity(artifact.source_path) for artifact in staged.artifacts
        }
        if len(staged_identities) != len(staged.artifacts):
            raise CollectionManifestInvalid(
                "Staged artifacts must identify distinct regular files."
            )

        def fixed_artifact(
            name: str,
            *,
            evidence_key: str,
            sha256: str,
            byte_size: int,
            media_type: str,
            purpose: str,
        ) -> ValidatedArtifact:
            staged_artifact = staged_by_name.get(name)
            if staged_artifact is None or (
                staged_artifact.sha256 != sha256
                or staged_artifact.byte_size != byte_size
            ):
                raise CollectionManifestInvalid("Staged artifact metadata is inconsistent.")
            return ValidatedArtifact(
                evidence_key=evidence_key,
                source_path=staged_artifact.source_path,
                sha256=sha256,
                byte_size=byte_size,
                media_type=media_type,
                purpose=purpose,
                segment_id=None,
            )

        manifest_artifact = staged_by_name.get("manifest.json")
        if manifest_artifact is None or manifest_artifact.sha256 != staged.manifest_sha256:
            raise CollectionManifestInvalid("Staged manifest metadata is inconsistent.")
        validated = [
            ValidatedArtifact(
                evidence_key="job-manifest",
                source_path=manifest_artifact.source_path,
                sha256=manifest_artifact.sha256,
                byte_size=manifest_artifact.byte_size,
                media_type="application/json",
                purpose="manifest",
                segment_id=None,
            ),
            fixed_artifact(
                posts_name,
                evidence_key="job-posts",
                sha256=posts_hash,
                byte_size=posts_bytes,
                media_type="application/x-ndjson",
                purpose="posts",
            ),
            fixed_artifact(
                checkpoints_name,
                evidence_key="job-checkpoints",
                sha256=checkpoints_hash,
                byte_size=checkpoints_bytes,
                media_type="application/x-ndjson",
                purpose="checkpoints",
            ),
        ]
        screenshots: dict[str, ValidatedArtifact] = {}
        screenshot_names: set[str] = set()
        for index, raw_screenshot in enumerate(screenshots_meta):
            screenshot = _require_mapping(raw_screenshot, f"Screenshot artifact {index}")
            _expect_keys(screenshot, _SCREENSHOT_KEYS, f"Screenshot artifact {index}")
            evidence_key = _require_text(
                screenshot.get("evidence_key"), "Screenshot evidence key"
            )
            if evidence_key in _RESERVED_EVIDENCE_KEYS or evidence_key in screenshots:
                raise CollectionManifestInvalid("Screenshot evidence keys must be unique.")
            name = _validate_screenshot_name(screenshot.get("name"))
            if name in screenshot_names:
                raise CollectionManifestInvalid("Screenshot artifact names must be unique.")
            screenshot_names.add(name)
            sha256 = _require_sha256(screenshot.get("sha256"), "Screenshot SHA-256")
            byte_size = _require_nonnegative_int(
                screenshot.get("bytes"), "Screenshot byte size"
            )
            media_type = _require_text(
                screenshot.get("media_type"), "Screenshot media type"
            )
            if media_type not in _SCREENSHOT_MEDIA_TYPES:
                raise CollectionManifestInvalid("Screenshot media type is unsupported.")
            purpose = _require_text(screenshot.get("purpose"), "Screenshot purpose")
            if purpose not in _SCREENSHOT_PURPOSES:
                raise CollectionManifestInvalid("Screenshot purpose is unsupported.")
            segment_id = _require_canonical_uuid(
                screenshot.get("segment_id"), "Screenshot segment ID"
            )
            if segment_id not in segment_by_id:
                raise CollectionManifestInvalid(
                    "Screenshot does not belong to a manifest segment."
                )
            staged_artifact = staged_by_name.get(name)
            if staged_artifact is None or (
                staged_artifact.sha256 != sha256
                or staged_artifact.byte_size != byte_size
            ):
                raise CollectionManifestInvalid("Staged screenshot metadata is inconsistent.")
            artifact = ValidatedArtifact(
                evidence_key=evidence_key,
                source_path=staged_artifact.source_path,
                sha256=sha256,
                byte_size=byte_size,
                media_type=media_type,
                purpose=purpose,
                segment_id=segment_id,
            )
            screenshots[evidence_key] = artifact
            validated.append(artifact)
        if set(staged_by_name) != {
            "manifest.json",
            posts_name,
            checkpoints_name,
            *screenshot_names,
        }:
            raise CollectionManifestInvalid("Staged artifacts do not match the manifest.")
        return tuple(validated), screenshots

    @staticmethod
    def _validate_posts(
        staged: StagedCollectionResult,
        *,
        snapshot: CollectionTargetSnapshot,
        segment_by_id: Mapping[str, _ManifestSegment],
        screenshot_by_key: Mapping[str, ValidatedArtifact],
        started_at: datetime,
        finished_at: datetime,
    ) -> tuple[tuple[_ValidatedPost, ...], dict[str, _ValidatedPost]]:
        posts: list[_ValidatedPost] = []
        by_external_id: dict[str, _ValidatedPost] = {}
        record_ids: set[str] = set()
        for index, raw_record in enumerate(staged.posts):
            record = _require_mapping(raw_record, f"Post record {index}")
            _expect_keys(record, _POST_KEYS, f"Post record {index}")
            record_id = _require_text(record.get("record_id"), "Record ID")
            if record_id in record_ids:
                raise CollectionManifestInvalid("Record IDs must be unique.")
            record_ids.add(record_id)
            segment_id = _require_canonical_uuid(
                record.get("segment_id"), "Post segment ID"
            )
            if segment_id not in segment_by_id:
                raise CollectionManifestInvalid("Post does not belong to a manifest segment.")
            checkpoint_ids = _require_string_tuple(
                record.get("checkpoint_ids"), "Post checkpoint IDs", allow_empty=False
            )
            external_post_id = _require_digits(
                record.get("external_post_id"), "External post ID"
            )
            if external_post_id in by_external_id:
                raise CollectionManifestInvalid(
                    "External post IDs must be unique within a result."
                )
            author_external_user_id = _require_digits(
                record.get("author_external_user_id"), "Author external user ID"
            )
            if author_external_user_id != snapshot.external_user_id:
                raise CollectionManifestInvalid("Post author does not match the target account.")
            _require_text(record.get("author_display_name"), "Author display name")
            published_at = _parse_time(record.get("published_at"), "Post published time")
            captured_at = _parse_time(record.get("captured_at"), "Post captured time")
            if not started_at <= captured_at <= finished_at:
                raise CollectionManifestInvalid("Post capture is outside the execution window.")
            if published_at > captured_at:
                raise CollectionManifestInvalid("Post publication follows its capture time.")
            canonical_url = (
                f"https://xueqiu.com/{snapshot.external_user_id}/{external_post_id}"
            )
            source_url = _require_text(record.get("source_url"), "Post source URL")
            if source_url != canonical_url:
                raise CollectionManifestInvalid("Post source URL is not canonical.")
            is_pinned = _require_bool(record.get("is_pinned"), "Pinned marker")
            observation_status = _require_text(
                record.get("observation_status"), "Observation status"
            )
            if observation_status not in _OBSERVATION_STATUSES:
                raise CollectionManifestInvalid("Observation status is unsupported.")
            body_capture = _require_text(record.get("body_capture"), "Post body capture")
            if body_capture not in _BODY_CAPTURE_STATES:
                raise CollectionManifestInvalid("Post body capture state is unsupported.")
            evidence_keys = _require_string_tuple(
                record.get("evidence_keys"), "Post evidence keys", allow_empty=True
            )
            _validate_evidence_references(
                evidence_keys,
                segment_id=segment_id,
                screenshot_by_key=screenshot_by_key,
            )
            visible_text = record.get("visible_text")
            content_sha256 = record.get("content_sha256")
            if observation_status == "available":
                if body_capture not in {"full", "expanded"}:
                    raise CollectionManifestInvalid(
                        "Available post body must be captured in full."
                    )
                if not isinstance(visible_text, str):
                    raise CollectionManifestInvalid(
                        "Available post visible text must be text."
                    )
                collector_hash = _require_sha256(
                    content_sha256, "Post content SHA-256"
                )
                try:
                    server_hash = post_archive.post_content_sha256(visible_text)
                except post_archive.PostArchiveError as error:
                    raise CollectionManifestInvalid(
                        "Available post visible text is empty after normalization."
                    ) from error
                if collector_hash != server_hash:
                    raise CollectionManifestInvalid("Post content SHA-256 does not match.")
                if _COLLAPSED_PREVIEW_SUFFIX.search(visible_text):
                    raise CollectionManifestInvalid(
                        "Available post body appears to be a collapsed preview."
                    )
            else:
                if visible_text is not None or content_sha256 is not None:
                    raise CollectionManifestInvalid(
                        "Status-only observation must have null text and content hash."
                    )
                if external_post_id not in snapshot.known_external_post_ids:
                    raise CollectionManifestInvalid(
                        "Status-only observation does not identify a known post."
                    )
            validated = _ValidatedPost(
                record_id=record_id,
                segment_id=segment_id,
                checkpoint_ids=checkpoint_ids,
                external_post_id=external_post_id,
                published_at=published_at,
                captured_at=captured_at,
                visible_text=visible_text,
                is_pinned=is_pinned,
                observation_status=observation_status,
                evidence_keys=evidence_keys,
                canonical_url=canonical_url,
            )
            posts.append(validated)
            by_external_id[external_post_id] = validated
        return tuple(posts), by_external_id

    @staticmethod
    def _validate_checkpoints(
        staged: StagedCollectionResult,
        *,
        segment_by_id: Mapping[str, _ManifestSegment],
        posts: tuple[_ValidatedPost, ...],
        post_by_external_id: Mapping[str, _ValidatedPost],
        screenshot_by_key: Mapping[str, ValidatedArtifact],
        started_at: datetime,
        finished_at: datetime,
        outcome_kind: str,
    ) -> tuple[ValidatedCheckpoint, ...]:
        checkpoints: list[ValidatedCheckpoint] = []
        checkpoint_ids: set[str] = set()
        referenced_evidence: set[str] = set()
        for post in posts:
            referenced_evidence.update(post.evidence_keys)
        for index, raw_checkpoint in enumerate(staged.checkpoints):
            checkpoint = _require_mapping(raw_checkpoint, f"Checkpoint {index}")
            _expect_keys(checkpoint, _CHECKPOINT_KEYS, f"Checkpoint {index}")
            checkpoint_id = _require_text(
                checkpoint.get("checkpoint_id"), "Checkpoint ID"
            )
            if checkpoint_id in checkpoint_ids:
                raise CollectionManifestInvalid("Checkpoint IDs must be unique.")
            checkpoint_ids.add(checkpoint_id)
            segment_id = _require_canonical_uuid(
                checkpoint.get("segment_id"), "Checkpoint segment ID"
            )
            if segment_id not in segment_by_id:
                raise CollectionManifestInvalid(
                    "Checkpoint does not belong to a manifest segment."
                )
            sequence = _require_nonnegative_int(
                checkpoint.get("sequence"), "Checkpoint sequence"
            )
            observed_at = _parse_time(
                checkpoint.get("observed_at"), "Checkpoint observed time"
            )
            if not started_at <= observed_at <= finished_at:
                raise CollectionManifestInvalid(
                    "Checkpoint observation is outside the execution window."
                )
            action_type = _require_text(
                checkpoint.get("action_type"), "Checkpoint action type"
            )
            triggered_remote_load = _require_bool(
                checkpoint.get("triggered_remote_load"), "Remote-load marker"
            )
            ordinal_value = checkpoint.get("remote_action_ordinal")
            if triggered_remote_load:
                remote_action_ordinal = _require_positive_int(
                    ordinal_value, "Remote action ordinal"
                )
            else:
                if ordinal_value is not None:
                    raise CollectionManifestInvalid(
                        "Non-remote checkpoint must not have a remote action ordinal."
                    )
                remote_action_ordinal = None
            visible_post_ids = _require_digit_tuple(
                checkpoint.get("visible_post_ids"),
                "Visible post IDs",
                allow_empty=True,
            )
            visible_posts: list[_ValidatedPost] = []
            for external_post_id in visible_post_ids:
                post = post_by_external_id.get(external_post_id)
                if post is None:
                    raise CollectionManifestInvalid(
                        "Checkpoint references an unknown visible post."
                    )
                if post.segment_id != segment_id:
                    raise CollectionManifestInvalid(
                        "Checkpoint visible post belongs to another segment."
                    )
                if checkpoint_id not in post.checkpoint_ids:
                    raise CollectionManifestInvalid(
                        "Checkpoint and post references are not reciprocal."
                    )
                visible_posts.append(post)
            earliest_non_pinned_at = _parse_optional_time(
                checkpoint.get("earliest_non_pinned_at"),
                "Earliest non-pinned time",
            )
            latest_non_pinned_at = _parse_optional_time(
                checkpoint.get("latest_non_pinned_at"),
                "Latest non-pinned time",
            )
            non_pinned_times = [
                post.published_at
                for post in visible_posts
                if _is_coverage_candidate(post)
            ]
            recalculated_earliest = min(non_pinned_times) if non_pinned_times else None
            recalculated_latest = max(non_pinned_times) if non_pinned_times else None
            if (
                earliest_non_pinned_at != recalculated_earliest
                or latest_non_pinned_at != recalculated_latest
            ):
                raise CollectionManifestInvalid(
                    "Checkpoint non-pinned time range does not match visible posts."
                )
            anchor_post_id = _parse_optional_digits(
                checkpoint.get("anchor_post_id"), "Checkpoint anchor post ID"
            )
            start_kind = _optional_text(
                checkpoint.get("start_kind"), "Checkpoint start kind"
            )
            if start_kind is not None and start_kind not in _START_KINDS:
                _raise_proof_or_invalid(outcome_kind, "Checkpoint start kind is unsupported.")
            completion_reason = _optional_text(
                checkpoint.get("completion_reason"), "Checkpoint completion reason"
            )
            if completion_reason is not None and completion_reason not in _COMPLETION_REASONS:
                raise CollectionManifestInvalid(
                    "Checkpoint completion reason is unsupported."
                )
            boundary_post_id = _parse_optional_digits(
                checkpoint.get("boundary_post_id"), "Boundary post ID"
            )
            reached_end = _require_bool(
                checkpoint.get("reached_end"), "Reached-end marker"
            )
            evidence_keys = _require_string_tuple(
                checkpoint.get("evidence_keys"),
                "Checkpoint evidence keys",
                allow_empty=True,
            )
            _validate_evidence_references(
                evidence_keys,
                segment_id=segment_id,
                screenshot_by_key=screenshot_by_key,
            )
            referenced_evidence.update(evidence_keys)
            checkpoints.append(
                ValidatedCheckpoint(
                    checkpoint_id=checkpoint_id,
                    segment_id=segment_id,
                    sequence=sequence,
                    observed_at=observed_at,
                    action_type=action_type,
                    triggered_remote_load=triggered_remote_load,
                    remote_action_ordinal=remote_action_ordinal,
                    visible_post_ids=visible_post_ids,
                    earliest_non_pinned_at=earliest_non_pinned_at,
                    latest_non_pinned_at=latest_non_pinned_at,
                    anchor_post_id=anchor_post_id,
                    start_kind=start_kind,
                    completion_reason=completion_reason,
                    boundary_post_id=boundary_post_id,
                    reached_end=reached_end,
                    evidence_keys=evidence_keys,
                )
            )

        by_checkpoint_id = {
            checkpoint.checkpoint_id: checkpoint for checkpoint in checkpoints
        }
        for post in posts:
            for checkpoint_id in post.checkpoint_ids:
                checkpoint = by_checkpoint_id.get(checkpoint_id)
                if checkpoint is None:
                    raise CollectionManifestInvalid(
                        "Post references an unknown checkpoint."
                    )
                if checkpoint.segment_id != post.segment_id:
                    raise CollectionManifestInvalid(
                        "Post checkpoint belongs to another segment."
                    )
                if post.external_post_id not in checkpoint.visible_post_ids:
                    raise CollectionManifestInvalid(
                        "Post and checkpoint references are not reciprocal."
                    )
        if referenced_evidence != set(screenshot_by_key):
            raise CollectionManifestInvalid(
                "Screenshot evidence contains an unknown or unreferenced key."
            )
        return tuple(checkpoints)

    @staticmethod
    def _validate_continuity_and_remote_actions(
        checkpoints: tuple[ValidatedCheckpoint, ...],
        *,
        posts: tuple[_ValidatedPost, ...],
        segments: tuple[_ManifestSegment, ...],
        outcome_kind: str,
        last_checkpoint_id: str | None,
        remote_action_count: int,
        stored_remote_action_count: int,
    ) -> dict[str, tuple[ValidatedCheckpoint, ...]]:
        groups: dict[str, list[ValidatedCheckpoint]] = defaultdict(list)
        for checkpoint in checkpoints:
            groups[checkpoint.segment_id].append(checkpoint)
        post_by_external_id = {post.external_post_id: post for post in posts}
        ordered_groups: dict[str, tuple[ValidatedCheckpoint, ...]] = {}
        flattened: list[ValidatedCheckpoint] = []
        for segment in segments:
            group = groups.get(segment.segment_id, [])
            if [checkpoint.sequence for checkpoint in group] != list(range(len(group))):
                _raise_proof_or_invalid(
                    outcome_kind, "Checkpoint sequence is not contiguous from zero."
                )
            if len(group) != segment.checkpoint_count:
                _raise_proof_or_invalid(
                    outcome_kind, "Checkpoint count does not match the segment manifest."
                )
            if group:
                if (
                    group[0].checkpoint_id != segment.first_checkpoint_id
                    or group[-1].checkpoint_id != segment.last_checkpoint_id
                ):
                    _raise_proof_or_invalid(
                        outcome_kind,
                        "Checkpoint endpoints do not match the segment manifest.",
                    )
                for previous, current in zip(group, group[1:]):
                    if previous.observed_at > current.observed_at:
                        raise CoverageUnproven(
                            "Checkpoint observation times must be monotonic by sequence."
                        )
                    anchor = current.anchor_post_id
                    anchor_post = (
                        post_by_external_id.get(anchor) if anchor is not None else None
                    )
                    if (
                        anchor is None
                        or anchor_post is None
                        or anchor_post.segment_id != segment.segment_id
                        or not _is_coverage_candidate(anchor_post)
                        or anchor not in previous.visible_post_ids
                        or anchor not in current.visible_post_ids
                    ):
                        raise CoverageUnproven(
                            "Adjacent checkpoints lack an available non-pinned shared anchor."
                        )
            elif segment.first_checkpoint_id is not None or segment.last_checkpoint_id is not None:
                _raise_proof_or_invalid(
                    outcome_kind, "Empty checkpoint segment declares endpoint IDs."
                )
            ordered = tuple(group)
            ordered_groups[segment.segment_id] = ordered
            flattened.extend(ordered)
        expected_last = flattened[-1].checkpoint_id if flattened else None
        if last_checkpoint_id != expected_last:
            _raise_proof_or_invalid(
                outcome_kind, "Outcome last checkpoint does not match checkpoint continuity."
            )

        triggered = [
            checkpoint for checkpoint in flattened if checkpoint.triggered_remote_load
        ]
        if [checkpoint.remote_action_ordinal for checkpoint in triggered] != list(
            range(1, len(triggered) + 1)
        ):
            raise CollectionManifestInvalid(
                "Remote action ordinals are not contiguous from one."
            )
        if len(triggered) != remote_action_count:
            raise CollectionManifestInvalid(
                "Remote action count does not match triggered checkpoints."
            )
        if remote_action_count < stored_remote_action_count:
            raise CollectionManifestInvalid(
                "Remote action count is below the stored job count."
            )
        return ordered_groups

    @staticmethod
    def _validate_complete_coverage(
        *,
        segments: tuple[_ManifestSegment, ...],
        checkpoints_by_segment: Mapping[str, tuple[ValidatedCheckpoint, ...]],
        post_by_external_id: Mapping[str, _ValidatedPost],
        screenshot_by_key: Mapping[str, ValidatedArtifact],
        outcome_completion_reason: str | None,
        snapshot: CollectionTargetSnapshot,
    ) -> None:
        for segment in segments:
            if segment.result != "complete":
                raise CoverageUnproven("Complete result contains a partial segment.")
            checkpoints = checkpoints_by_segment[segment.segment_id]
            if not checkpoints:
                raise CoverageUnproven("Complete segment has no checkpoints.")
            first = checkpoints[0]
            last = checkpoints[-1]
            if not _checkpoint_has_artifact_purpose(
                first,
                purpose="segment_start",
                screenshot_by_key=screenshot_by_key,
            ):
                raise CoverageUnproven("Complete segment has no start screenshot.")
            if not _checkpoint_has_artifact_purpose(
                last,
                purpose="segment_end",
                screenshot_by_key=screenshot_by_key,
            ):
                raise CoverageUnproven("Complete segment has no end screenshot.")
            if first.start_kind not in _START_KINDS:
                raise CoverageUnproven("Complete segment start kind is unsupported.")
            if first.start_kind == "stable_entry":
                if (
                    first.latest_non_pinned_at is None
                    or first.latest_non_pinned_at < segment.interval.end_at
                ):
                    raise CoverageUnproven(
                        "Stable-entry start does not reach the segment upper bound."
                    )
            elif first.start_kind == "resume_checkpoint":
                if (
                    snapshot.resume_checkpoint_id is None
                    or first.checkpoint_id != snapshot.resume_checkpoint_id
                ):
                    raise CoverageUnproven(
                        "Resume start does not match the stored resume checkpoint."
                    )
            if (
                segment.completion_reason != outcome_completion_reason
                or last.completion_reason != outcome_completion_reason
            ):
                raise CoverageUnproven(
                    "Segment, checkpoint, and outcome completion reasons differ."
                )

            if outcome_completion_reason == "lower_bound_crossed":
                first_earlier: _ValidatedPost | None = None
                seen_posts: set[str] = set()
                for checkpoint in checkpoints:
                    for external_post_id in checkpoint.visible_post_ids:
                        if external_post_id in seen_posts:
                            continue
                        seen_posts.add(external_post_id)
                        post = post_by_external_id[external_post_id]
                        if (
                            _is_coverage_candidate(post)
                            and post.published_at < segment.interval.start_at
                        ):
                            first_earlier = post
                            break
                    if first_earlier is not None:
                        break
                if (
                    first_earlier is None
                    or last.boundary_post_id != first_earlier.external_post_id
                    or last.boundary_post_id not in last.visible_post_ids
                    or first_earlier.segment_id != segment.segment_id
                ):
                    raise CoverageUnproven(
                        "Lower-bound checkpoint does not identify the first earlier non-pinned post."
                    )
            elif outcome_completion_reason == "end_of_history":
                if not last.reached_end:
                    raise CoverageUnproven(
                        "End-of-history checkpoint did not reach the remote end."
                    )
                if last.boundary_post_id is not None:
                    raise CoverageUnproven(
                        "End-of-history checkpoint must not claim a boundary post."
                    )
            else:
                raise CoverageUnproven("Complete result has no supported completion reason.")


def _validate_snapshot(
    snapshot: CollectionTargetSnapshot,
) -> tuple[datetime, datetime | None]:
    if not isinstance(snapshot, CollectionTargetSnapshot):
        raise CollectionManifestInvalid("Collection target snapshot has an unsupported type.")
    _require_canonical_uuid(snapshot.job_id, "Snapshot job ID")
    _require_positive_int(snapshot.handoff_version, "Snapshot handoff version")
    _require_text(snapshot.collector_id, "Snapshot collector ID")
    _require_canonical_uuid(snapshot.person_id, "Snapshot person ID")
    _require_canonical_uuid(snapshot.account_id, "Snapshot account ID")
    if snapshot.platform != "xueqiu":
        raise CollectionManifestInvalid("Snapshot platform is unsupported.")
    _require_digits(snapshot.external_user_id, "Snapshot external user ID")
    if snapshot.mode not in _MODES:
        raise CollectionManifestInvalid("Snapshot collection mode is unsupported.")
    if not isinstance(snapshot.requested_interval, UtcInterval):
        raise CollectionManifestInvalid("Snapshot requested interval is invalid.")
    if not isinstance(snapshot.segments, tuple):
        raise CollectionManifestInvalid("Snapshot segments must be a tuple.")
    seen_segments: set[str] = set()
    for segment in snapshot.segments:
        if not isinstance(segment, ResultSegmentTarget):
            raise CollectionManifestInvalid("Snapshot segment has an unsupported type.")
        _require_canonical_uuid(segment.segment_id, "Snapshot segment ID")
        if segment.segment_id in seen_segments:
            raise CollectionManifestInvalid("Snapshot segment IDs must be unique.")
        seen_segments.add(segment.segment_id)
        _require_nonnegative_int(segment.ordinal, "Snapshot segment ordinal")
        if not isinstance(segment.interval, UtcInterval):
            raise CollectionManifestInvalid("Snapshot segment interval is invalid.")
    heartbeat = _normalize_datetime(
        snapshot.last_heartbeat_at, "Snapshot last heartbeat", optional=True
    )
    _require_nonnegative_int(
        snapshot.stored_remote_action_count, "Stored remote action count"
    )
    if not isinstance(snapshot.known_external_post_ids, frozenset):
        raise CollectionManifestInvalid("Known external post IDs must be a frozenset.")
    for external_post_id in snapshot.known_external_post_ids:
        _require_digits(external_post_id, "Known external post ID")
    if snapshot.resume_checkpoint_id is not None:
        _require_text(snapshot.resume_checkpoint_id, "Resume checkpoint ID")
    now = _normalize_datetime(snapshot.now, "Snapshot current time", optional=False)
    if heartbeat is not None and heartbeat > now + _FUTURE_GRACE:
        raise CollectionManifestInvalid("Snapshot heartbeat is too far in the future.")
    return now, heartbeat


def _parse_interval(value: Any, label: str) -> UtcInterval:
    interval = _require_mapping(value, label)
    _expect_keys(interval, _INTERVAL_KEYS, label)
    start_at = _parse_time(interval.get("start_at"), f"{label} start")
    end_at = _parse_time(interval.get("end_at"), f"{label} end")
    try:
        return UtcInterval(start_at, end_at)
    except (TypeError, ValueError) as error:
        raise CollectionManifestInvalid(f"{label} bounds are invalid.") from error


def _parse_time(value: Any, label: str) -> datetime:
    text = _require_text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise CollectionManifestInvalid(f"{label} must be an ISO-8601 datetime.") from error
    return _normalize_datetime(parsed, label, optional=False)


def _parse_optional_time(value: Any, label: str) -> datetime | None:
    if value is None:
        return None
    return _parse_time(value, label)


def _normalize_datetime(
    value: datetime | None, label: str, *, optional: bool
) -> datetime | None:
    if value is None:
        if optional:
            return None
        raise CollectionManifestInvalid(f"{label} is required.")
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise CollectionManifestInvalid(f"{label} must include a timezone.")
    try:
        return value.astimezone(_UTC)
    except (OverflowError, ValueError) as error:
        raise CollectionManifestInvalid(f"{label} is invalid.") from error


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise CollectionManifestInvalid(f"{label} must be a boolean.")
    return value


def _require_string_tuple(
    value: Any, label: str, *, allow_empty: bool
) -> tuple[str, ...]:
    items = _require_list(value, label)
    if not allow_empty and not items:
        raise CollectionManifestInvalid(f"{label} must not be empty.")
    result = tuple(_require_text(item, label) for item in items)
    if len(set(result)) != len(result):
        raise CollectionManifestInvalid(f"{label} must not contain duplicates.")
    return result


def _require_digit_tuple(
    value: Any, label: str, *, allow_empty: bool
) -> tuple[str, ...]:
    items = _require_list(value, label)
    if not allow_empty and not items:
        raise CollectionManifestInvalid(f"{label} must not be empty.")
    result = tuple(_require_digits(item, label) for item in items)
    if len(set(result)) != len(result):
        raise CollectionManifestInvalid(f"{label} must not contain duplicates.")
    return result


def _parse_optional_digits(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _require_digits(value, label)


def _validate_evidence_references(
    evidence_keys: tuple[str, ...],
    *,
    segment_id: str,
    screenshot_by_key: Mapping[str, ValidatedArtifact],
) -> None:
    for evidence_key in evidence_keys:
        artifact = screenshot_by_key.get(evidence_key)
        if artifact is None:
            raise CollectionManifestInvalid("Record references unknown screenshot evidence.")
        if artifact.segment_id != segment_id:
            raise CollectionManifestInvalid(
                "Screenshot evidence belongs to another segment."
            )


def _is_coverage_candidate(post: _ValidatedPost) -> bool:
    return post.observation_status == "available" and not post.is_pinned


def _raise_proof_or_invalid(outcome_kind: str, message: str) -> None:
    if outcome_kind == "complete":
        raise CoverageUnproven(message)
    raise CollectionManifestInvalid(message)


def _checkpoint_has_artifact_purpose(
    checkpoint: ValidatedCheckpoint,
    *,
    purpose: str,
    screenshot_by_key: Mapping[str, ValidatedArtifact],
) -> bool:
    return any(
        screenshot_by_key[key].purpose == purpose
        and screenshot_by_key[key].segment_id == checkpoint.segment_id
        for key in checkpoint.evidence_keys
    )


def _canonical_accepted_manifest(
    manifest: Mapping[str, Any],
    *,
    started_at: datetime,
    finished_at: datetime,
    last_heartbeat_at: datetime | None,
    requested_interval: UtcInterval,
    segments: tuple[_ManifestSegment, ...],
) -> Mapping[str, Any]:
    canonical = _thaw_json(manifest)
    canonical["target"]["requested_interval"] = {
        "start_at": serialize_utc(requested_interval.start_at),
        "end_at": serialize_utc(requested_interval.end_at),
    }
    canonical["execution"]["started_at"] = serialize_utc(started_at)
    canonical["execution"]["finished_at"] = serialize_utc(finished_at)
    canonical["execution"]["last_heartbeat_at"] = (
        serialize_utc(last_heartbeat_at) if last_heartbeat_at is not None else None
    )
    for raw_segment, segment in zip(canonical["segments"], segments, strict=True):
        raw_segment["interval"] = {
            "start_at": serialize_utc(segment.interval.start_at),
            "end_at": serialize_utc(segment.interval.end_at),
        }
    return _freeze_json(canonical)
