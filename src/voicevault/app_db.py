from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator


DATABASE_FILENAME = "voicevault.db"


class AppDatabaseError(Exception):
    """Base class for stable application-database failures."""


class DatabaseVersionError(AppDatabaseError):
    def __init__(self, database_version: int, code_version: int) -> None:
        self.database_version = database_version
        self.code_version = code_version
        super().__init__(
            f"Database migration version {database_version} is newer than supported version {code_version}."
        )


class MigrationRegistryError(AppDatabaseError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    apply: Callable[[sqlite3.Connection], None]


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def resolve_data_dir(data_dir: str | Path | None = None) -> Path:
    if data_dir is not None:
        return Path(data_dir).expanduser()
    configured = os.environ.get("VOICEVAULT_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "VoiceVault"


class AppDatabase:
    def __init__(
        self,
        *,
        data_dir: str | Path | None = None,
        db_path: str | Path | None = None,
        migrations: Iterable[Migration] | None = None,
    ) -> None:
        if data_dir is not None and db_path is not None:
            raise ValueError("Specify either data_dir or db_path, not both.")
        self.path = Path(db_path).expanduser() if db_path is not None else resolve_data_dir(data_dir) / DATABASE_FILENAME
        self.migrations = tuple(migrations) if migrations is not None else DEFAULT_MIGRATIONS
        expected_versions = tuple(range(1, len(self.migrations) + 1))
        actual_versions = tuple(migration.version for migration in self.migrations)
        if actual_versions != expected_versions:
            raise MigrationRegistryError("Migration versions must be contiguous and start at 1.")

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, factory=_ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            self._ensure_migration_table(connection)
            applied_versions = {
                row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
            }
            database_version = max(applied_versions, default=0)
            code_version = self.migrations[-1].version if self.migrations else 0
            if database_version > code_version:
                raise DatabaseVersionError(database_version, code_version)
            for migration in self.migrations:
                if migration.version not in applied_versions:
                    self._apply_migration(connection, migration)

    @staticmethod
    def _ensure_migration_table(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    @staticmethod
    def _apply_migration(connection: sqlite3.Connection, migration: Migration) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            migration.apply(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))",
                (migration.version,),
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()


def _apply_v1(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE persons (
            person_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE person_aliases (
            alias_id TEXT PRIMARY KEY,
            person_id TEXT NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
            alias TEXT NOT NULL CHECK (length(trim(alias)) > 0),
            created_at TEXT NOT NULL,
            UNIQUE(person_id, alias)
        )
        """,
        """
        CREATE TABLE platform_accounts (
            account_id TEXT PRIMARY KEY,
            person_id TEXT NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
            platform TEXT NOT NULL CHECK (length(trim(platform)) > 0),
            external_user_id TEXT NOT NULL CHECK (length(trim(external_user_id)) > 0),
            display_name TEXT,
            archive_basis_confirmed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(platform, external_user_id)
        )
        """,
        """
        CREATE TABLE coverage_intervals (
            coverage_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES platform_accounts(account_id) ON DELETE CASCADE,
            start_at TEXT NOT NULL,
            end_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            CHECK (start_at < end_at),
            UNIQUE(account_id, start_at, end_at)
        )
        """,
    )
    for statement in statements:
        connection.execute(statement)


def _apply_v2(connection: sqlite3.Connection) -> None:
    statuses = (
        "'pending_codex', 'claimed', 'running', 'waiting_for_human', "
        "'rate_limited', 'partial', 'succeeded', 'failed', 'cancelled', 'interrupted'"
    )
    connection.execute(
        f"""
        CREATE TABLE collection_jobs (
            job_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES platform_accounts(account_id) ON DELETE CASCADE,
            mode TEXT NOT NULL CHECK (mode IN ('normal', 'recheck')),
            status TEXT NOT NULL CHECK (status IN ({statuses})),
            requested_start_at TEXT NOT NULL,
            requested_end_at TEXT NOT NULL,
            outcome TEXT,
            remote_action_count INTEGER NOT NULL DEFAULT 0 CHECK (remote_action_count >= 0),
            handoff_version INTEGER NOT NULL DEFAULT 0 CHECK (handoff_version >= 0),
            collector_id TEXT,
            lease_expires_at TEXT,
            cancel_requested_at TEXT,
            checkpoint_json TEXT,
            error_code TEXT,
            error_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (requested_start_at < requested_end_at)
        )
        """
    )
    connection.execute(
        f"""
        CREATE UNIQUE INDEX one_active_collection_job_per_account
        ON collection_jobs(account_id)
        WHERE status NOT IN ('succeeded', 'failed', 'cancelled')
        """
    )
    connection.execute(
        "CREATE INDEX collection_jobs_status_idx ON collection_jobs(status, updated_at)"
    )
    connection.execute(
        """
        CREATE TABLE collection_segments (
            segment_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES collection_jobs(job_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            start_at TEXT NOT NULL,
            end_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running')),
            checkpoint_json TEXT,
            progress_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (start_at < end_at),
            UNIQUE(job_id, ordinal),
            UNIQUE(job_id, start_at, end_at)
        )
        """
    )
    connection.execute(
        "CREATE INDEX collection_segments_job_idx ON collection_segments(job_id, ordinal)"
    )
    connection.execute(
        """
        CREATE TABLE collection_handoffs (
            handoff_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES collection_jobs(job_id) ON DELETE CASCADE,
            version INTEGER NOT NULL CHECK (version > 0),
            instance_id TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            claimed_at TEXT,
            revoked_at TEXT,
            collector_id TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(job_id, version)
        )
        """
    )
    connection.execute(
        "CREATE INDEX collection_handoffs_job_idx ON collection_handoffs(job_id, version)"
    )
    connection.execute(
        "ALTER TABLE coverage_intervals ADD COLUMN job_id TEXT REFERENCES collection_jobs(job_id)"
    )
    connection.execute(
        "CREATE INDEX coverage_intervals_job_idx ON coverage_intervals(job_id)"
    )


def _apply_v3(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE posts (
            post_id TEXT NOT NULL PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES platform_accounts(account_id) ON DELETE RESTRICT,
            external_post_id TEXT NOT NULL CHECK (length(trim(external_post_id)) > 0),
            published_at TEXT,
            canonical_url TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(account_id, external_post_id)
        )
        """
    )
    connection.execute(
        "CREATE INDEX posts_account_published_idx ON posts(account_id, published_at, post_id)"
    )
    connection.execute(
        """
        CREATE TABLE post_revisions (
            revision_id TEXT NOT NULL PRIMARY KEY,
            post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
            content_hash TEXT NOT NULL CHECK (
                typeof(content_hash) = 'text'
                AND length(content_hash) = 64
                AND length(CAST(content_hash AS BLOB)) = 64
                AND content_hash NOT GLOB '*[^0-9a-f]*'
            ),
            content_text TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            first_seen_job_id TEXT REFERENCES collection_jobs(job_id) ON DELETE RESTRICT,
            UNIQUE(post_id, content_hash)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX post_revisions_post_captured_idx
        ON post_revisions(post_id, captured_at, revision_id)
        """
    )
    connection.execute(
        """
        CREATE TABLE post_observations (
            observation_id TEXT NOT NULL PRIMARY KEY,
            post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK (status IN ('available', 'deleted', 'unavailable')),
            observed_at TEXT NOT NULL,
            source_job_id TEXT NOT NULL REFERENCES collection_jobs(job_id) ON DELETE RESTRICT,
            UNIQUE(source_job_id, post_id, status, observed_at)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX post_observations_post_observed_idx
        ON post_observations(post_id, observed_at, observation_id)
        """
    )
    connection.execute(
        """
        CREATE TABLE capture_evidence (
            evidence_id TEXT NOT NULL PRIMARY KEY,
            sha256 TEXT NOT NULL UNIQUE CHECK (
                typeof(sha256) = 'text'
                AND length(sha256) = 64
                AND length(CAST(sha256 AS BLOB)) = 64
                AND sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            media_type TEXT NOT NULL CHECK (length(trim(media_type)) > 0),
            byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
            local_path TEXT NOT NULL UNIQUE CHECK (length(trim(local_path)) > 0),
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE post_evidence (
            post_evidence_id TEXT NOT NULL PRIMARY KEY,
            revision_id TEXT REFERENCES post_revisions(revision_id) ON DELETE CASCADE,
            observation_id TEXT REFERENCES post_observations(observation_id) ON DELETE CASCADE,
            evidence_id TEXT NOT NULL REFERENCES capture_evidence(evidence_id) ON DELETE RESTRICT,
            job_id TEXT NOT NULL REFERENCES collection_jobs(job_id) ON DELETE RESTRICT,
            relation_kind TEXT NOT NULL CHECK (length(trim(relation_kind)) > 0),
            created_at TEXT NOT NULL,
            CHECK ((revision_id IS NULL) <> (observation_id IS NULL))
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX post_evidence_revision_unique_idx
        ON post_evidence(job_id, revision_id, evidence_id, relation_kind)
        WHERE revision_id IS NOT NULL
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX post_evidence_observation_unique_idx
        ON post_evidence(job_id, observation_id, evidence_id, relation_kind)
        WHERE observation_id IS NOT NULL
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX collection_segments_job_segment_unique_idx
        ON collection_segments(job_id, segment_id)
        """
    )
    connection.execute(
        """
        CREATE TABLE collection_job_evidence (
            job_evidence_id TEXT NOT NULL PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES collection_jobs(job_id) ON DELETE RESTRICT,
            segment_id TEXT REFERENCES collection_segments(segment_id) ON DELETE RESTRICT,
            evidence_id TEXT NOT NULL REFERENCES capture_evidence(evidence_id) ON DELETE RESTRICT,
            evidence_role TEXT NOT NULL CHECK (length(trim(evidence_role)) > 0),
            checkpoint_key TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (job_id, segment_id)
                REFERENCES collection_segments(job_id, segment_id) ON DELETE RESTRICT,
            UNIQUE(job_id, evidence_id, evidence_role, checkpoint_key)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX collection_job_evidence_job_role_checkpoint_idx
        ON collection_job_evidence(job_id, evidence_role, checkpoint_key)
        """
    )
    connection.execute(
        """
        CREATE TABLE content_dispositions (
            post_id TEXT NOT NULL PRIMARY KEY REFERENCES posts(post_id) ON DELETE CASCADE,
            state TEXT NOT NULL CHECK (state IN ('active', 'suppressed', 'purged')),
            reason TEXT,
            changed_at TEXT NOT NULL,
            purged_content_hash TEXT,
            CHECK (
                state = 'active'
                OR (reason IS NOT NULL AND length(trim(reason)) > 0)
            ),
            CHECK (
                purged_content_hash IS NULL
                OR (
                    typeof(purged_content_hash) = 'text'
                    AND length(purged_content_hash) = 64
                    AND length(CAST(purged_content_hash AS BLOB)) = 64
                    AND purged_content_hash NOT GLOB '*[^0-9a-f]*'
                )
            ),
            CHECK (state <> 'purged' OR purged_content_hash IS NOT NULL)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX content_dispositions_state_changed_idx
        ON content_dispositions(state, changed_at)
        """
    )
    connection.execute(
        """
        CREATE TABLE collection_checkpoints (
            checkpoint_id TEXT NOT NULL PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES collection_jobs(job_id) ON DELETE RESTRICT,
            segment_id TEXT NOT NULL REFERENCES collection_segments(segment_id) ON DELETE RESTRICT,
            sequence INTEGER NOT NULL CHECK (sequence >= 0),
            observed_at TEXT NOT NULL,
            action_type TEXT NOT NULL CHECK (length(trim(action_type)) > 0),
            triggered_remote_load INTEGER NOT NULL CHECK (triggered_remote_load IN (0, 1)),
            remote_action_ordinal INTEGER CHECK (
                remote_action_ordinal IS NULL OR remote_action_ordinal >= 1
            ),
            visible_post_ids_json TEXT NOT NULL,
            earliest_non_pinned_at TEXT,
            latest_non_pinned_at TEXT,
            anchor_post_id TEXT,
            start_kind TEXT,
            completion_reason TEXT,
            boundary_post_id TEXT,
            canonical_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (job_id, segment_id)
                REFERENCES collection_segments(job_id, segment_id) ON DELETE RESTRICT,
            UNIQUE(job_id, segment_id, sequence)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX collection_checkpoints_job_segment_sequence_idx
        ON collection_checkpoints(job_id, segment_id, sequence)
        """
    )
    connection.execute(
        """
        CREATE TABLE collection_submissions (
            submission_id TEXT NOT NULL PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES collection_jobs(job_id) ON DELETE RESTRICT,
            handoff_version INTEGER NOT NULL CHECK (handoff_version > 0),
            collector_id TEXT NOT NULL CHECK (length(trim(collector_id)) > 0),
            manifest_sha256 TEXT NOT NULL CHECK (
                typeof(manifest_sha256) = 'text'
                AND length(manifest_sha256) = 64
                AND length(CAST(manifest_sha256 AS BLOB)) = 64
                AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            accepted_manifest_json TEXT NOT NULL,
            receipt_json TEXT NOT NULL,
            outcome_kind TEXT NOT NULL CHECK (outcome_kind IN ('complete', 'partial')),
            accepted_at TEXT NOT NULL,
            UNIQUE(job_id, handoff_version),
            UNIQUE(job_id, submission_id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX collection_submissions_job_accepted_idx
        ON collection_submissions(job_id, accepted_at)
        """
    )
    connection.execute("ALTER TABLE collection_jobs ADD COLUMN last_heartbeat_at TEXT")
    connection.execute("ALTER TABLE collection_jobs ADD COLUMN submitted_at TEXT")
    connection.execute(
        """
        ALTER TABLE collection_jobs ADD COLUMN result_manifest_sha256 TEXT CHECK (
            result_manifest_sha256 IS NULL
            OR (
                typeof(result_manifest_sha256) = 'text'
                AND length(result_manifest_sha256) = 64
                AND length(CAST(result_manifest_sha256 AS BLOB)) = 64
                AND result_manifest_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        )
        """
    )


def _apply_v4(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE knowledge_chunks (
            chunk_id TEXT NOT NULL PRIMARY KEY,
            revision_id TEXT NOT NULL
                REFERENCES post_revisions(revision_id) ON DELETE CASCADE,
            rule_version TEXT NOT NULL CHECK (length(trim(rule_version)) > 0),
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            char_start INTEGER NOT NULL CHECK (char_start >= 0),
            char_end INTEGER NOT NULL CHECK (char_end > char_start),
            content_text TEXT NOT NULL CHECK (length(content_text) > 0),
            content_hash TEXT NOT NULL CHECK (
                typeof(content_hash) = 'text'
                AND length(content_hash) = 64
                AND length(CAST(content_hash AS BLOB)) = 64
                AND content_hash NOT GLOB '*[^0-9a-f]*'
            ),
            UNIQUE(revision_id, rule_version, ordinal),
            UNIQUE(revision_id, rule_version, char_start, char_end)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX knowledge_chunks_revision_rule_offsets_idx
        ON knowledge_chunks(revision_id, rule_version, char_start, char_end)
        """
    )
    connection.execute(
        """
        CREATE TABLE index_generations (
            generation_id TEXT NOT NULL PRIMARY KEY,
            person_id TEXT NOT NULL REFERENCES persons(person_id) ON DELETE RESTRICT,
            chunk_rule_version TEXT NOT NULL CHECK (length(trim(chunk_rule_version)) > 0),
            embedding_provider TEXT CHECK (
                embedding_provider IS NULL OR length(trim(embedding_provider)) > 0
            ),
            embedding_model TEXT CHECK (
                embedding_model IS NULL OR length(trim(embedding_model)) > 0
            ),
            embedding_dimension INTEGER CHECK (
                embedding_dimension IS NULL OR embedding_dimension > 0
            ),
            embedding_fingerprint TEXT CHECK (
                embedding_fingerprint IS NULL
                OR (
                    typeof(embedding_fingerprint) = 'text'
                    AND length(embedding_fingerprint) = 64
                    AND length(CAST(embedding_fingerprint AS BLOB)) = 64
                    AND embedding_fingerprint NOT GLOB '*[^0-9a-f]*'
                )
            ),
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'building', 'ready', 'degraded', 'stale', 'failed')
            ),
            retrieval_mode TEXT NOT NULL CHECK (
                retrieval_mode IN ('none', 'fulltext_only', 'hybrid')
            ),
            error_json TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(generation_id, person_id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX index_generations_person_status_idx
        ON index_generations(person_id, status, created_at, generation_id)
        """
    )
    connection.execute(
        """
        CREATE TABLE index_generation_chunks (
            generation_id TEXT NOT NULL
                REFERENCES index_generations(generation_id) ON DELETE CASCADE,
            chunk_id TEXT NOT NULL
                REFERENCES knowledge_chunks(chunk_id) ON DELETE CASCADE,
            PRIMARY KEY(generation_id, chunk_id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX index_generation_chunks_chunk_idx
        ON index_generation_chunks(chunk_id, generation_id)
        """
    )
    connection.execute(
        """
        CREATE TRIGGER index_generation_chunks_person_insert
        BEFORE INSERT ON index_generation_chunks
        WHEN NOT EXISTS (
            SELECT 1
            FROM index_generations g
            JOIN knowledge_chunks c ON c.chunk_id = NEW.chunk_id
            JOIN post_revisions r ON r.revision_id = c.revision_id
            JOIN posts p ON p.post_id = r.post_id
            JOIN platform_accounts a ON a.account_id = p.account_id
            WHERE g.generation_id = NEW.generation_id
              AND g.person_id = a.person_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'generation chunk person ownership mismatch');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER index_generation_chunks_person_update
        BEFORE UPDATE OF generation_id, chunk_id ON index_generation_chunks
        WHEN NOT EXISTS (
            SELECT 1
            FROM index_generations g
            JOIN knowledge_chunks c ON c.chunk_id = NEW.chunk_id
            JOIN post_revisions r ON r.revision_id = c.revision_id
            JOIN posts p ON p.post_id = r.post_id
            JOIN platform_accounts a ON a.account_id = p.account_id
            WHERE g.generation_id = NEW.generation_id
              AND g.person_id = a.person_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'generation chunk person ownership mismatch');
        END
        """
    )
    connection.execute(
        """
        CREATE TABLE person_index_heads (
            person_id TEXT NOT NULL PRIMARY KEY
                REFERENCES persons(person_id) ON DELETE RESTRICT,
            generation_id TEXT NOT NULL UNIQUE,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(generation_id, person_id)
                REFERENCES index_generations(generation_id, person_id) ON DELETE RESTRICT
        )
        """
    )


def _apply_v5(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE retrieval_runs (
            run_id TEXT NOT NULL PRIMARY KEY,
            request_json TEXT NOT NULL CHECK (length(request_json) > 0),
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'running', 'succeeded', 'failed', 'interrupted')
            ),
            retrieval_mode TEXT NOT NULL CHECK (
                retrieval_mode IN ('none', 'hybrid', 'mixed', 'fulltext_only')
            ),
            degradation_json TEXT CHECK (
                degradation_json IS NULL OR length(degradation_json) > 0
            ),
            error_json TEXT CHECK (error_json IS NULL OR length(error_json) > 0),
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            CHECK (status <> 'pending' OR (started_at IS NULL AND completed_at IS NULL)),
            CHECK (status <> 'running' OR (started_at IS NOT NULL AND completed_at IS NULL)),
            CHECK (
                status NOT IN ('succeeded', 'failed', 'interrupted')
                OR completed_at IS NOT NULL
            ),
            CHECK (status <> 'succeeded' OR retrieval_mode <> 'none'),
            CHECK (status NOT IN ('pending', 'running', 'failed', 'interrupted') OR retrieval_mode = 'none')
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX retrieval_runs_status_created_idx
        ON retrieval_runs(status, created_at, run_id)
        """
    )
    connection.execute(
        """
        CREATE TABLE retrieval_run_persons (
            run_id TEXT NOT NULL REFERENCES retrieval_runs(run_id) ON DELETE CASCADE,
            person_id TEXT NOT NULL REFERENCES persons(person_id) ON DELETE RESTRICT,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            generation_id TEXT,
            generation_status TEXT NOT NULL CHECK (
                generation_status IN (
                    'missing', 'pending', 'building', 'ready',
                    'degraded', 'stale', 'failed'
                )
            ),
            retrieval_mode TEXT NOT NULL CHECK (
                retrieval_mode IN ('none', 'hybrid', 'fulltext_only')
            ),
            PRIMARY KEY(run_id, person_id),
            UNIQUE(run_id, ordinal),
            UNIQUE(run_id, person_id, generation_id),
            FOREIGN KEY(generation_id, person_id)
                REFERENCES index_generations(generation_id, person_id) ON DELETE RESTRICT,
            CHECK (
                (generation_id IS NULL AND generation_status = 'missing' AND retrieval_mode = 'none')
                OR (generation_id IS NOT NULL AND generation_status <> 'missing')
            )
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE retrieval_evidence (
            evidence_id TEXT NOT NULL PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES retrieval_runs(run_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            person_id TEXT NOT NULL REFERENCES persons(person_id) ON DELETE RESTRICT,
            account_id TEXT NOT NULL REFERENCES platform_accounts(account_id) ON DELETE RESTRICT,
            platform TEXT NOT NULL CHECK (length(trim(platform)) > 0),
            post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE RESTRICT,
            revision_id TEXT NOT NULL REFERENCES post_revisions(revision_id) ON DELETE RESTRICT,
            chunk_id TEXT NOT NULL REFERENCES knowledge_chunks(chunk_id) ON DELETE RESTRICT,
            generation_id TEXT NOT NULL REFERENCES index_generations(generation_id) ON DELETE RESTRICT,
            canonical_url TEXT,
            published_at TEXT,
            captured_at TEXT NOT NULL,
            observation_status TEXT CHECK (
                observation_status IS NULL
                OR observation_status IN ('available', 'deleted', 'unavailable')
            ),
            observed_at TEXT,
            char_start INTEGER NOT NULL CHECK (char_start >= 0),
            char_end INTEGER NOT NULL CHECK (char_end > char_start),
            fulltext_rank INTEGER CHECK (fulltext_rank IS NULL OR fulltext_rank > 0),
            vector_rank INTEGER CHECK (vector_rank IS NULL OR vector_rank > 0),
            fused_rank INTEGER NOT NULL CHECK (fused_rank > 0),
            created_at TEXT NOT NULL,
            UNIQUE(run_id, ordinal),
            UNIQUE(run_id, chunk_id),
            FOREIGN KEY(generation_id, chunk_id)
                REFERENCES index_generation_chunks(generation_id, chunk_id) ON DELETE RESTRICT,
            FOREIGN KEY(run_id, person_id, generation_id)
                REFERENCES retrieval_run_persons(run_id, person_id, generation_id) ON DELETE RESTRICT,
            CHECK (fulltext_rank IS NOT NULL OR vector_rank IS NOT NULL),
            CHECK ((observation_status IS NULL) = (observed_at IS NULL))
        )
        """
    )
    connection.execute(
        """
        CREATE TRIGGER retrieval_run_persons_immutable_update
        BEFORE UPDATE OF
            person_id, ordinal, generation_id, generation_status
        ON retrieval_run_persons
        BEGIN
            SELECT RAISE(ABORT, 'retrieval person snapshot is immutable');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER retrieval_run_persons_mode_update
        BEFORE UPDATE OF retrieval_mode ON retrieval_run_persons
        WHEN NOT (
            EXISTS (
                SELECT 1 FROM retrieval_runs run
                WHERE run.run_id = OLD.run_id AND run.status = 'running'
            )
            AND (
                (OLD.retrieval_mode = 'hybrid'
                 AND NEW.retrieval_mode IN ('hybrid', 'fulltext_only', 'none'))
                OR (OLD.retrieval_mode = 'fulltext_only'
                    AND NEW.retrieval_mode IN ('fulltext_only', 'none'))
                OR (OLD.retrieval_mode = 'none' AND NEW.retrieval_mode = 'none')
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'retrieval person mode transition is invalid');
        END
        """
    )
    connection.execute(
        """
        CREATE INDEX retrieval_evidence_run_fused_idx
        ON retrieval_evidence(run_id, fused_rank, ordinal)
        """
    )
    lineage_query = """
        NOT EXISTS (
            SELECT 1
            FROM retrieval_run_persons rp
            JOIN platform_accounts a
              ON a.account_id = NEW.account_id
             AND a.person_id = NEW.person_id
             AND a.platform = NEW.platform
            JOIN posts p
              ON p.post_id = NEW.post_id
             AND p.account_id = NEW.account_id
            JOIN post_revisions r
              ON r.revision_id = NEW.revision_id
             AND r.post_id = NEW.post_id
            JOIN knowledge_chunks c
              ON c.chunk_id = NEW.chunk_id
             AND c.revision_id = NEW.revision_id
            JOIN index_generations g
              ON g.generation_id = NEW.generation_id
             AND g.person_id = NEW.person_id
            JOIN index_generation_chunks gc
              ON gc.generation_id = NEW.generation_id
             AND gc.chunk_id = NEW.chunk_id
            WHERE rp.run_id = NEW.run_id
              AND rp.person_id = NEW.person_id
              AND rp.generation_id = NEW.generation_id
              AND c.char_start = NEW.char_start
              AND c.char_end = NEW.char_end
              AND replace(r.captured_at, 'Z', '+00:00')
                    = replace(NEW.captured_at, 'Z', '+00:00')
              AND p.canonical_url IS NEW.canonical_url
              AND replace(p.published_at, 'Z', '+00:00')
                    IS replace(NEW.published_at, 'Z', '+00:00')
              AND (
                    (
                        NEW.observation_status IS NULL
                        AND NEW.observed_at IS NULL
                        AND NOT EXISTS (
                            SELECT 1 FROM post_observations any_observation
                            WHERE any_observation.post_id = NEW.post_id
                        )
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM post_observations observation
                        WHERE observation.post_id = NEW.post_id
                          AND observation.status = NEW.observation_status
                          AND replace(observation.observed_at, 'Z', '+00:00')
                                = replace(NEW.observed_at, 'Z', '+00:00')
                          AND NOT EXISTS (
                              SELECT 1
                              FROM post_observations newer
                              WHERE newer.post_id = observation.post_id
                                AND (
                                    newer.observed_at > observation.observed_at
                                    OR (
                                        newer.observed_at = observation.observed_at
                                        AND newer.observation_id > observation.observation_id
                                    )
                                )
                          )
                    )
              )
        )
    """
    connection.execute(
        f"""
        CREATE TRIGGER retrieval_evidence_lineage_insert
        BEFORE INSERT ON retrieval_evidence
        WHEN {lineage_query}
        BEGIN
            SELECT RAISE(ABORT, 'retrieval evidence lineage mismatch');
        END
        """
    )
    connection.execute(
        f"""
        CREATE TRIGGER retrieval_evidence_lineage_update
        BEFORE UPDATE OF
            run_id, person_id, account_id, platform, post_id, revision_id,
            chunk_id, generation_id, canonical_url, published_at, captured_at,
            observation_status, observed_at, char_start, char_end
        ON retrieval_evidence
        WHEN {lineage_query}
        BEGIN
            SELECT RAISE(ABORT, 'retrieval evidence lineage mismatch');
        END
        """
    )


def _apply_v6(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE question_runs (
            run_id TEXT NOT NULL PRIMARY KEY,
            retrieval_run_id TEXT NOT NULL
                REFERENCES retrieval_runs(run_id) ON DELETE RESTRICT,
            provider TEXT NOT NULL CHECK (
                provider IN ('codex_task', 'openai_compatible')
            ),
            status TEXT NOT NULL CHECK (
                status IN (
                    'pending_codex', 'pending_provider', 'running',
                    'succeeded', 'citation_invalid', 'failed', 'interrupted'
                )
            ),
            evidence_sha256 TEXT NOT NULL CHECK (
                length(evidence_sha256) = 64
                AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            candidate_json TEXT CHECK (
                candidate_json IS NULL OR length(candidate_json) > 0
            ),
            result_json TEXT CHECK (result_json IS NULL OR length(result_json) > 0),
            error_json TEXT CHECK (error_json IS NULL OR length(error_json) > 0),
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            CHECK (
                (provider = 'codex_task' AND status <> 'pending_provider')
                OR (provider = 'openai_compatible' AND status <> 'pending_codex')
            ),
            CHECK (
                status NOT IN ('pending_codex', 'pending_provider')
                OR (
                    started_at IS NULL AND completed_at IS NULL
                    AND candidate_json IS NULL AND result_json IS NULL
                    AND error_json IS NULL
                )
            ),
            CHECK (
                status <> 'running'
                OR (
                    started_at IS NOT NULL AND completed_at IS NULL
                    AND candidate_json IS NULL AND result_json IS NULL
                    AND error_json IS NULL
                )
            ),
            CHECK (
                status NOT IN ('succeeded', 'citation_invalid', 'failed', 'interrupted')
                OR completed_at IS NOT NULL
            ),
            CHECK (
                status <> 'succeeded'
                OR (candidate_json IS NOT NULL AND result_json IS NOT NULL AND error_json IS NULL)
            ),
            CHECK (
                status <> 'citation_invalid'
                OR (candidate_json IS NOT NULL AND result_json IS NULL AND error_json IS NOT NULL)
            ),
            CHECK (
                status NOT IN ('failed', 'interrupted')
                OR (result_json IS NULL AND error_json IS NOT NULL)
            )
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX question_runs_status_created_idx
        ON question_runs(status, created_at, run_id)
        """
    )
    connection.execute(
        """
        CREATE TRIGGER question_runs_snapshot_immutable_update
        BEFORE UPDATE OF
            retrieval_run_id, provider, evidence_sha256, created_at
        ON question_runs
        BEGIN
            SELECT RAISE(ABORT, 'question run snapshot is immutable');
        END
        """
    )
    connection.execute(
        """
        CREATE TABLE question_run_persons (
            run_id TEXT NOT NULL REFERENCES question_runs(run_id) ON DELETE CASCADE,
            person_id TEXT NOT NULL REFERENCES persons(person_id) ON DELETE RESTRICT,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
            has_evidence INTEGER NOT NULL CHECK (has_evidence IN (0, 1)),
            PRIMARY KEY(run_id, person_id),
            UNIQUE(run_id, ordinal)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE question_evidence (
            run_id TEXT NOT NULL REFERENCES question_runs(run_id) ON DELETE CASCADE,
            evidence_id TEXT NOT NULL CHECK (
                evidence_id GLOB 'E[1-9]*'
                AND evidence_id NOT GLOB 'E*[^0-9]*'
            ),
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            retrieval_run_id TEXT NOT NULL,
            retrieval_evidence_id TEXT NOT NULL,
            person_id TEXT NOT NULL REFERENCES persons(person_id) ON DELETE RESTRICT,
            account_id TEXT NOT NULL REFERENCES platform_accounts(account_id) ON DELETE RESTRICT,
            platform TEXT NOT NULL CHECK (length(trim(platform)) > 0),
            post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE RESTRICT,
            revision_id TEXT NOT NULL REFERENCES post_revisions(revision_id) ON DELETE RESTRICT,
            chunk_id TEXT NOT NULL REFERENCES knowledge_chunks(chunk_id) ON DELETE RESTRICT,
            excerpt TEXT NOT NULL CHECK (length(excerpt) > 0),
            char_start INTEGER NOT NULL CHECK (char_start >= 0),
            char_end INTEGER NOT NULL CHECK (char_end > char_start),
            canonical_url TEXT,
            published_at TEXT,
            captured_at TEXT NOT NULL,
            observation_status TEXT CHECK (
                observation_status IS NULL
                OR observation_status IN ('available', 'deleted', 'unavailable')
            ),
            observed_at TEXT,
            disposition_state TEXT NOT NULL CHECK (
                disposition_state IN ('active', 'suppressed', 'purged')
            ),
            PRIMARY KEY(run_id, evidence_id),
            UNIQUE(run_id, ordinal),
            UNIQUE(run_id, retrieval_evidence_id),
            FOREIGN KEY(run_id, person_id)
                REFERENCES question_run_persons(run_id, person_id) ON DELETE RESTRICT,
            FOREIGN KEY(retrieval_evidence_id)
                REFERENCES retrieval_evidence(evidence_id) ON DELETE RESTRICT,
            CHECK ((observation_status IS NULL) = (observed_at IS NULL))
        )
        """
    )
    connection.execute(
        """
        CREATE TRIGGER question_run_persons_immutable_update
        BEFORE UPDATE ON question_run_persons
        BEGIN
            SELECT RAISE(ABORT, 'question person snapshot is immutable');
        END
        """
    )
    lineage_query = """
        NOT EXISTS (
            SELECT 1
            FROM question_runs qr
            JOIN retrieval_runs rr
              ON rr.run_id = qr.retrieval_run_id
             AND rr.status = 'succeeded'
            JOIN retrieval_evidence re
              ON re.evidence_id = NEW.retrieval_evidence_id
             AND re.run_id = NEW.retrieval_run_id
            JOIN post_revisions revision
              ON revision.revision_id = NEW.revision_id
            JOIN knowledge_chunks chunk
              ON chunk.chunk_id = NEW.chunk_id
             AND chunk.revision_id = NEW.revision_id
            JOIN content_dispositions disposition
              ON disposition.post_id = NEW.post_id
            WHERE qr.run_id = NEW.run_id
              AND qr.retrieval_run_id = NEW.retrieval_run_id
              AND re.person_id = NEW.person_id
              AND re.account_id = NEW.account_id
              AND re.platform = NEW.platform
              AND re.post_id = NEW.post_id
              AND re.revision_id = NEW.revision_id
              AND re.chunk_id = NEW.chunk_id
              AND re.char_start = NEW.char_start
              AND re.char_end = NEW.char_end
              AND re.canonical_url IS NEW.canonical_url
              AND re.published_at IS NEW.published_at
              AND re.captured_at = NEW.captured_at
              AND re.observation_status IS NEW.observation_status
              AND re.observed_at IS NEW.observed_at
              AND disposition.state = NEW.disposition_state
              AND substr(
                    revision.content_text,
                    NEW.char_start + 1,
                    NEW.char_end - NEW.char_start
                  ) = NEW.excerpt
        )
    """
    connection.execute(
        f"""
        CREATE TRIGGER question_evidence_lineage_insert
        BEFORE INSERT ON question_evidence
        WHEN {lineage_query}
        BEGIN
            SELECT RAISE(ABORT, 'question evidence lineage mismatch');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER question_evidence_immutable_update
        BEFORE UPDATE ON question_evidence
        BEGIN
            SELECT RAISE(ABORT, 'question evidence snapshot is immutable');
        END
        """
    )


def _apply_v7(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE index_jobs (
            job_id TEXT NOT NULL PRIMARY KEY,
            person_id TEXT NOT NULL REFERENCES persons(person_id) ON DELETE RESTRICT,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'running', 'succeeded', 'failed', 'interrupted')
            ),
            generation_id TEXT,
            retrieval_mode TEXT NOT NULL DEFAULT 'none' CHECK (
                retrieval_mode IN ('none', 'hybrid', 'fulltext_only')
            ),
            error_json TEXT CHECK (error_json IS NULL OR length(error_json) > 0),
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            CHECK (status <> 'pending' OR (started_at IS NULL AND completed_at IS NULL)),
            CHECK (status <> 'running' OR (started_at IS NOT NULL AND completed_at IS NULL)),
            CHECK (
                status NOT IN ('succeeded', 'failed', 'interrupted')
                OR completed_at IS NOT NULL
            ),
            CHECK (
                status <> 'succeeded'
                OR (generation_id IS NOT NULL AND retrieval_mode <> 'none' AND error_json IS NULL)
            ),
            CHECK (
                status NOT IN ('pending', 'running', 'failed', 'interrupted')
                OR retrieval_mode = 'none'
            ),
            CHECK (status NOT IN ('failed', 'interrupted') OR error_json IS NOT NULL)
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX index_jobs_one_active_person_idx
        ON index_jobs(person_id) WHERE status IN ('pending', 'running')
        """
    )
    connection.execute(
        """
        CREATE INDEX index_jobs_status_created_idx
        ON index_jobs(status, created_at, job_id)
        """
    )
    connection.execute(
        """
        CREATE TABLE legacy_import_runs (
            run_id TEXT NOT NULL PRIMARY KEY,
            source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint) = 64),
            status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
            summary_json TEXT,
            error_json TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            CHECK (status = 'running' OR completed_at IS NOT NULL),
            CHECK (status <> 'succeeded' OR (summary_json IS NOT NULL AND error_json IS NULL)),
            CHECK (status <> 'failed' OR error_json IS NOT NULL)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE legacy_import_mappings (
            source_kind TEXT NOT NULL CHECK (length(trim(source_kind)) > 0),
            source_key TEXT NOT NULL CHECK (length(trim(source_key)) > 0),
            target_kind TEXT NOT NULL CHECK (length(trim(target_kind)) > 0),
            target_id TEXT NOT NULL CHECK (length(trim(target_id)) > 0),
            first_run_id TEXT NOT NULL
                REFERENCES legacy_import_runs(run_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            PRIMARY KEY(source_kind, source_key, target_kind),
            UNIQUE(target_kind, target_id)
        )
        """
    )


def _apply_v8(connection: sqlite3.Connection) -> None:
    replacements = (
        (
            "r.captured_at = NEW.captured_at",
            "replace(r.captured_at, 'Z', '+00:00') = replace(NEW.captured_at, 'Z', '+00:00')",
        ),
        (
            "p.published_at IS NEW.published_at",
            "replace(p.published_at, 'Z', '+00:00') IS replace(NEW.published_at, 'Z', '+00:00')",
        ),
        (
            "observation.observed_at = NEW.observed_at",
            "replace(observation.observed_at, 'Z', '+00:00') = replace(NEW.observed_at, 'Z', '+00:00')",
        ),
    )
    for name in (
        "retrieval_evidence_lineage_insert",
        "retrieval_evidence_lineage_update",
    ):
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (name,),
        ).fetchone()
        if row is None or not isinstance(row[0], str):
            raise sqlite3.DatabaseError("Retrieval lineage trigger is missing.")
        statement = row[0]
        for before, after in replacements:
            statement = statement.replace(before, after)
        connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(statement)


DEFAULT_MIGRATIONS = (
    Migration(1, _apply_v1),
    Migration(2, _apply_v2),
    Migration(3, _apply_v3),
    Migration(4, _apply_v4),
    Migration(5, _apply_v5),
    Migration(6, _apply_v6),
    Migration(7, _apply_v7),
    Migration(8, _apply_v8),
)
