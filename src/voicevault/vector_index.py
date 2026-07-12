from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from .embedding import EmbeddingBatch


_SCHEMA_VERSION = "voicevault-vector-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_MANIFEST_KEYS = {
    "schema_version",
    "generation_id",
    "person_id",
    "model",
    "dimension",
    "count",
    "chunk_ids",
    "vector_sha256",
    "provider_fingerprint",
    "normalized",
}


class VectorIndexError(Exception):
    """Base class for stable, sanitized vector-index failures."""


class VectorShardNotFound(VectorIndexError):
    """The selected person shard is not present."""


class VectorIndexInvalid(VectorIndexError):
    """A vector shard or its manifest is unsafe, incomplete, or corrupt."""


class VectorIndexConflict(VectorIndexError):
    """Published shard files conflict with the requested immutable content."""


@dataclass(frozen=True)
class VectorHit:
    chunk_id: str
    rank: int
    similarity: float


@dataclass(frozen=True)
class VectorShard:
    generation_id: str
    person_id: str
    model: str
    dimension: int
    count: int
    chunk_ids: tuple[str, ...]
    vector_sha256: str
    provider_fingerprint: str
    normalized: bool


class VectorIndexProvider(Protocol):
    def build_person_shard(
        self,
        generation_id: str,
        person_id: str,
        chunk_ids: tuple[str, ...],
        embeddings: EmbeddingBatch,
    ) -> str:
        ...

    def load_person_shard(self, generation_id: str, person_id: str) -> VectorShard:
        ...

    def read_person_vectors(
        self,
        generation_id: str,
        person_id: str,
        chunk_ids: tuple[str, ...],
    ) -> tuple[tuple[float, ...], ...]:
        ...

    def search_person(
        self,
        generation_id: str,
        person_id: str,
        query_vector: tuple[float, ...],
        limit: int,
        allowed_chunk_ids: tuple[str, ...] | None = None,
    ) -> tuple[VectorHit, ...]:
        ...


class LocalVectorIndexProvider:
    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        try:
            self.data_dir = Path(data_dir).absolute()
        except (TypeError, ValueError, OSError):
            raise VectorIndexInvalid("Vector data directory is invalid.") from None

    def build_person_shard(
        self,
        generation_id: str,
        person_id: str,
        chunk_ids: tuple[str, ...],
        embeddings: EmbeddingBatch,
    ) -> str:
        generation = _canonical_uuid(generation_id, "Generation ID")
        person = _canonical_uuid(person_id, "Person ID")
        chunks = _validate_chunk_ids(chunk_ids)
        if not isinstance(embeddings, EmbeddingBatch):
            raise ValueError("Embeddings must be an EmbeddingBatch.")
        if len(chunks) != len(embeddings.vectors):
            raise ValueError("Chunk and embedding counts must match.")

        matrix = _normalized_matrix(embeddings)
        vector_bytes = matrix.tobytes(order="C")
        vector_sha256 = hashlib.sha256(vector_bytes).hexdigest()
        manifest = {
            "schema_version": _SCHEMA_VERSION,
            "generation_id": generation,
            "person_id": person,
            "model": embeddings.model,
            "dimension": embeddings.dimension,
            "count": len(chunks),
            "chunk_ids": list(chunks),
            "vector_sha256": vector_sha256,
            "provider_fingerprint": embeddings.provider_fingerprint,
            "normalized": True,
        }
        manifest_bytes = _canonical_json(manifest)
        person_dir, vector_path, manifest_path = self._paths(
            generation, person, create=True
        )
        relative_manifest = manifest_path.relative_to(self.data_dir).as_posix()

        vector_state = _lstat(vector_path)
        manifest_state = _lstat(manifest_path)
        if vector_state is not None and manifest_state is not None:
            self._verify_pair(
                vector_path,
                manifest_path,
                expected_manifest=manifest,
            )
            return relative_manifest

        token = uuid.uuid4().hex
        staging_vector = person_dir / f"{person}.{token}.staging.f32"
        staging_manifest = person_dir / f"{person}.{token}.staging.json"
        try:
            _write_staging(staging_vector, vector_bytes)
            _write_staging(staging_manifest, manifest_bytes)
            if vector_state is not None:
                _require_regular_file(vector_path)
                if (
                    vector_path.stat().st_size != len(vector_bytes)
                    or _sha256_file(vector_path) != vector_sha256
                ):
                    raise VectorIndexConflict("Vector shard conflicts with requested content.")
            if manifest_state is not None:
                _require_regular_file(manifest_path)
                existing = _load_manifest_file(manifest_path)
                if existing != manifest:
                    raise VectorIndexConflict("Vector manifest conflicts with requested content.")
            if vector_state is None:
                _publish_no_overwrite(staging_vector, vector_path)
            if manifest_state is None:
                _publish_no_overwrite(staging_manifest, manifest_path)
            _fsync_directory(person_dir)
        except VectorIndexError:
            raise
        except (OSError, ValueError, TypeError):
            raise VectorIndexInvalid("Vector shard publish failed.") from None

        self._verify_pair(vector_path, manifest_path, expected_manifest=manifest)
        return relative_manifest

    def load_person_shard(self, generation_id: str, person_id: str) -> VectorShard:
        generation = _canonical_uuid(generation_id, "Generation ID")
        person = _canonical_uuid(person_id, "Person ID")
        _, vector_path, manifest_path = self._paths(generation, person, create=False)
        vector_state = _lstat(vector_path)
        manifest_state = _lstat(manifest_path)
        if vector_state is None and manifest_state is None:
            raise VectorShardNotFound("Vector shard is not available.")
        if vector_state is None or manifest_state is None:
            raise VectorIndexInvalid("Vector shard is incomplete.")
        manifest = self._verify_pair(
            vector_path,
            manifest_path,
            expected_generation_id=generation,
            expected_person_id=person,
        )
        return _manifest_to_shard(manifest)

    def read_person_vectors(
        self,
        generation_id: str,
        person_id: str,
        chunk_ids: tuple[str, ...],
    ) -> tuple[tuple[float, ...], ...]:
        requested = _validate_chunk_ids(chunk_ids, allow_empty=True)
        shard = self.load_person_shard(generation_id, person_id)
        if not requested:
            return ()
        positions = {chunk_id: index for index, chunk_id in enumerate(shard.chunk_ids)}
        if any(chunk_id not in positions for chunk_id in requested):
            raise VectorIndexInvalid("Requested vector is not present in the shard.")
        _, vector_path, _ = self._paths(shard.generation_id, shard.person_id, create=False)
        matrix = _open_memmap(vector_path, shard)
        try:
            return tuple(
                tuple(float(value) for value in matrix[positions[chunk_id]])
                for chunk_id in requested
            )
        finally:
            _close_memmap(matrix)

    def search_person(
        self,
        generation_id: str,
        person_id: str,
        query_vector: tuple[float, ...],
        limit: int,
        allowed_chunk_ids: tuple[str, ...] | None = None,
    ) -> tuple[VectorHit, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("Vector search limit must be positive.")
        allowed = (
            None
            if allowed_chunk_ids is None
            else frozenset(_validate_chunk_ids(allowed_chunk_ids, allow_empty=True))
        )
        shard = self.load_person_shard(generation_id, person_id)
        query = _normalized_query(query_vector, shard.dimension)
        if shard.count == 0 or allowed == frozenset():
            return ()
        _, vector_path, _ = self._paths(shard.generation_id, shard.person_id, create=False)
        matrix = _open_memmap(vector_path, shard)
        try:
            similarities = np.asarray(matrix @ query, dtype=np.float64)
            candidates = [
                (chunk_id, float(similarities[index]))
                for index, chunk_id in enumerate(shard.chunk_ids)
                if allowed is None or chunk_id in allowed
            ]
        except (OSError, ValueError, TypeError, FloatingPointError):
            raise VectorIndexInvalid("Vector search failed.") from None
        finally:
            _close_memmap(matrix)
        candidates.sort(key=lambda item: (-item[1], item[0]))
        return tuple(
            VectorHit(chunk_id=chunk_id, rank=rank, similarity=similarity)
            for rank, (chunk_id, similarity) in enumerate(candidates[:limit], start=1)
        )

    def _paths(
        self,
        generation_id: str,
        person_id: str,
        *,
        create: bool,
    ) -> tuple[Path, Path, Path]:
        parts = (
            self.data_dir,
            self.data_dir / "indexes",
            self.data_dir / "indexes" / "generations",
            self.data_dir / "indexes" / "generations" / generation_id,
            self.data_dir / "indexes" / "generations" / generation_id / "persons",
        )
        _safe_directory_chain(parts, create=create)
        person_dir = parts[-1]
        return (
            person_dir,
            person_dir / f"{person_id}.f32",
            person_dir / f"{person_id}.json",
        )

    def _verify_pair(
        self,
        vector_path: Path,
        manifest_path: Path,
        *,
        expected_manifest: dict[str, object] | None = None,
        expected_generation_id: str | None = None,
        expected_person_id: str | None = None,
    ) -> dict[str, object]:
        try:
            _require_regular_file(vector_path)
            _require_regular_file(manifest_path)
            manifest = _load_manifest_file(manifest_path)
            _validate_manifest(manifest)
            if (
                expected_generation_id is not None
                and manifest["generation_id"] != expected_generation_id
            ) or (
                expected_person_id is not None
                and manifest["person_id"] != expected_person_id
            ):
                raise VectorIndexInvalid("Vector manifest identity is invalid.")
            expected_size = int(manifest["count"]) * int(manifest["dimension"]) * 4
            if vector_path.stat().st_size != expected_size:
                raise VectorIndexInvalid("Vector shard size is invalid.")
            if _sha256_file(vector_path) != manifest["vector_sha256"]:
                raise VectorIndexInvalid("Vector shard checksum is invalid.")
            if expected_manifest is not None and manifest != expected_manifest:
                raise VectorIndexConflict("Vector shard conflicts with requested content.")
            return manifest
        except VectorIndexError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise VectorIndexInvalid("Vector shard is invalid.") from None


def _normalized_matrix(embeddings: EmbeddingBatch) -> np.ndarray:
    try:
        matrix64 = np.asarray(embeddings.vectors, dtype=np.float64)
        if matrix64.shape != (len(embeddings.vectors), embeddings.dimension):
            raise ValueError
        if not np.isfinite(matrix64).all():
            raise ValueError
        norms = np.linalg.norm(matrix64, axis=1)
        if not np.isfinite(norms).all() or np.any(norms <= 0):
            raise ValueError
        normalized = np.asarray(matrix64 / norms[:, None], dtype="<f4", order="C")
        if not np.isfinite(normalized).all():
            raise ValueError
        return normalized
    except (TypeError, ValueError, FloatingPointError):
        raise ValueError("Embedding vectors must be finite and non-zero.") from None


def _normalized_query(query_vector: tuple[float, ...], dimension: int) -> np.ndarray:
    if not isinstance(query_vector, tuple) or len(query_vector) != dimension:
        raise ValueError("Query vector dimension is invalid.")
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        for value in query_vector
    ):
        raise ValueError("Query vector must contain finite numbers.")
    query = np.asarray(query_vector, dtype=np.float64)
    norm = float(np.linalg.norm(query))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("Query vector must be non-zero.")
    normalized = np.asarray(query / norm, dtype="<f4")
    if not np.isfinite(normalized).all():
        raise ValueError("Query vector must contain finite numbers.")
    return normalized


def _validate_chunk_ids(
    chunk_ids: tuple[str, ...], *, allow_empty: bool = False
) -> tuple[str, ...]:
    if not isinstance(chunk_ids, tuple) or (not chunk_ids and not allow_empty):
        raise ValueError("Chunk IDs must be a non-empty tuple.")
    if any(not isinstance(chunk_id, str) or not chunk_id.strip() for chunk_id in chunk_ids):
        raise ValueError("Chunk IDs must be non-empty strings.")
    if len(set(chunk_ids)) != len(chunk_ids):
        raise ValueError("Chunk IDs must be unique.")
    return chunk_ids


def _canonical_uuid(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical UUID.")
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"{label} must be a canonical UUID.") from None
    if value != canonical:
        raise ValueError(f"{label} must be a canonical UUID.")
    return canonical


def _validate_manifest(manifest: dict[str, object]) -> None:
    if set(manifest) != _MANIFEST_KEYS:
        raise VectorIndexInvalid("Vector manifest schema is invalid.")
    if manifest.get("schema_version") != _SCHEMA_VERSION:
        raise VectorIndexInvalid("Vector manifest schema is invalid.")
    generation = _canonical_uuid(manifest.get("generation_id"), "Generation ID")  # type: ignore[arg-type]
    person = _canonical_uuid(manifest.get("person_id"), "Person ID")  # type: ignore[arg-type]
    if generation != manifest["generation_id"] or person != manifest["person_id"]:
        raise VectorIndexInvalid("Vector manifest identity is invalid.")
    model = manifest.get("model")
    dimension = manifest.get("dimension")
    count = manifest.get("count")
    raw_chunks = manifest.get("chunk_ids")
    fingerprint = manifest.get("provider_fingerprint")
    vector_sha = manifest.get("vector_sha256")
    if not isinstance(model, str) or not model.strip():
        raise VectorIndexInvalid("Vector manifest model is invalid.")
    if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension < 1:
        raise VectorIndexInvalid("Vector manifest dimension is invalid.")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise VectorIndexInvalid("Vector manifest count is invalid.")
    if not isinstance(raw_chunks, list) or len(raw_chunks) != count:
        raise VectorIndexInvalid("Vector manifest chunks are invalid.")
    _validate_chunk_ids(tuple(raw_chunks))
    if not isinstance(fingerprint, str) or _SHA256.fullmatch(fingerprint) is None:
        raise VectorIndexInvalid("Vector manifest fingerprint is invalid.")
    if not isinstance(vector_sha, str) or _SHA256.fullmatch(vector_sha) is None:
        raise VectorIndexInvalid("Vector manifest checksum is invalid.")
    if manifest.get("normalized") is not True:
        raise VectorIndexInvalid("Vector manifest normalization is invalid.")


def _manifest_to_shard(manifest: dict[str, object]) -> VectorShard:
    return VectorShard(
        generation_id=str(manifest["generation_id"]),
        person_id=str(manifest["person_id"]),
        model=str(manifest["model"]),
        dimension=int(manifest["dimension"]),
        count=int(manifest["count"]),
        chunk_ids=tuple(manifest["chunk_ids"]),  # type: ignore[arg-type]
        vector_sha256=str(manifest["vector_sha256"]),
        provider_fingerprint=str(manifest["provider_fingerprint"]),
        normalized=True,
    )


def _open_memmap(vector_path: Path, shard: VectorShard) -> np.memmap:
    try:
        return np.memmap(
            vector_path,
            dtype="<f4",
            mode="r",
            shape=(shard.count, shard.dimension),
            order="C",
        )
    except (OSError, ValueError, TypeError):
        raise VectorIndexInvalid("Vector shard cannot be opened.") from None


def _close_memmap(matrix: np.memmap) -> None:
    mapping = getattr(matrix, "_mmap", None)
    if mapping is not None:
        mapping.close()


def _load_manifest_file(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise VectorIndexInvalid("Vector manifest is invalid.") from None
    if not isinstance(payload, dict):
        raise VectorIndexInvalid("Vector manifest is invalid.")
    return payload


def _canonical_json(value: dict[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _write_staging(path: Path, content: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise VectorIndexInvalid("Vector staging file already exists.") from None


def _publish_no_overwrite(staging: Path, final: Path) -> None:
    try:
        os.link(staging, final)
    except FileExistsError:
        raise VectorIndexConflict("Vector shard was published concurrently.") from None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise VectorIndexInvalid("Vector path is invalid.") from None


def _require_regular_file(path: Path) -> None:
    state = _lstat(path)
    if (
        state is None
        or stat.S_ISLNK(state.st_mode)
        or not stat.S_ISREG(state.st_mode)
        or bool(getattr(state, "st_file_attributes", 0) & _REPARSE_POINT)
    ):
        raise VectorIndexInvalid("Vector file is unsafe.")


def _safe_directory_chain(parts: tuple[Path, ...], *, create: bool) -> None:
    for path in parts:
        state = _lstat(path)
        if state is None and create:
            try:
                path.mkdir()
            except FileExistsError:
                state = _lstat(path)
            except OSError:
                raise VectorIndexInvalid("Vector directory is unavailable.") from None
            else:
                state = _lstat(path)
        if state is None:
            continue
        if (
            stat.S_ISLNK(state.st_mode)
            or not stat.S_ISDIR(state.st_mode)
            or bool(getattr(state, "st_file_attributes", 0) & _REPARSE_POINT)
        ):
            raise VectorIndexInvalid("Vector directory is unsafe.")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
