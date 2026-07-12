from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


_INDEX_SCHEMA_VERSION = "voicevault-fulltext-v1"
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_CHANNELS = ("trigram", "cjk_bigram")


class FullTextError(Exception):
    """Base class for stable full-text index failures."""


class FullTextUnavailable(FullTextError):
    """SQLite FTS5 with the required tokenizer is unavailable."""


class FullTextIndexInvalid(FullTextError):
    """A derived full-text index is missing, unsafe, or corrupt."""


class FullTextConflict(FullTextError):
    """An existing generation conflicts with the requested build."""


@dataclass(frozen=True)
class FullTextDocument:
    chunk_id: str
    person_id: str
    platform: str
    published_at: datetime | None
    text: str


@dataclass(frozen=True)
class FullTextSearchFilters:
    person_ids: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    published_from: datetime | None = None
    published_to: datetime | None = None
    allowed_chunk_ids: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.person_ids, tuple)
            or not isinstance(self.platforms, tuple)
            or (
                self.allowed_chunk_ids is not None
                and not isinstance(self.allowed_chunk_ids, tuple)
            )
        ):
            raise ValueError("full-text filters must use tuples")


@dataclass(frozen=True)
class FullTextHit:
    chunk_id: str
    rank: int
    matched_channels: tuple[str, ...]
    channel_ranks: tuple[tuple[str, int], ...]


class FullTextIndexProvider(Protocol):
    def build(
        self, generation_id: str, documents: tuple[FullTextDocument, ...]
    ) -> str:
        ...

    def search(
        self,
        generation_id: str,
        query: str,
        filters: FullTextSearchFilters,
        limit: int,
    ) -> tuple[FullTextHit, ...]:
        ...


class LocalFullTextIndexProvider:
    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        try:
            self.data_dir = Path(data_dir).absolute()
        except (TypeError, ValueError, OSError):
            raise FullTextIndexInvalid("Full-text data directory is invalid.") from None

    def build(
        self,
        generation_id: str,
        documents: tuple[FullTextDocument, ...],
    ) -> str:
        generation = _canonical_generation_id(generation_id)
        prepared = _prepare_documents(documents)
        fingerprint = _documents_fingerprint(prepared)
        generation_dir, final_path = self._paths(generation, create=True)
        relative_path = final_path.relative_to(self.data_dir).as_posix()
        if _lstat(final_path) is not None:
            _require_regular_final(final_path)
            self._verify_index(
                final_path,
                generation,
                expected_fingerprint=fingerprint,
                expected_count=len(prepared),
            )
            return relative_path

        staging_path = generation_dir / f"fulltext.{uuid.uuid4().hex}.staging.sqlite"
        try:
            self._build_staging(
                staging_path,
                generation_id=generation,
                documents=prepared,
                fingerprint=fingerprint,
            )
            _fsync_file(staging_path)
            _require_regular_file(staging_path)
            try:
                os.link(staging_path, final_path)
            except FileExistsError:
                pass
            except OSError:
                if _lstat(final_path) is None:
                    raise FullTextIndexInvalid("Full-text index could not be published.") from None
            _fsync_directory(generation_dir)
            _require_regular_final(final_path)
            self._verify_index(
                final_path,
                generation,
                expected_fingerprint=fingerprint,
                expected_count=len(prepared),
            )
            return relative_path
        except FullTextError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError):
            raise FullTextIndexInvalid("Full-text index build failed.") from None

    def search(
        self,
        generation_id: str,
        query: str,
        filters: FullTextSearchFilters,
        limit: int,
    ) -> tuple[FullTextHit, ...]:
        generation = _canonical_generation_id(generation_id)
        normalized_query = _validate_query(query)
        validated_filters = _validate_filters(filters)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("Full-text search limit must be positive.")
        _generation_dir, final_path = self._paths(generation, create=False)
        _require_regular_final(final_path)
        self._verify_index(final_path, generation)
        try:
            connection = _connect_readonly(final_path)
            try:
                channel_ranks: dict[str, dict[str, int]] = {}
                if len(normalized_query) >= 3:
                    for rank, chunk_id in enumerate(
                        _allowed_results(
                            _search_trigram(
                                connection, normalized_query, validated_filters
                            ),
                            validated_filters.allowed_chunk_ids,
                        ),
                        1,
                    ):
                        channel_ranks.setdefault(chunk_id, {})["trigram"] = rank
                bigrams = _query_bigrams(normalized_query)
                if bigrams:
                    for rank, chunk_id in enumerate(
                        _allowed_results(
                            _search_bigrams(connection, bigrams, validated_filters),
                            validated_filters.allowed_chunk_ids,
                        ),
                        1,
                    ):
                        channel_ranks.setdefault(chunk_id, {})["cjk_bigram"] = rank
            finally:
                connection.close()
        except FullTextError:
            raise
        except sqlite3.OperationalError as error:
            if _is_fts_unavailable(error):
                raise FullTextUnavailable("Required SQLite FTS5 support is unavailable.") from None
            raise FullTextIndexInvalid("Full-text index search failed.") from None
        except (OSError, sqlite3.Error, TypeError, ValueError):
            raise FullTextIndexInvalid("Full-text index search failed.") from None

        ordered = sorted(
            channel_ranks.items(),
            key=lambda item: (
                -sum(1.0 / (60 + rank) for rank in item[1].values()),
                item[0],
            ),
        )[:limit]
        return tuple(
            FullTextHit(
                chunk_id=chunk_id,
                rank=rank,
                matched_channels=tuple(
                    channel for channel in _CHANNELS if channel in ranks
                ),
                channel_ranks=tuple(
                    (channel, ranks[channel])
                    for channel in _CHANNELS
                    if channel in ranks
                ),
            )
            for rank, (chunk_id, ranks) in enumerate(ordered, 1)
        )

    def _paths(self, generation_id: str, *, create: bool) -> tuple[Path, Path]:
        generation_dir = self.data_dir / "indexes" / "generations" / generation_id
        if create:
            _ensure_safe_directory_chain(generation_dir)
        else:
            _require_safe_directory_chain(generation_dir)
        return generation_dir, generation_dir / "fulltext.sqlite"

    @staticmethod
    def _build_staging(
        path: Path,
        *,
        generation_id: str,
        documents: tuple[FullTextDocument, ...],
        fingerprint: str,
    ) -> None:
        try:
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                "CREATE TABLE metadata(key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                """
                CREATE TABLE documents(
                    chunk_id TEXT NOT NULL PRIMARY KEY,
                    person_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    published_at TEXT,
                    text TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX documents_filters_idx ON documents(person_id, platform, published_at, chunk_id)"
            )
            connection.execute(
                "CREATE VIRTUAL TABLE documents_fts USING fts5(chunk_id UNINDEXED, text, tokenize='trigram')"
            )
            connection.execute(
                """
                CREATE TABLE bigrams(
                    bigram TEXT NOT NULL,
                    chunk_id TEXT NOT NULL REFERENCES documents(chunk_id) ON DELETE CASCADE,
                    position INTEGER NOT NULL CHECK(position >= 0),
                    PRIMARY KEY(bigram, chunk_id, position)
                )
                """
            )
            connection.execute(
                "CREATE INDEX bigrams_chunk_idx ON bigrams(chunk_id, bigram, position)"
            )
            metadata = {
                "schema_version": _INDEX_SCHEMA_VERSION,
                "generation_id": generation_id,
                "document_count": str(len(documents)),
                "documents_sha256": fingerprint,
            }
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)", metadata.items()
            )
            for item in documents:
                published_at = _serialize_optional_utc(item.published_at)
                connection.execute(
                    "INSERT INTO documents VALUES (?, ?, ?, ?, ?)",
                    (
                        item.chunk_id,
                        item.person_id,
                        item.platform,
                        published_at,
                        item.text,
                    ),
                )
                connection.execute(
                    "INSERT INTO documents_fts(chunk_id, text) VALUES (?, ?)",
                    (item.chunk_id, item.text),
                )
                connection.executemany(
                    "INSERT INTO bigrams(bigram, chunk_id, position) VALUES (?, ?, ?)",
                    (
                        (bigram, item.chunk_id, position)
                        for position, bigram in _document_bigrams(item.text)
                    ),
                )
            connection.commit()
        except sqlite3.OperationalError as error:
            if _is_fts_unavailable(error):
                raise FullTextUnavailable("Required SQLite FTS5 support is unavailable.") from None
            raise FullTextIndexInvalid("Full-text index build failed.") from None
        except (sqlite3.Error, OSError, TypeError, ValueError):
            raise FullTextIndexInvalid("Full-text index build failed.") from None
        finally:
            if "connection" in locals():
                connection.close()

    @staticmethod
    def _verify_index(
        path: Path,
        generation_id: str,
        *,
        expected_fingerprint: str | None = None,
        expected_count: int | None = None,
    ) -> None:
        try:
            connection = _connect_readonly(path)
            try:
                quick_check = connection.execute("PRAGMA quick_check").fetchone()
                if quick_check is None or quick_check[0] != "ok":
                    raise FullTextIndexInvalid("Full-text index is corrupt.")
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                    )
                }
                if not {"metadata", "documents", "documents_fts", "bigrams"} <= tables:
                    raise FullTextIndexInvalid("Full-text index schema is invalid.")
                expected_columns = {
                    "metadata": ("key", "value"),
                    "documents": (
                        "chunk_id",
                        "person_id",
                        "platform",
                        "published_at",
                        "text",
                    ),
                    "documents_fts": ("chunk_id", "text"),
                    "bigrams": ("bigram", "chunk_id", "position"),
                }
                for table, columns in expected_columns.items():
                    actual = tuple(
                        row[1]
                        for row in connection.execute(f"PRAGMA table_info('{table}')")
                    )
                    if actual != columns:
                        raise FullTextIndexInvalid("Full-text index schema is invalid.")
                fts_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'documents_fts'"
                ).fetchone()
                if (
                    fts_sql is None
                    or "fts5" not in fts_sql[0].lower()
                    or "trigram" not in fts_sql[0].lower()
                ):
                    raise FullTextIndexInvalid("Full-text index schema is invalid.")
                metadata = dict(connection.execute("SELECT key, value FROM metadata"))
                required = {
                    "schema_version",
                    "generation_id",
                    "document_count",
                    "documents_sha256",
                }
                if set(metadata) != required or (
                    metadata["schema_version"] != _INDEX_SCHEMA_VERSION
                    or metadata["generation_id"] != generation_id
                ):
                    raise FullTextIndexInvalid("Full-text index metadata is invalid.")
                try:
                    document_count = int(metadata["document_count"])
                except ValueError:
                    raise FullTextIndexInvalid("Full-text index metadata is invalid.") from None
                stored_count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
                fts_count = connection.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0]
                if document_count < 0 or stored_count != document_count or fts_count != document_count:
                    raise FullTextIndexInvalid("Full-text index is incomplete.")
                document_rows = connection.execute(
                    """
                    SELECT chunk_id, person_id, platform, published_at, text
                    FROM documents ORDER BY chunk_id
                    """
                ).fetchall()
                documents = tuple(
                    FullTextDocument(
                        chunk_id=row[0],
                        person_id=row[1],
                        platform=row[2],
                        published_at=_parse_optional_utc(row[3]),
                        text=row[4],
                    )
                    for row in document_rows
                )
                computed_fingerprint = _documents_fingerprint(documents)
                if metadata["documents_sha256"] != computed_fingerprint:
                    raise FullTextIndexInvalid("Full-text index content is inconsistent.")
                fts_rows = connection.execute(
                    "SELECT chunk_id, text FROM documents_fts ORDER BY chunk_id"
                ).fetchall()
                if [tuple(row) for row in fts_rows] != [
                    (item.chunk_id, item.text) for item in documents
                ]:
                    raise FullTextIndexInvalid("Full-text index content is inconsistent.")
                expected_bigrams = sorted(
                    (
                        bigram,
                        item.chunk_id,
                        position,
                    )
                    for item in documents
                    for position, bigram in _document_bigrams(item.text)
                )
                actual_bigrams = [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT bigram, chunk_id, position FROM bigrams ORDER BY bigram, chunk_id, position"
                    )
                ]
                if actual_bigrams != expected_bigrams:
                    raise FullTextIndexInvalid("Full-text index content is inconsistent.")
                if expected_count is not None and document_count != expected_count:
                    raise FullTextConflict("Existing full-text generation conflicts with this build.")
                if (
                    expected_fingerprint is not None
                    and metadata["documents_sha256"] != expected_fingerprint
                ):
                    raise FullTextConflict("Existing full-text generation conflicts with this build.")
            finally:
                connection.close()
        except FullTextError:
            raise
        except sqlite3.OperationalError as error:
            if _is_fts_unavailable(error):
                raise FullTextUnavailable("Required SQLite FTS5 support is unavailable.") from None
            raise FullTextIndexInvalid("Full-text index is invalid.") from None
        except (OSError, sqlite3.Error, TypeError, ValueError):
            raise FullTextIndexInvalid("Full-text index is invalid.") from None


def _prepare_documents(
    documents: tuple[FullTextDocument, ...],
) -> tuple[FullTextDocument, ...]:
    if not isinstance(documents, tuple):
        raise ValueError("Full-text documents must be a tuple.")
    prepared: list[FullTextDocument] = []
    seen: set[str] = set()
    for item in documents:
        if not isinstance(item, FullTextDocument):
            raise ValueError("Full-text documents must be FullTextDocument values.")
        for value, label in (
            (item.chunk_id, "chunk_id"),
            (item.person_id, "person_id"),
            (item.platform, "platform"),
            (item.text, "text"),
        ):
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError(f"Full-text document {label} is invalid.")
        _serialize_optional_utc(item.published_at)
        if item.chunk_id in seen:
            raise ValueError("Full-text document chunk IDs must be unique.")
        seen.add(item.chunk_id)
        prepared.append(item)
    return tuple(sorted(prepared, key=lambda item: item.chunk_id))


def _documents_fingerprint(documents: tuple[FullTextDocument, ...]) -> str:
    canonical = json.dumps(
        [
            {
                "chunk_id": item.chunk_id,
                "person_id": item.person_id,
                "platform": item.platform,
                "published_at": _serialize_optional_utc(item.published_at),
                "text": item.text,
            }
            for item in documents
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _search_trigram(
    connection: sqlite3.Connection,
    query: str,
    filters: FullTextSearchFilters,
) -> tuple[str, ...]:
    where, parameters = _filter_sql(filters, alias="d")
    expression = f'"{query.replace(chr(34), chr(34) * 2)}"'
    rows = connection.execute(
        f"""
        SELECT d.chunk_id, bm25(documents_fts) AS channel_score
        FROM documents_fts
        JOIN documents d ON d.chunk_id = documents_fts.chunk_id
        WHERE documents_fts MATCH ? {where}
        ORDER BY channel_score, d.chunk_id
        """,
        (expression, *parameters),
    ).fetchall()
    return tuple(row[0] for row in rows)


def _search_bigrams(
    connection: sqlite3.Connection,
    bigrams: tuple[str, ...],
    filters: FullTextSearchFilters,
) -> tuple[str, ...]:
    where, parameters = _filter_sql(filters, alias="d")
    placeholders = ",".join("?" for _ in bigrams)
    rows = connection.execute(
        f"""
        SELECT d.chunk_id, COUNT(*) AS channel_frequency
        FROM bigrams b
        JOIN documents d ON d.chunk_id = b.chunk_id
        WHERE b.bigram IN ({placeholders}) {where}
        GROUP BY d.chunk_id
        HAVING COUNT(DISTINCT b.bigram) = ?
        ORDER BY channel_frequency DESC, d.chunk_id
        """,
        (*bigrams, *parameters, len(bigrams)),
    ).fetchall()
    return tuple(row[0] for row in rows)


def _filter_sql(
    filters: FullTextSearchFilters,
    *,
    alias: str,
) -> tuple[str, tuple[str, ...]]:
    clauses: list[str] = []
    parameters: list[str] = []
    if filters.person_ids:
        clauses.append(
            f"{alias}.person_id IN ({','.join('?' for _ in filters.person_ids)})"
        )
        parameters.extend(filters.person_ids)
    if filters.platforms:
        clauses.append(
            f"{alias}.platform IN ({','.join('?' for _ in filters.platforms)})"
        )
        parameters.extend(filters.platforms)
    if filters.published_from is not None:
        clauses.append(f"{alias}.published_at >= ?")
        parameters.append(_serialize_utc(filters.published_from))
    if filters.published_to is not None:
        clauses.append(f"{alias}.published_at < ?")
        parameters.append(_serialize_utc(filters.published_to))
    return (" AND " + " AND ".join(clauses) if clauses else "", tuple(parameters))


def _allowed_results(
    chunk_ids: tuple[str, ...],
    allowed_chunk_ids: tuple[str, ...] | None,
) -> tuple[str, ...]:
    if allowed_chunk_ids is None:
        return chunk_ids
    allowed = frozenset(allowed_chunk_ids)
    return tuple(chunk_id for chunk_id in chunk_ids if chunk_id in allowed)


def _validate_filters(filters: FullTextSearchFilters) -> FullTextSearchFilters:
    if not isinstance(filters, FullTextSearchFilters):
        raise ValueError("Full-text filters are invalid.")
    for values in (
        filters.person_ids,
        filters.platforms,
        filters.allowed_chunk_ids or (),
    ):
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("Full-text filter values must be non-empty strings.")
    start = filters.published_from
    end = filters.published_to
    if start is not None:
        _serialize_utc(start)
    if end is not None:
        _serialize_utc(end)
    if start is not None and end is not None:
        if start.astimezone(timezone.utc) >= end.astimezone(timezone.utc):
            raise ValueError("Published filter start must be before end.")
    return filters


def _validate_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip() or "\x00" in query:
        raise ValueError("Full-text query must be non-empty text.")
    return query.strip()


def _document_bigrams(text: str) -> tuple[tuple[int, str], ...]:
    result: list[tuple[int, str]] = []
    position = 0
    for match in _CJK_RUN.finditer(text):
        run = match.group(0)
        for index in range(len(run) - 1):
            result.append((position, run[index : index + 2]))
            position += 1
    return tuple(result)


def _query_bigrams(query: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(bigram for _position, bigram in _document_bigrams(query)))


def _canonical_generation_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Generation ID must be a canonical UUID.")
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise ValueError("Generation ID must be a canonical UUID.") from None
    if parsed != value:
        raise ValueError("Generation ID must be a canonical UUID.")
    return parsed


def _serialize_optional_utc(value: datetime | None) -> str | None:
    return _serialize_utc(value) if value is not None else None


def _serialize_utc(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Published timestamps must include a timezone.")
    normalized = value.astimezone(timezone.utc)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


def _parse_optional_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise FullTextIndexInvalid("Full-text index timestamp is invalid.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise FullTextIndexInvalid("Full-text index timestamp is invalid.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FullTextIndexInvalid("Full-text index timestamp is invalid.")
    return parsed.astimezone(timezone.utc)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _is_fts_unavailable(error: BaseException) -> bool:
    message = str(error).lower()
    return "fts5" in message or "trigram" in message or "no such module" in message


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise FullTextIndexInvalid("Full-text index path is unavailable.") from None


def _is_link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _require_regular_file(path: Path) -> None:
    info = _lstat(path)
    if info is None or _is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise FullTextIndexInvalid("Full-text index file is unsafe.")


def _require_regular_final(path: Path) -> None:
    _require_safe_directory_chain(path.parent)
    _require_regular_file(path)


def _ensure_safe_directory_chain(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in parts:
        current /= part
        info = _lstat(current)
        if info is None:
            try:
                current.mkdir()
            except FileExistsError:
                pass
            except OSError:
                raise FullTextIndexInvalid("Full-text index directory is unavailable.") from None
            info = _lstat(current)
        if info is None or _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise FullTextIndexInvalid("Full-text index directory is unsafe.")


def _require_safe_directory_chain(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in parts:
        current /= part
        info = _lstat(current)
        if info is None or _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise FullTextIndexInvalid("Full-text index directory is unsafe.")


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise FullTextIndexInvalid("Full-text index directory could not be synchronized.") from None
