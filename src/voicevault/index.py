from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .kb import KnowledgeBase
from .models import Event, Statement


class VoiceVaultIndex:
    def __init__(self, kb: KnowledgeBase):
        self.kb = kb
        self.path = kb.index_path

    def rebuild(self, statements: list[Statement]) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("DROP TABLE IF EXISTS statements")
            _create_schema(conn)
            conn.execute("DELETE FROM build_info")
            conn.executemany(
                """
                INSERT INTO statements (
                  statement_id, role_id, source_type, source_url, published_at, captured_at,
                  title, body, symbols, topics, stance, time_horizon, confidence, notes,
                  source_platform, source_user_id, source_author
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_statement_row(statement) for statement in statements],
            )
            conn.execute(
                "INSERT INTO build_info (key, value) VALUES (?, ?)",
                ("built_at", datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        return len(statements)

    def count_statements(self) -> int:
        with closing(self._connect()) as conn:
            _create_schema(conn)
            row = conn.execute("SELECT COUNT(*) AS count FROM statements").fetchone()
        return int(row["count"])

    def list_roles(self) -> list[str]:
        with closing(self._connect()) as conn:
            _create_schema(conn)
            rows = conn.execute("SELECT DISTINCT role_id FROM statements ORDER BY role_id").fetchall()
        return [str(row["role_id"]) for row in rows]

    def statements_for_role(self, role_id: str) -> list[Statement]:
        with closing(self._connect()) as conn:
            _create_schema(conn)
            rows = conn.execute(
                "SELECT * FROM statements WHERE role_id = ? ORDER BY published_at, statement_id",
                (role_id,),
            ).fetchall()
        return [_row_to_statement(row) for row in rows]

    def all_statements(self) -> list[Statement]:
        with closing(self._connect()) as conn:
            _create_schema(conn)
            rows = conn.execute("SELECT * FROM statements ORDER BY role_id, published_at, statement_id").fetchall()
        return [_row_to_statement(row) for row in rows]

    def query_relevant(self, event: Event) -> dict[str, list[Statement]]:
        wanted_symbols = {value.lower() for value in event.symbols}
        wanted_topics = {value.lower() for value in event.topics}
        grouped: dict[str, list[Statement]] = {}
        for statement in self.all_statements():
            statement_symbols = {value.lower() for value in statement.symbols}
            statement_topics = {value.lower() for value in statement.topics}
            body = statement.body.lower()
            symbol_hit = bool(wanted_symbols & statement_symbols) or any(symbol in body for symbol in wanted_symbols)
            topic_hit = bool(wanted_topics & statement_topics) or any(topic in body for topic in wanted_topics)
            if symbol_hit or topic_hit:
                grouped.setdefault(statement.role_id, []).append(statement)
        return grouped

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS statements (
          statement_id TEXT PRIMARY KEY,
          role_id TEXT NOT NULL,
          source_type TEXT NOT NULL,
          source_url TEXT NOT NULL,
          published_at TEXT NOT NULL,
          captured_at TEXT NOT NULL,
          title TEXT NOT NULL,
          body TEXT NOT NULL,
          symbols TEXT NOT NULL,
          topics TEXT NOT NULL,
          stance TEXT NOT NULL,
          time_horizon TEXT NOT NULL,
          confidence TEXT NOT NULL,
          notes TEXT NOT NULL,
          source_platform TEXT NOT NULL,
          source_user_id TEXT NOT NULL,
          source_author TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_statements_role ON statements(role_id)")
    conn.execute("CREATE TABLE IF NOT EXISTS build_info (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


def _statement_row(statement: Statement) -> tuple[str, ...]:
    return (
        statement.statement_id,
        statement.role_id,
        statement.source_type,
        statement.source_url,
        statement.published_at,
        statement.captured_at,
        statement.title,
        statement.body,
        json.dumps(statement.symbols, ensure_ascii=False),
        json.dumps(statement.topics, ensure_ascii=False),
        statement.stance,
        statement.time_horizon,
        statement.confidence,
        statement.notes,
        statement.source_platform,
        statement.source_user_id,
        statement.source_author,
    )


def _row_to_statement(row: sqlite3.Row) -> Statement:
    return Statement(
        statement_id=str(row["statement_id"]),
        role_id=str(row["role_id"]),
        source_type=str(row["source_type"]),
        source_url=str(row["source_url"]),
        published_at=str(row["published_at"]),
        captured_at=str(row["captured_at"]),
        title=str(row["title"]),
        body=str(row["body"]),
        symbols=json.loads(str(row["symbols"])),
        topics=json.loads(str(row["topics"])),
        stance=str(row["stance"]),
        time_horizon=str(row["time_horizon"]),
        confidence=str(row["confidence"]),
        notes=str(row["notes"]),
        source_platform=_row_text(row, "source_platform"),
        source_user_id=_row_text(row, "source_user_id"),
        source_author=_row_text(row, "source_author"),
    )


def _row_text(row: sqlite3.Row, key: str) -> str:
    return str(row[key]) if key in row.keys() else ""
