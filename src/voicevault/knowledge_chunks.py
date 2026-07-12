from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass

from .app_db import AppDatabase


class KnowledgeChunkError(Exception):
    """Base class for deterministic knowledge-chunk failures."""


class KnowledgeChunkConflict(KnowledgeChunkError):
    """Stored chunk state conflicts with deterministic materialization."""


@dataclass(frozen=True)
class ChunkingRule:
    version: str = "paragraph-window-v1"
    max_chars: int = 1200
    overlap_chars: int = 160

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("Chunk rule version is required.")
        if (
            not isinstance(self.max_chars, int)
            or isinstance(self.max_chars, bool)
            or self.max_chars < 1
        ):
            raise ValueError("Chunk max_chars must be a positive integer.")
        if (
            not isinstance(self.overlap_chars, int)
            or isinstance(self.overlap_chars, bool)
            or self.overlap_chars < 0
            or self.overlap_chars >= self.max_chars
        ):
            raise ValueError("Chunk overlap must be non-negative and smaller than max_chars.")


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    revision_id: str
    ordinal: int
    char_start: int
    char_end: int
    text: str


def chunk_revision(
    revision_id: str,
    content_text: str,
    rule: ChunkingRule,
) -> tuple[KnowledgeChunk, ...]:
    if not isinstance(revision_id, str) or not revision_id.strip():
        raise ValueError("Revision ID is required.")
    if not isinstance(content_text, str) or not content_text:
        raise ValueError("Revision content text must not be empty.")
    if not isinstance(rule, ChunkingRule):
        raise TypeError("Chunk rule must be a ChunkingRule.")

    boundaries = tuple(match.end() for match in re.finditer(r"\n+", content_text))
    spans: list[tuple[int, int]] = []
    start = 0
    text_length = len(content_text)
    while start < text_length:
        limit = min(start + rule.max_chars, text_length)
        if limit == text_length:
            end = text_length
        else:
            natural = [
                boundary
                for boundary in boundaries
                if start + rule.overlap_chars < boundary <= limit
            ]
            end = natural[-1] if natural else limit
        spans.append((start, end))
        if end == text_length:
            break
        start = end - min(rule.overlap_chars, end - start - 1)

    chunks: list[KnowledgeChunk] = []
    for ordinal, (char_start, char_end) in enumerate(spans):
        text = content_text[char_start:char_end]
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        chunk_id = _chunk_id(
            revision_id=revision_id,
            rule_version=rule.version,
            char_start=char_start,
            char_end=char_end,
            content_hash=content_hash,
        )
        chunks.append(
            KnowledgeChunk(
                chunk_id=chunk_id,
                revision_id=revision_id,
                ordinal=ordinal,
                char_start=char_start,
                char_end=char_end,
                text=text,
            )
        )
    return tuple(chunks)


class KnowledgeChunkRepository:
    """Materialize current active revisions on a caller-owned SQLite connection."""

    def __init__(self, database: AppDatabase) -> None:
        self.database = database

    def materialize_current(
        self,
        connection: sqlite3.Connection,
        person_id: str,
        rule: ChunkingRule,
    ) -> tuple[KnowledgeChunk, ...]:
        if not isinstance(person_id, str) or not person_id.strip():
            raise ValueError("Person ID is required.")
        rows = connection.execute(
            """
            SELECT pr.revision_id, pr.content_text
            FROM post_revisions pr
            JOIN posts p ON p.post_id = pr.post_id
            JOIN platform_accounts a ON a.account_id = p.account_id
            JOIN content_dispositions d
              ON d.post_id = p.post_id AND d.state = 'active'
            WHERE a.person_id = ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM post_revisions newer
                  WHERE newer.post_id = pr.post_id
                    AND (
                        newer.captured_at > pr.captured_at
                        OR (
                            newer.captured_at = pr.captured_at
                            AND newer.revision_id > pr.revision_id
                        )
                    )
              )
            ORDER BY p.post_id, pr.revision_id
            """,
            (person_id,),
        ).fetchall()

        return self._materialize_rows(connection, rows, rule)

    def materialize_active_revisions(
        self,
        connection: sqlite3.Connection,
        person_id: str,
        rule: ChunkingRule,
    ) -> tuple[KnowledgeChunk, ...]:
        if not isinstance(person_id, str) or not person_id.strip():
            raise ValueError("Person ID is required.")
        rows = connection.execute(
            """
            SELECT pr.revision_id, pr.content_text
            FROM post_revisions pr
            JOIN posts p ON p.post_id = pr.post_id
            JOIN platform_accounts a ON a.account_id = p.account_id
            JOIN content_dispositions d
              ON d.post_id = p.post_id AND d.state = 'active'
            WHERE a.person_id = ?
            ORDER BY p.post_id, pr.captured_at, pr.revision_id
            """,
            (person_id,),
        ).fetchall()
        return self._materialize_rows(connection, rows, rule)

    @staticmethod
    def _materialize_rows(
        connection: sqlite3.Connection,
        rows: list[sqlite3.Row],
        rule: ChunkingRule,
    ) -> tuple[KnowledgeChunk, ...]:
        materialized: list[KnowledgeChunk] = []
        for row in rows:
            chunks = chunk_revision(row["revision_id"], row["content_text"], rule)
            for chunk in chunks:
                content_hash = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
                connection.execute(
                    """
                    INSERT OR IGNORE INTO knowledge_chunks(
                        chunk_id, revision_id, rule_version, ordinal,
                        char_start, char_end, content_text, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.chunk_id,
                        chunk.revision_id,
                        rule.version,
                        chunk.ordinal,
                        chunk.char_start,
                        chunk.char_end,
                        chunk.text,
                        content_hash,
                    ),
                )
                stored = connection.execute(
                    """
                    SELECT revision_id, rule_version, ordinal, char_start,
                           char_end, content_text, content_hash
                    FROM knowledge_chunks WHERE chunk_id = ?
                    """,
                    (chunk.chunk_id,),
                ).fetchone()
                expected = (
                    chunk.revision_id,
                    rule.version,
                    chunk.ordinal,
                    chunk.char_start,
                    chunk.char_end,
                    chunk.text,
                    content_hash,
                )
                if stored is None or tuple(stored) != expected:
                    raise KnowledgeChunkConflict(
                        "Stored knowledge chunk conflicts with deterministic materialization."
                    )
                materialized.append(chunk)
        return tuple(materialized)


def _chunk_id(
    *,
    revision_id: str,
    rule_version: str,
    char_start: int,
    char_end: int,
    content_hash: str,
) -> str:
    canonical = json.dumps(
        {
            "char_end": char_end,
            "char_start": char_start,
            "content_hash": content_hash,
            "revision_id": revision_id,
            "rule_version": rule_version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
