from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import stat
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, Mapping

from .collection_results import ValidatedArtifact


_MEDIA_EXTENSIONS = MappingProxyType(
    {
        "application/json": ".json",
        "application/x-ndjson": ".jsonl",
        "image/png": ".png",
        "image/jpeg": ".jpg",
    }
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_COPY_CHUNK_SIZE = 1024 * 1024
_PRESERVE_LOCK = threading.RLock()


class EvidenceStoreError(Exception):
    """Base class for stable evidence-store failures."""


class EvidenceSourceChanged(EvidenceStoreError):
    """A validated source no longer matches its validated artifact."""


class EvidenceContentConflict(EvidenceStoreError):
    """Existing content-addressed evidence conflicts with the artifact."""


class UnsupportedEvidenceMediaType(EvidenceStoreError):
    """The evidence media type has no stable CAS extension."""


@dataclass(frozen=True)
class StoredEvidence:
    evidence_key: str
    sha256: str
    media_type: str
    byte_size: int
    relative_path: str


@dataclass(frozen=True)
class _PreparedArtifact:
    evidence_key: str
    source_path: Path
    sha256: str
    byte_size: int
    media_type: str
    extension: str
    relative_path: str


class EvidenceStore:
    def __init__(self, data_dir: str | Path) -> None:
        try:
            self.data_dir = Path(data_dir).absolute()
        except (OSError, TypeError, ValueError):
            raise EvidenceStoreError(
                "The evidence data directory is invalid."
            ) from None

    def preserve(
        self, artifacts: tuple[ValidatedArtifact, ...]
    ) -> Mapping[str, StoredEvidence]:
        try:
            prepared = _prepare_artifacts(artifacts)
            if not prepared:
                return MappingProxyType({})
            with _PRESERVE_LOCK:
                return self._preserve_locked(prepared)
        except EvidenceStoreError:
            raise
        except (AttributeError, OSError, TypeError, ValueError):
            raise EvidenceStoreError(
                "Evidence artifacts could not be preserved safely."
            ) from None

    def _preserve_locked(
        self, artifacts: tuple[_PreparedArtifact, ...]
    ) -> Mapping[str, StoredEvidence]:
        stored: dict[str, StoredEvidence] = {}
        for artifact in artifacts:
            target = self._preserve_one(artifact)
            stored[artifact.evidence_key] = StoredEvidence(
                evidence_key=artifact.evidence_key,
                sha256=artifact.sha256,
                media_type=artifact.media_type,
                byte_size=artifact.byte_size,
                relative_path=artifact.relative_path,
            )
            _require_target_path(target, self.data_dir)
        return MappingProxyType(stored)

    def _preserve_one(self, artifact: _PreparedArtifact) -> Path:
        target = self.data_dir / Path(artifact.relative_path)
        _ensure_safe_directory_chain(target.parent)
        _require_target_path(target, self.data_dir)
        message = "The evidence target directory is unavailable or unsafe."
        with _TrustedDirectory(
            target.parent,
            EvidenceStoreError,
            message,
        ) as directory:
            _reject_other_media_targets(target.parent, artifact)
            directory.verify_path()
            target_name = target.name
            sidecar_name = f"{artifact.sha256}.meta"
            if directory.lstat(target_name) is not None:
                _reject_other_media_targets_trusted(directory, artifact)
                _verify_existing_target(directory, target_name, artifact)
                _verify_source(artifact)
                _claim_digest_metadata(directory, artifact)
                _verify_canonical_evidence(
                    directory,
                    target_name,
                    sidecar_name,
                    artifact,
                )
                return target

            temporary_name = _copy_source_to_unique_temporary(
                directory, target_name, artifact
            )
            directory.verify_path()
            _reject_other_media_targets_trusted(directory, artifact)
            _claim_digest_metadata(directory, artifact)
            _reject_other_media_targets_trusted(directory, artifact)
            _verify_existing_target(directory, temporary_name, artifact)
            if directory.lstat(target_name) is not None:
                _verify_canonical_evidence(
                    directory,
                    target_name,
                    sidecar_name,
                    artifact,
                )
                return target

            try:
                directory.publish_noreplace(temporary_name, target_name)
            except FileExistsError:
                _verify_existing_target(directory, target_name, artifact)
            except OSError:
                if directory.lstat(target_name) is not None:
                    _verify_existing_target(directory, target_name, artifact)
                else:
                    raise EvidenceStoreError(
                        "Evidence could not be published safely."
                    ) from None
            _verify_canonical_evidence(
                directory,
                target_name,
                sidecar_name,
                artifact,
            )
        return target


def _verify_canonical_evidence(
    directory: _TrustedDirectory,
    target_name: str,
    sidecar_name: str,
    artifact: _PreparedArtifact,
) -> None:
    # The filesystem is not transactional; perform one final stable read of each
    # source, target, and sidecar before return without promising protection from
    # a writer that mutates an object after its final verification.
    _verify_source(artifact)
    _verify_existing_target(directory, target_name, artifact)
    _verify_digest_metadata(directory, sidecar_name, artifact)
    _reject_other_media_targets_trusted(directory, artifact)
    directory.verify_path()


def _prepare_artifacts(
    artifacts: tuple[ValidatedArtifact, ...],
) -> tuple[_PreparedArtifact, ...]:
    try:
        iterator = iter(artifacts)
    except TypeError:
        raise EvidenceStoreError("Evidence artifacts are invalid.") from None

    prepared: list[_PreparedArtifact] = []
    evidence_keys: set[str] = set()
    digest_metadata: dict[str, tuple[str, int]] = {}
    for artifact in iterator:
        try:
            evidence_key = artifact.evidence_key
            digest = artifact.sha256
            byte_size = artifact.byte_size
            media_type = artifact.media_type
            source_path = Path(artifact.source_path).absolute()
        except (AttributeError, OSError, TypeError, ValueError):
            raise EvidenceStoreError("An evidence artifact is invalid.") from None

        if not isinstance(evidence_key, str) or not evidence_key.strip():
            raise EvidenceStoreError("Evidence keys must be non-empty strings.")
        if evidence_key in evidence_keys:
            raise EvidenceStoreError("Evidence keys must be unique.")
        evidence_keys.add(evidence_key)

        if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
            raise EvidenceStoreError("Evidence SHA-256 values must be lowercase hex.")
        if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 0:
            raise EvidenceStoreError("Evidence byte sizes must be non-negative integers.")
        if not isinstance(media_type, str) or media_type not in _MEDIA_EXTENSIONS:
            raise UnsupportedEvidenceMediaType(
                "The evidence media type is unsupported."
            )

        metadata = (media_type, byte_size)
        previous_metadata = digest_metadata.get(digest)
        if previous_metadata is not None and previous_metadata != metadata:
            raise EvidenceContentConflict(
                "One SHA-256 cannot identify conflicting evidence metadata."
            )
        digest_metadata[digest] = metadata

        extension = _MEDIA_EXTENSIONS[media_type]
        relative_path = (
            f"evidence/sha256/{digest[:2]}/{digest}{extension}"
        )
        prepared.append(
            _PreparedArtifact(
                evidence_key=evidence_key,
                source_path=source_path,
                sha256=digest,
                byte_size=byte_size,
                media_type=media_type,
                extension=extension,
                relative_path=relative_path,
            )
        )
    return tuple(prepared)


def _absolute_chain(path: Path) -> tuple[Path, ...]:
    absolute = path.absolute()
    if not absolute.anchor:
        raise EvidenceStoreError("An evidence path is invalid.")
    current = Path(absolute.anchor)
    chain = [current]
    for part in absolute.parts[1:]:
        current /= part
        chain.append(current)
    return tuple(chain)


def _is_link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _ensure_safe_directory_chain(path: Path) -> None:
    message = "The evidence directory is unavailable or unsafe."
    if os.name == "nt":
        handles = _open_windows_directory_chain(
            path,
            EvidenceStoreError,
            message,
            create=True,
        )
        _close_windows_handles(handles)
        return
    handles, names = _open_posix_directory_chain(
        path,
        EvidenceStoreError,
        message,
        create=True,
    )
    try:
        _verify_posix_directory_chain(
            handles,
            names,
            EvidenceStoreError,
            message,
        )
    finally:
        _close_posix_handles(handles)


def _require_safe_source_parent(path: Path) -> None:
    try:
        chain = _absolute_chain(path)
    except EvidenceStoreError:
        raise EvidenceSourceChanged(
            "The evidence source is unavailable or unsafe."
        ) from None
    for directory in chain:
        try:
            info = os.lstat(directory)
        except (OSError, TypeError, ValueError):
            raise EvidenceSourceChanged(
                "The evidence source is unavailable or unsafe."
            ) from None
        if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise EvidenceSourceChanged(
                "Evidence source paths must not contain links or reparse points."
            )


def _close_posix_handles(handles: list[int]) -> None:
    for descriptor in reversed(handles):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _fdopen_owned(descriptor: int, mode: str) -> BinaryIO:
    try:
        return os.fdopen(descriptor, mode)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _posix_directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _verify_posix_directory_chain(
    handles: list[int],
    names: tuple[str, ...],
    error_type: type[EvidenceStoreError],
    message: str,
) -> None:
    if len(handles) != len(names) + 1:
        raise error_type(message)
    try:
        root_info = os.fstat(handles[0])
        if _is_link_or_reparse(root_info) or not stat.S_ISDIR(root_info.st_mode):
            raise error_type(message)
        for index, name in enumerate(names):
            entry = os.stat(
                name,
                dir_fd=handles[index],
                follow_symlinks=False,
            )
            opened = os.fstat(handles[index + 1])
            if (
                _is_link_or_reparse(entry)
                or not stat.S_ISDIR(entry.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or _identity(entry) != _identity(opened)
            ):
                raise error_type(message)
    except EvidenceStoreError:
        raise
    except (OSError, TypeError, ValueError):
        raise error_type(message) from None


def _open_posix_directory_chain(
    path: Path,
    error_type: type[EvidenceStoreError],
    message: str,
    *,
    create: bool,
) -> tuple[list[int], tuple[str, ...]]:
    if (
        os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or (create and os.mkdir not in os.supports_dir_fd)
    ):
        raise error_type(message)
    try:
        absolute = path.absolute()
        chain = _absolute_chain(absolute)
        root_descriptor = os.open(chain[0], _posix_directory_flags())
    except EvidenceStoreError:
        raise
    except (OSError, TypeError, ValueError):
        raise error_type(message) from None

    handles = [root_descriptor]
    names: list[str] = []
    try:
        for name in absolute.parts[1:]:
            _require_basename(name, error_type, message)
            try:
                entry = os.stat(
                    name,
                    dir_fd=handles[-1],
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if not create:
                    raise error_type(message) from None
                try:
                    os.mkdir(name, dir_fd=handles[-1])
                except FileExistsError:
                    pass
                entry = os.stat(
                    name,
                    dir_fd=handles[-1],
                    follow_symlinks=False,
                )
            if _is_link_or_reparse(entry) or not stat.S_ISDIR(entry.st_mode):
                raise error_type(message)
            descriptor = os.open(
                name,
                _posix_directory_flags(),
                dir_fd=handles[-1],
            )
            handles.append(descriptor)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or _identity(entry) != _identity(opened)
            ):
                raise error_type(message)
            names.append(name)
        result_names = tuple(names)
        _verify_posix_directory_chain(
            handles,
            result_names,
            error_type,
            message,
        )
        return handles, result_names
    except EvidenceStoreError:
        _close_posix_handles(handles)
        raise
    except (OSError, TypeError, ValueError):
        _close_posix_handles(handles)
        raise error_type(message) from None


def _close_windows_handles(handles: list[int]) -> None:
    if os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    for handle in reversed(handles):
        close_handle(ctypes.c_void_p(handle))


def _open_windows_directory_chain(
    path: Path,
    error_type: type[EvidenceStoreError],
    message: str,
    *,
    create: bool = False,
) -> list[int]:
    handles: list[int] = []
    try:
        for directory in _absolute_chain(path):
            try:
                info = os.lstat(directory)
            except FileNotFoundError:
                if not create:
                    raise error_type(message) from None
                try:
                    os.mkdir(directory)
                except FileExistsError:
                    pass
                info = os.lstat(directory)
            if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise error_type(message)
            handles.append(
                _open_windows_directory_handle(
                    directory,
                    info,
                    error_type,
                    message,
                )
            )
    except EvidenceStoreError:
        _close_windows_handles(handles)
        raise
    except (OSError, TypeError, ValueError):
        _close_windows_handles(handles)
        raise error_type(message) from None
    return handles


def _open_windows_directory_handle(
    directory: Path,
    before: os.stat_result,
    error_type: type[EvidenceStoreError],
    message: str,
) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    invalid_handle = ctypes.c_void_p(-1).value
    handle: int | None = None
    try:
        raw_handle = create_file(
            str(directory),
            0x80000000,
            0x00000001 | 0x00000002,
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        if raw_handle is None or raw_handle == invalid_handle:
            raise OSError(ctypes.get_last_error(), "directory handle unavailable")
        handle = int(raw_handle)
        if _windows_handle_attributes(handle) & _REPARSE_POINT:
            raise error_type(message)
        after = os.lstat(directory)
        if (
            _is_link_or_reparse(after)
            or not stat.S_ISDIR(after.st_mode)
            or _identity(before) != _identity(after)
        ):
            raise error_type(message)
        return handle
    except EvidenceStoreError:
        if handle is not None:
            _close_windows_handles([handle])
        raise
    except (OSError, TypeError, ValueError):
        if handle is not None:
            _close_windows_handles([handle])
        raise error_type(message) from None


def _windows_handle_attributes(handle: int) -> int:
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    get_information.restype = ctypes.c_int
    information = _ByHandleFileInformation()
    if not get_information(ctypes.c_void_p(handle), ctypes.byref(information)):
        raise OSError(ctypes.get_last_error(), "file information unavailable")
    return int(information.dwFileAttributes)


def _windows_open_file_read(path: Path) -> BinaryIO:
    from ctypes import wintypes
    import msvcrt

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    get_information.restype = ctypes.c_int
    invalid_handle = ctypes.c_void_p(-1).value
    handle = create_file(
        str(path),
        0x80000000,
        0x00000001,
        None,
        3,
        0x00200000 | 0x08000000,
        None,
    )
    if handle is None or handle == invalid_handle:
        raise OSError(ctypes.get_last_error(), "file handle unavailable")
    information = _ByHandleFileInformation()
    if not get_information(ctypes.c_void_p(handle), ctypes.byref(information)):
        error_number = ctypes.get_last_error()
        close_handle(ctypes.c_void_p(handle))
        raise OSError(error_number, "file information unavailable")
    if information.dwFileAttributes & _REPARSE_POINT:
        close_handle(ctypes.c_void_p(handle))
        raise OSError(errno.ELOOP, "reparse points are not allowed")
    try:
        descriptor = msvcrt.open_osfhandle(
            int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
    except (OSError, ValueError):
        close_handle(ctypes.c_void_p(handle))
        raise
    return _fdopen_owned(descriptor, "rb")


class _TrustedDirectory:
    def __init__(
        self,
        path: Path,
        error_type: type[EvidenceStoreError],
        message: str,
    ) -> None:
        self.path = path.absolute()
        self.error_type = error_type
        self.message = message
        self.identity: tuple[int, int] | None = None
        self.posix_handles: list[int] = []
        self.posix_names: tuple[str, ...] = ()
        self.windows_handles: list[int] = []

    def __enter__(self) -> _TrustedDirectory:
        try:
            if os.name == "nt":
                self.windows_handles = _open_windows_directory_chain(
                    self.path, self.error_type, self.message
                )
                opened = os.lstat(self.path)
            else:
                self.posix_handles, self.posix_names = (
                    _open_posix_directory_chain(
                        self.path,
                        self.error_type,
                        self.message,
                        create=False,
                    )
                )
                opened = os.fstat(self.posix_handles[-1])
            if (
                _is_link_or_reparse(opened)
                or not stat.S_ISDIR(opened.st_mode)
            ):
                raise self.error_type(self.message)
            self.identity = _identity(opened)
            self.verify_path()
            return self
        except EvidenceStoreError:
            self._close()
            raise
        except (OSError, TypeError, ValueError):
            self._close()
            raise self.error_type(self.message) from None

    def __exit__(self, *_args: object) -> None:
        self._close()

    def _close(self) -> None:
        if self.posix_handles:
            _close_posix_handles(self.posix_handles)
            self.posix_handles = []
            self.posix_names = ()
        if self.windows_handles:
            _close_windows_handles(self.windows_handles)
            self.windows_handles = []

    def verify_path(self) -> None:
        if self.posix_handles:
            _verify_posix_directory_chain(
                self.posix_handles,
                self.posix_names,
                self.error_type,
                self.message,
            )
            opened = os.fstat(self.posix_handles[-1])
            if self.identity is None or _identity(opened) != self.identity:
                raise self.error_type(self.message)
            return
        try:
            info = os.lstat(self.path)
        except (OSError, TypeError, ValueError):
            raise self.error_type(self.message) from None
        if (
            self.identity is None
            or _is_link_or_reparse(info)
            or not stat.S_ISDIR(info.st_mode)
            or _identity(info) != self.identity
        ):
            raise self.error_type(self.message)

    def lstat(
        self,
        name: str,
        error_type: type[EvidenceStoreError] | None = None,
        message: str | None = None,
    ) -> os.stat_result | None:
        selected_error = error_type or self.error_type
        selected_message = message or self.message
        _require_basename(name, selected_error, selected_message)
        try:
            if self.posix_handles:
                return os.stat(
                    name,
                    dir_fd=self.posix_handles[-1],
                    follow_symlinks=False,
                )
            return os.lstat(self.path / name)
        except FileNotFoundError:
            return None
        except (OSError, TypeError, ValueError):
            raise selected_error(selected_message) from None

    def open_read(
        self,
        name: str,
        error_type: type[EvidenceStoreError] | None = None,
        message: str | None = None,
    ) -> BinaryIO:
        selected_error = error_type or self.error_type
        selected_message = message or self.message
        _require_basename(name, selected_error, selected_message)
        try:
            if self.posix_handles:
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                )
                descriptor = os.open(
                    name,
                    flags,
                    dir_fd=self.posix_handles[-1],
                )
                return _fdopen_owned(descriptor, "rb")
            return _windows_open_file_read(self.path / name)
        except (OSError, TypeError, ValueError):
            raise selected_error(selected_message) from None

    def open_exclusive(self, name: str) -> BinaryIO:
        _require_basename(name, self.error_type, self.message)
        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_BINARY", 0)
            )
            if self.posix_handles:
                descriptor = os.open(
                    name,
                    flags,
                    0o600,
                    dir_fd=self.posix_handles[-1],
                )
            else:
                descriptor = os.open(self.path / name, flags, 0o600)
            return _fdopen_owned(descriptor, "wb")
        except (OSError, TypeError, ValueError):
            raise self.error_type(self.message) from None

    def publish_noreplace(self, source_name: str, target_name: str) -> None:
        _require_basename(source_name, self.error_type, self.message)
        _require_basename(target_name, self.error_type, self.message)
        self.verify_path()
        if not self.posix_handles:
            _rename_noreplace(
                self.path / source_name,
                self.path / target_name,
            )
        else:
            _rename_noreplace_at(
                self.posix_handles[-1],
                source_name,
                target_name,
            )
        self.verify_path()


def _require_basename(
    name: str,
    error_type: type[EvidenceStoreError],
    message: str,
) -> None:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise error_type(message)


def _require_target_path(target: Path, data_dir: Path) -> None:
    try:
        resolved_data_dir = data_dir.resolve(strict=True)
        resolved_parent = target.parent.resolve(strict=True)
        resolved_parent.relative_to(resolved_data_dir)
    except (OSError, RuntimeError, ValueError):
        raise EvidenceStoreError(
            "The evidence target must remain inside the data directory."
        ) from None


def _reject_other_media_targets(
    _target_directory: Path, _artifact: _PreparedArtifact
) -> None:
    """Race-test seam; real sibling checks use the trusted directory handle."""


def _reject_other_media_targets_trusted(
    directory: _TrustedDirectory, artifact: _PreparedArtifact
) -> None:
    message = "Existing evidence could not be verified safely."
    for extension in _MEDIA_EXTENSIONS.values():
        if extension == artifact.extension:
            continue
        sibling_name = f"{artifact.sha256}{extension}"
        if directory.lstat(
            sibling_name,
            EvidenceContentConflict,
            message,
        ) is not None:
            raise EvidenceContentConflict(
                "One SHA-256 cannot identify multiple evidence media types."
            )


def _identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _file_signature(info: os.stat_result) -> tuple[int, int, int]:
    mtime_ns = getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))
    ctime_ns = getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000))
    return (info.st_size, mtime_ns, ctime_ns)


def _require_regular_source(info: os.stat_result) -> None:
    if _is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise EvidenceSourceChanged(
            "Evidence sources must be regular files, not links or reparse points."
        )


def _verify_source(
    artifact: _PreparedArtifact, destination: BinaryIO | None = None
) -> None:
    message = "The evidence source is unavailable or unsafe."
    with _TrustedDirectory(
        artifact.source_path.parent,
        EvidenceSourceChanged,
        message,
    ) as directory:
        _require_safe_source_parent(artifact.source_path.parent)
        directory.verify_path()
        before = directory.lstat(artifact.source_path.name)
        if before is None:
            raise EvidenceSourceChanged(message)
        _require_regular_source(before)
        if before.st_size != artifact.byte_size:
            raise EvidenceSourceChanged(
                "Evidence source bytes no longer match the validated artifact."
            )

        digest = hashlib.sha256()
        byte_size = 0
        try:
            with directory.open_read(artifact.source_path.name) as source:
                opened = os.fstat(source.fileno())
                _require_regular_source(opened)
                if _identity(before) != _identity(opened):
                    raise EvidenceSourceChanged(
                        "The evidence source identity changed before it was opened."
                    )
                if opened.st_size != artifact.byte_size:
                    raise EvidenceSourceChanged(
                        "Evidence source bytes no longer match the validated artifact."
                    )

                while True:
                    chunk = source.read(_COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    byte_size += len(chunk)
                    if destination is not None:
                        try:
                            written = destination.write(chunk)
                        except OSError:
                            raise EvidenceStoreError(
                                "Evidence could not be staged safely."
                            ) from None
                        if written != len(chunk):
                            raise EvidenceStoreError(
                                "Evidence could not be staged safely."
                            )

                after_handle = os.fstat(source.fileno())
                after_path = directory.lstat(artifact.source_path.name)
                if after_path is None or not (
                    _identity(opened)
                    == _identity(after_handle)
                    == _identity(after_path)
                ):
                    raise EvidenceSourceChanged(
                        "The evidence source identity changed while it was read."
                    )
                if _file_signature(opened) != _file_signature(after_handle):
                    raise EvidenceSourceChanged(
                        "The evidence source changed while it was read."
                    )
                directory.verify_path()
        except EvidenceStoreError:
            raise
        except (OSError, TypeError, ValueError):
            raise EvidenceSourceChanged(
                "The evidence source could not be read safely."
            ) from None

        if byte_size != artifact.byte_size or digest.hexdigest() != artifact.sha256:
            raise EvidenceSourceChanged(
                "Evidence source bytes no longer match the validated artifact."
            )


def _copy_source_to_unique_temporary(
    directory: _TrustedDirectory,
    target_name: str,
    artifact: _PreparedArtifact,
) -> str:
    temporary_name = f".{target_name}.{uuid.uuid4().hex}.tmp"
    try:
        with directory.open_exclusive(temporary_name) as destination:
            _verify_source(artifact, destination)
            destination.flush()
            os.fsync(destination.fileno())
            directory.verify_path()
    except EvidenceStoreError:
        raise
    except (OSError, TypeError, ValueError):
        raise EvidenceStoreError("Evidence could not be staged safely.") from None
    return temporary_name


def _metadata_bytes(artifact: _PreparedArtifact) -> bytes:
    return (
        f"voicevault-evidence-v1\n{artifact.media_type}\n{artifact.byte_size}\n"
    ).encode("ascii")


def _verify_digest_metadata(
    directory: _TrustedDirectory,
    sidecar_name: str,
    artifact: _PreparedArtifact,
) -> None:
    message = "Evidence metadata could not be verified safely."
    before = directory.lstat(
        sidecar_name,
        EvidenceContentConflict,
        message,
    )
    if before is None or _is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise EvidenceContentConflict(
            "Evidence metadata is missing, unsafe, or conflicting."
        )
    expected = _metadata_bytes(artifact)
    if before.st_size != len(expected):
        raise EvidenceContentConflict("Evidence metadata conflicts with the digest.")
    try:
        with directory.open_read(
            sidecar_name,
            EvidenceContentConflict,
            message,
        ) as source:
            opened = os.fstat(source.fileno())
            if (
                _is_link_or_reparse(opened)
                or not stat.S_ISREG(opened.st_mode)
                or _identity(before) != _identity(opened)
                or _file_signature(before) != _file_signature(opened)
            ):
                raise EvidenceContentConflict(
                    "Evidence metadata changed during verification."
                )
            raw = source.read(len(expected) + 1)
            after_handle = os.fstat(source.fileno())
            after_path = directory.lstat(
                sidecar_name,
                EvidenceContentConflict,
                message,
            )
            if (
                after_path is None
                or _is_link_or_reparse(after_handle)
                or not stat.S_ISREG(after_handle.st_mode)
                or _is_link_or_reparse(after_path)
                or not stat.S_ISREG(after_path.st_mode)
                or not (
                    _identity(opened)
                    == _identity(after_handle)
                    == _identity(after_path)
                )
                or not (
                    _file_signature(opened)
                    == _file_signature(after_handle)
                    == _file_signature(after_path)
                )
            ):
                raise EvidenceContentConflict(
                    "Evidence metadata changed during verification."
                )
            directory.verify_path()
    except EvidenceStoreError:
        raise
    except (OSError, TypeError, ValueError):
        raise EvidenceContentConflict(
            "Evidence metadata could not be verified safely."
        ) from None
    if raw != expected:
        raise EvidenceContentConflict("Evidence metadata conflicts with the digest.")


def _claim_digest_metadata(
    directory: _TrustedDirectory, artifact: _PreparedArtifact
) -> None:
    sidecar_name = f"{artifact.sha256}.meta"
    if directory.lstat(sidecar_name) is not None:
        _verify_digest_metadata(directory, sidecar_name, artifact)
        return

    temporary_name = f".{sidecar_name}.{uuid.uuid4().hex}.tmp"
    try:
        with directory.open_exclusive(temporary_name) as destination:
            destination.write(_metadata_bytes(artifact))
            destination.flush()
            os.fsync(destination.fileno())
            directory.verify_path()
    except (OSError, TypeError, ValueError):
        raise EvidenceStoreError(
            "Evidence metadata could not be staged safely."
        ) from None

    _verify_digest_metadata(directory, temporary_name, artifact)
    try:
        directory.publish_noreplace(temporary_name, sidecar_name)
    except FileExistsError:
        _verify_digest_metadata(directory, sidecar_name, artifact)
    except OSError:
        if directory.lstat(sidecar_name) is not None:
            _verify_digest_metadata(directory, sidecar_name, artifact)
        else:
            raise EvidenceStoreError(
                "Evidence metadata could not be published safely."
            ) from None
    _verify_digest_metadata(directory, sidecar_name, artifact)


def _verify_existing_target(
    directory: _TrustedDirectory,
    target_name: str,
    artifact: _PreparedArtifact,
) -> None:
    message = "Existing evidence could not be verified safely."
    before = directory.lstat(
        target_name,
        EvidenceContentConflict,
        message,
    )
    if before is None:
        raise FileExistsError("The evidence target changed during verification.")
    if _is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise EvidenceContentConflict(
            "Existing evidence must be a regular file."
        )
    if before.st_size != artifact.byte_size:
        raise EvidenceContentConflict(
            "Existing evidence conflicts with its content address."
        )

    digest = hashlib.sha256()
    byte_size = 0
    try:
        with directory.open_read(
            target_name,
            EvidenceContentConflict,
            message,
        ) as source:
            opened = os.fstat(source.fileno())
            if _is_link_or_reparse(opened) or not stat.S_ISREG(opened.st_mode):
                raise EvidenceContentConflict(
                    "Existing evidence must be a regular file."
                )
            if _identity(before) != _identity(opened):
                raise EvidenceContentConflict(
                    "Existing evidence changed during verification."
                )
            while True:
                chunk = source.read(_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                byte_size += len(chunk)
            after_handle = os.fstat(source.fileno())
            after_path = directory.lstat(
                target_name,
                EvidenceContentConflict,
                message,
            )
            if after_path is None or not (
                _identity(opened) == _identity(after_handle) == _identity(after_path)
            ):
                raise EvidenceContentConflict(
                    "Existing evidence changed during verification."
                )
            directory.verify_path()
            if _file_signature(opened) != _file_signature(after_handle):
                raise EvidenceContentConflict(
                    "Existing evidence changed during verification."
                )
    except EvidenceStoreError:
        raise
    except (OSError, TypeError, ValueError):
        raise EvidenceContentConflict(
            "Existing evidence could not be verified safely."
        ) from None

    if byte_size != artifact.byte_size or digest.hexdigest() != artifact.sha256:
        raise EvidenceContentConflict(
            "Existing evidence conflicts with its content address."
        )


def _rename_noreplace(source: Path, target: Path) -> None:
    if os.name != "nt":
        raise OSError(errno.ENOTSUP, "absolute no-replace publish is unavailable")
    os.rename(source, target)


def _rename_noreplace_at(
    directory_fd: int, source_name: str, target_name: str
) -> None:
    if sys.platform.startswith("linux") and _linux_rename_noreplace_at(
        directory_fd, source_name, target_name
    ):
        return
    if sys.platform == "darwin" and _darwin_rename_noreplace_at(
        directory_fd, source_name, target_name
    ):
        return
    os.link(
        source_name,
        target_name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
        follow_symlinks=False,
    )


def _linux_rename_noreplace_at(
    directory_fd: int, source_name: str, target_name: str
) -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        return False
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        directory_fd,
        os.fsencode(source_name),
        directory_fd,
        os.fsencode(target_name),
        1,
    )
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    unsupported = {errno.ENOSYS, errno.EINVAL}
    if hasattr(errno, "ENOTSUP"):
        unsupported.add(errno.ENOTSUP)
    if error_number in unsupported:
        return False
    raise OSError(error_number, os.strerror(error_number))


def _darwin_rename_noreplace_at(
    directory_fd: int, source_name: str, target_name: str
) -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = getattr(libc, "renameatx_np", None)
    if renameatx_np is None:
        return False
    renameatx_np.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameatx_np.restype = ctypes.c_int
    result = renameatx_np(
        directory_fd,
        os.fsencode(source_name),
        directory_fd,
        os.fsencode(target_name),
        0x00000004,
    )
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number in {errno.ENOSYS, errno.EINVAL}:
        return False
    raise OSError(error_number, os.strerror(error_number))
