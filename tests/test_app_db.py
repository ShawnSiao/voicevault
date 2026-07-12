from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from voicevault.app_db import AppDatabase, DEFAULT_MIGRATIONS, DatabaseVersionError, Migration, resolve_data_dir


ARCHIVE_TABLES = {
    "posts",
    "post_revisions",
    "post_observations",
    "capture_evidence",
    "post_evidence",
    "collection_job_evidence",
    "content_dispositions",
    "collection_checkpoints",
    "collection_submissions",
}

INDEX_TABLES = {
    "knowledge_chunks",
    "index_generations",
    "index_generation_chunks",
    "person_index_heads",
}

RETRIEVAL_TABLES = {
    "retrieval_runs",
    "retrieval_run_persons",
    "retrieval_evidence",
}

QUESTION_TABLES = {
    "question_runs",
    "question_run_persons",
    "question_evidence",
}

INTEGRATION_TABLES = {
    "index_jobs",
    "legacy_import_runs",
    "legacy_import_mappings",
}

EXPECTED_TABLES = {
    "schema_migrations",
    "persons",
    "person_aliases",
    "platform_accounts",
    "coverage_intervals",
    "collection_jobs",
    "collection_segments",
    "collection_handoffs",
} | ARCHIVE_TABLES | INDEX_TABLES | RETRIEVAL_TABLES | QUESTION_TABLES | INTEGRATION_TABLES

NOW = "2026-07-11T00:00:00.000000Z"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


class AppDatabaseTests(unittest.TestCase):
    @staticmethod
    def _insert_collection_fixture(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO persons VALUES (?, ?, ?, ?)",
            ("person-1", "Alice", NOW, NOW),
        )
        connection.execute(
            "INSERT INTO platform_accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("account-1", "person-1", "xueqiu", "12345", "Alice", NOW, NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO collection_jobs(
                job_id, account_id, mode, status, requested_start_at,
                requested_end_at, created_at, updated_at
            ) VALUES (?, ?, 'normal', 'pending_codex', ?, ?, ?, ?)
            """,
            (
                "job-1",
                "account-1",
                "2026-07-01T00:00:00.000000Z",
                "2026-07-02T00:00:00.000000Z",
                NOW,
                NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO collection_segments(
                segment_id, job_id, ordinal, start_at, end_at,
                status, created_at, updated_at
            ) VALUES (?, ?, 0, ?, ?, 'pending', ?, ?)
            """,
            (
                "segment-1",
                "job-1",
                "2026-07-01T00:00:00.000000Z",
                "2026-07-02T00:00:00.000000Z",
                NOW,
                NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO collection_handoffs(
                handoff_id, job_id, version, instance_id, expires_at, created_at
            ) VALUES (?, ?, 1, ?, ?, ?)
            """,
            ("handoff-1", "job-1", "instance-1", "2026-07-12T00:00:00.000000Z", NOW),
        )
        connection.execute(
            """
            INSERT INTO coverage_intervals(
                coverage_id, account_id, start_at, end_at, recorded_at, job_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "coverage-1",
                "account-1",
                "2026-07-01T00:00:00.000000Z",
                "2026-07-02T00:00:00.000000Z",
                NOW,
                "job-1",
            ),
        )

    @staticmethod
    def _insert_second_collection_job(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO platform_accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("account-2", "person-1", "xueqiu", "67890", "Alice 2", NOW, NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO collection_jobs(
                job_id, account_id, mode, status, requested_start_at,
                requested_end_at, created_at, updated_at
            ) VALUES (?, ?, 'normal', 'pending_codex', ?, ?, ?, ?)
            """,
            (
                "job-2",
                "account-2",
                "2026-07-02T00:00:00.000000Z",
                "2026-07-03T00:00:00.000000Z",
                NOW,
                NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO collection_segments(
                segment_id, job_id, ordinal, start_at, end_at,
                status, created_at, updated_at
            ) VALUES (?, ?, 0, ?, ?, 'pending', ?, ?)
            """,
            (
                "segment-2",
                "job-2",
                "2026-07-02T00:00:00.000000Z",
                "2026-07-03T00:00:00.000000Z",
                NOW,
                NOW,
            ),
        )

    @staticmethod
    def _insert_post(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT INTO posts(
                post_id, account_id, external_post_id, published_at,
                canonical_url, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("post-1", "account-1", "external-1", NOW, "https://xueqiu.com/12345/1", NOW),
        )

    def test_explicit_data_dir_is_used_for_new_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "runtime"

            database = AppDatabase(data_dir=data_dir)
            database.initialize()

            self.assertEqual(database.path, data_dir / "voicevault.db")
            self.assertTrue(database.path.is_file())

    def test_environment_data_dir_is_used_when_no_explicit_path_is_given(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "from-environment"
            with patch.dict(os.environ, {"VOICEVAULT_DATA_DIR": str(data_dir)}, clear=False):
                database = AppDatabase()
                database.initialize()

            self.assertEqual(database.path, data_dir / "voicevault.db")
            self.assertTrue(database.path.is_file())

    def test_default_data_dir_is_local_app_data_voicevault(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"LOCALAPPDATA": temp_dir}, clear=False):
                with patch.dict(os.environ, {"VOICEVAULT_DATA_DIR": ""}, clear=False):
                    self.assertEqual(resolve_data_dir(), Path(temp_dir) / "VoiceVault")

    def test_explicit_database_path_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "isolated" / "custom.sqlite"

            database = AppDatabase(db_path=db_path)
            database.initialize()

            self.assertEqual(database.path, db_path)
            self.assertTrue(db_path.is_file())

    def test_immediate_transaction_acquires_write_lock_commits_and_closes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))
            database.initialize()

            with database.transaction(immediate=True) as connection:
                transaction_connection = connection
                self.assertTrue(connection.in_transaction)
                with database.connect() as contender:
                    contender.execute("PRAGMA busy_timeout = 0")
                    with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                        contender.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO persons VALUES (?, ?, ?, ?)",
                    ("person-1", "Alice", NOW, NOW),
                )

            with database.connect() as connection:
                person_count = connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
            self.assertEqual(person_count, 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                transaction_connection.execute("SELECT 1")

    def test_immediate_transaction_rolls_back_and_closes_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))
            database.initialize()

            with self.assertRaisesRegex(RuntimeError, "abort transaction"):
                with database.transaction(immediate=True) as connection:
                    transaction_connection = connection
                    connection.execute(
                        "INSERT INTO persons VALUES (?, ?, ?, ?)",
                        ("person-1", "Alice", NOW, NOW),
                    )
                    raise RuntimeError("abort transaction")

            with database.connect() as connection:
                person_count = connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
            self.assertEqual(person_count, 0)
            with self.assertRaises(sqlite3.ProgrammingError):
                transaction_connection.execute("SELECT 1")

    def test_default_transaction_remains_deferred_and_rolls_back_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))
            database.initialize()

            with self.assertRaisesRegex(RuntimeError, "abort transaction"):
                with database.transaction() as connection:
                    self.assertFalse(connection.in_transaction)
                    connection.execute(
                        "INSERT INTO persons VALUES (?, ?, ?, ?)",
                        ("person-1", "Alice", NOW, NOW),
                    )
                    raise RuntimeError("abort transaction")

            with database.connect() as connection:
                person_count = connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
            self.assertEqual(person_count, 0)

    def test_initialization_creates_production_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))

            database.initialize()
            with database.connect() as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                migration_count = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
                person_count = connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0]

            self.assertEqual(tables, EXPECTED_TABLES)
            self.assertEqual(migration_count, 8)
            self.assertEqual(person_count, 0)

    def test_migrations_are_idempotent_and_connections_enforce_foreign_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))

            database.initialize()
            database.initialize()
            with database.connect() as connection:
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0],
                    8,
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO person_aliases(alias_id, person_id, alias, created_at) VALUES (?, ?, ?, ?)",
                        ("alias-1", "missing-person", "Nobody", "2026-07-11T00:00:00Z"),
                    )

    def test_upgrade_v1_to_v2_applies_only_new_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "voicevault.db"
            AppDatabase(db_path=db_path, migrations=DEFAULT_MIGRATIONS[:1]).initialize()

            def must_not_rerun_v1(connection: sqlite3.Connection) -> None:
                raise AssertionError("v1 migration was rerun")

            def apply_v2(connection: sqlite3.Connection) -> None:
                connection.execute("CREATE TABLE upgrade_state(value TEXT NOT NULL)")

            upgraded = AppDatabase(
                db_path=db_path,
                migrations=(Migration(1, must_not_rerun_v1), Migration(2, apply_v2)),
            )
            upgraded.initialize()

            with upgraded.connect() as connection:
                versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
                upgrade_table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'upgrade_state'"
                ).fetchone()
            self.assertEqual(versions, [1, 2])
            self.assertIsNotNone(upgrade_table)

    def test_v2_upgrade_preserves_coverage_and_adds_job_foreign_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "voicevault.db"
            v1_database = AppDatabase(db_path=db_path, migrations=DEFAULT_MIGRATIONS[:1])
            v1_database.initialize()
            with v1_database.transaction() as connection:
                connection.execute(
                    "INSERT INTO persons VALUES (?, ?, ?, ?)",
                    ("person-1", "Alice", "2026-07-11T00:00:00Z", "2026-07-11T00:00:00Z"),
                )
                connection.execute(
                    "INSERT INTO platform_accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "account-1",
                        "person-1",
                        "xueqiu",
                        "12345",
                        None,
                        "2026-07-11T00:00:00Z",
                        "2026-07-11T00:00:00Z",
                        "2026-07-11T00:00:00Z",
                    ),
                )
                connection.execute(
                    "INSERT INTO coverage_intervals VALUES (?, ?, ?, ?, ?)",
                    (
                        "coverage-1",
                        "account-1",
                        "2026-07-01T00:00:00Z",
                        "2026-07-02T00:00:00Z",
                        "2026-07-11T00:00:00Z",
                    ),
                )

            upgraded = AppDatabase(db_path=db_path)
            upgraded.initialize()

            with upgraded.connect() as connection:
                coverage = connection.execute(
                    "SELECT coverage_id, job_id FROM coverage_intervals"
                ).fetchone()
                foreign_keys = connection.execute("PRAGMA foreign_key_list(coverage_intervals)").fetchall()
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO coverage_intervals VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            "coverage-2",
                            "account-1",
                            "2026-07-02T00:00:00Z",
                            "2026-07-03T00:00:00Z",
                            "2026-07-11T00:00:00Z",
                            "missing-job",
                        ),
                    )

            self.assertEqual(tuple(coverage), ("coverage-1", None))
            self.assertTrue(
                any(
                    row["table"] == "collection_jobs"
                    and row["from"] == "job_id"
                    and row["to"] == "job_id"
                    for row in foreign_keys
                )
            )

    def test_v2_to_v3_upgrade_preserves_collection_state_and_adds_nullable_job_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "voicevault.db"
            v2_database = AppDatabase(db_path=db_path, migrations=DEFAULT_MIGRATIONS[:2])
            v2_database.initialize()
            with v2_database.transaction() as connection:
                self._insert_collection_fixture(connection)

            upgraded = AppDatabase(db_path=db_path)
            upgraded.initialize()

            with upgraded.connect() as connection:
                versions = [
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                preserved_ids = {
                    "person": connection.execute("SELECT person_id FROM persons").fetchone()[0],
                    "account": connection.execute(
                        "SELECT account_id FROM platform_accounts"
                    ).fetchone()[0],
                    "coverage": connection.execute(
                        "SELECT coverage_id FROM coverage_intervals"
                    ).fetchone()[0],
                    "job": connection.execute("SELECT job_id FROM collection_jobs").fetchone()[0],
                    "segment": connection.execute(
                        "SELECT segment_id FROM collection_segments"
                    ).fetchone()[0],
                    "handoff": connection.execute(
                        "SELECT handoff_id FROM collection_handoffs"
                    ).fetchone()[0],
                }
                new_job_columns = connection.execute(
                    """
                    SELECT last_heartbeat_at, submitted_at, result_manifest_sha256
                    FROM collection_jobs WHERE job_id = 'job-1'
                    """
                ).fetchone()

            self.assertEqual(versions, [1, 2, 3, 4, 5, 6, 7, 8])
            self.assertEqual(
                preserved_ids,
                {
                    "person": "person-1",
                    "account": "account-1",
                    "coverage": "coverage-1",
                    "job": "job-1",
                    "segment": "segment-1",
                    "handoff": "handoff-1",
                },
            )
            self.assertEqual(tuple(new_job_columns), (None, None, None))

    def test_v3_archive_indexes_are_created(self) -> None:
        required_indexes = {
            "posts_account_published_idx",
            "post_revisions_post_captured_idx",
            "post_observations_post_observed_idx",
            "post_evidence_revision_unique_idx",
            "post_evidence_observation_unique_idx",
            "collection_job_evidence_job_role_checkpoint_idx",
            "content_dispositions_state_changed_idx",
            "collection_checkpoints_job_segment_sequence_idx",
            "collection_submissions_job_accepted_idx",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))
            database.initialize()
            with database.connect() as connection:
                index_names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                }
                post_evidence_indexes = {
                    row["name"]: (row["unique"], row["partial"])
                    for row in connection.execute("PRAGMA index_list(post_evidence)")
                }
                submission_unique_columns = {
                    tuple(
                        column["name"]
                        for column in connection.execute(
                            f"PRAGMA index_info('{row['name']}')"
                        )
                    )
                    for row in connection.execute("PRAGMA index_list(collection_submissions)")
                    if row["unique"]
                }

            self.assertLessEqual(required_indexes, index_names)
            self.assertEqual(
                post_evidence_indexes["post_evidence_revision_unique_idx"],
                (1, 1),
            )
            self.assertEqual(
                post_evidence_indexes["post_evidence_observation_unique_idx"],
                (1, 1),
            )
            self.assertIn(("job_id", "handoff_version"), submission_unique_columns)
            self.assertIn(("job_id", "submission_id"), submission_unique_columns)

    def test_v3_archive_primary_keys_are_not_nullable(self) -> None:
        primary_keys = {
            "posts": "post_id",
            "post_revisions": "revision_id",
            "post_observations": "observation_id",
            "capture_evidence": "evidence_id",
            "post_evidence": "post_evidence_id",
            "collection_job_evidence": "job_evidence_id",
            "content_dispositions": "post_id",
            "collection_checkpoints": "checkpoint_id",
            "collection_submissions": "submission_id",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))
            database.initialize()
            with database.connect() as connection:
                primary_key_metadata = {
                    table: next(
                        row
                        for row in connection.execute(f"PRAGMA table_info('{table}')")
                        if row["pk"] == 1
                    )
                    for table in primary_keys
                }

            for table, column_name in primary_keys.items():
                with self.subTest(table=table):
                    self.assertEqual(primary_key_metadata[table]["name"], column_name)
                    self.assertEqual(primary_key_metadata[table]["notnull"], 1)

    def test_v3_collection_segment_composite_index_has_exact_columns(self) -> None:
        index_name = "collection_segments_job_segment_unique_idx"
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))
            database.initialize()
            with database.connect() as connection:
                indexes = {
                    row["name"]: row
                    for row in connection.execute("PRAGMA index_list(collection_segments)")
                }
                self.assertIn(index_name, indexes)
                index_columns = tuple(
                    row["name"]
                    for row in connection.execute(f"PRAGMA index_info('{index_name}')")
                )

            self.assertEqual(indexes[index_name]["unique"], 1)
            self.assertEqual(index_columns, ("job_id", "segment_id"))

    def test_v3_segment_composite_foreign_keys_have_exact_columns(self) -> None:
        def foreign_key_descriptors(
            connection: sqlite3.Connection,
            table: str,
        ) -> set[tuple[tuple[str, ...], tuple[str, ...], str, str]]:
            grouped_rows: dict[int, list[sqlite3.Row]] = {}
            for row in connection.execute(f"PRAGMA foreign_key_list('{table}')"):
                grouped_rows.setdefault(row["id"], []).append(row)
            return {
                (
                    tuple(row["from"] for row in sorted(rows, key=lambda row: row["seq"])),
                    tuple(row["to"] for row in sorted(rows, key=lambda row: row["seq"])),
                    rows[0]["table"],
                    rows[0]["on_delete"],
                )
                for rows in grouped_rows.values()
            }

        composite_segment_fk = (
            ("job_id", "segment_id"),
            ("job_id", "segment_id"),
            "collection_segments",
            "RESTRICT",
        )
        independent_job_fk = (
            ("job_id",),
            ("job_id",),
            "collection_jobs",
            "RESTRICT",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))
            database.initialize()
            with database.connect() as connection:
                descriptors = {
                    table: foreign_key_descriptors(connection, table)
                    for table in ("collection_job_evidence", "collection_checkpoints")
                }

            for table in descriptors:
                with self.subTest(table=table):
                    self.assertIn(composite_segment_fk, descriptors[table])
            self.assertIn(independent_job_fk, descriptors["collection_job_evidence"])

    def test_v3_foreign_key_delete_actions_match_archive_contract(self) -> None:
        expected_actions = {
            "posts": {("account_id", "platform_accounts", "RESTRICT")},
            "post_revisions": {
                ("post_id", "posts", "CASCADE"),
                ("first_seen_job_id", "collection_jobs", "RESTRICT"),
            },
            "post_observations": {
                ("post_id", "posts", "CASCADE"),
                ("source_job_id", "collection_jobs", "RESTRICT"),
            },
            "post_evidence": {
                ("revision_id", "post_revisions", "CASCADE"),
                ("observation_id", "post_observations", "CASCADE"),
                ("evidence_id", "capture_evidence", "RESTRICT"),
                ("job_id", "collection_jobs", "RESTRICT"),
            },
            "collection_job_evidence": {
                ("job_id", "collection_jobs", "RESTRICT"),
                ("job_id", "collection_segments", "RESTRICT"),
                ("segment_id", "collection_segments", "RESTRICT"),
                ("evidence_id", "capture_evidence", "RESTRICT"),
            },
            "content_dispositions": {("post_id", "posts", "CASCADE")},
            "collection_checkpoints": {
                ("job_id", "collection_jobs", "RESTRICT"),
                ("job_id", "collection_segments", "RESTRICT"),
                ("segment_id", "collection_segments", "RESTRICT"),
            },
            "collection_submissions": {("job_id", "collection_jobs", "RESTRICT")},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))
            database.initialize()
            with database.connect() as connection:
                actual_actions = {
                    table: {
                        (row["from"], row["table"], row["on_delete"])
                        for row in connection.execute(f"PRAGMA foreign_key_list('{table}')")
                    }
                    for table in expected_actions
                }

            self.assertEqual(actual_actions, expected_actions)

    def test_v3_collection_job_evidence_rejects_segment_from_another_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))
            database.initialize()
            with database.transaction() as connection:
                self._insert_collection_fixture(connection)
                self._insert_second_collection_job(connection)
                connection.execute(
                    "INSERT INTO capture_evidence VALUES (?, ?, ?, ?, ?, ?)",
                    ("evidence-1", HASH_A, "image/png", 10, "evidence/aa/file.png", NOW),
                )

                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO collection_job_evidence(
                            job_evidence_id, job_id, segment_id, evidence_id,
                            evidence_role, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        ("job-evidence-cross-job", "job-1", "segment-2", "evidence-1", "manifest", NOW),
                    )

                connection.execute(
                    """
                    INSERT INTO collection_job_evidence(
                        job_evidence_id, job_id, segment_id, evidence_id,
                        evidence_role, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("job-evidence-no-segment", "job-1", None, "evidence-1", "manifest", NOW),
                )
                stored_segment = connection.execute(
                    """
                    SELECT segment_id FROM collection_job_evidence
                    WHERE job_evidence_id = 'job-evidence-no-segment'
                    """
                ).fetchone()[0]
                self.assertIsNone(stored_segment)

    def test_v3_collection_checkpoint_rejects_segment_from_another_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))
            database.initialize()
            with database.transaction() as connection:
                self._insert_collection_fixture(connection)
                self._insert_second_collection_job(connection)

                def insert_checkpoint(checkpoint_id: str, job_id: str, segment_id: str) -> None:
                    connection.execute(
                        """
                        INSERT INTO collection_checkpoints(
                            checkpoint_id, job_id, segment_id, sequence, observed_at,
                            action_type, triggered_remote_load, remote_action_ordinal,
                            visible_post_ids_json, earliest_non_pinned_at,
                            latest_non_pinned_at, anchor_post_id, start_kind,
                            completion_reason, boundary_post_id, canonical_json, created_at
                        ) VALUES (?, ?, ?, 0, ?, 'initial', 0, NULL, '[]', NULL,
                                  NULL, NULL, NULL, NULL, NULL, '{}', ?)
                        """,
                        (checkpoint_id, job_id, segment_id, NOW, NOW),
                    )

                with self.assertRaises(sqlite3.IntegrityError):
                    insert_checkpoint("checkpoint-cross-job", "job-1", "segment-2")

                insert_checkpoint("checkpoint-valid", "job-1", "segment-1")
                stored_segment = connection.execute(
                    """
                    SELECT segment_id FROM collection_checkpoints
                    WHERE checkpoint_id = 'checkpoint-valid'
                    """
                ).fetchone()[0]
                self.assertEqual(stored_segment, "segment-1")

    def test_v3_posts_revisions_and_job_result_hash_enforce_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))
            database.initialize()
            with database.transaction() as connection:
                self._insert_collection_fixture(connection)

                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?)",
                        ("post-missing-account", "missing-account", "external-x", NOW, None, NOW),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?)",
                        ("post-blank-external", "account-1", "   ", NOW, None, NOW),
                    )

                self._insert_post(connection)
                connection.execute(
                    "INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?)",
                    ("post-nullables", "account-1", "external-2", None, None, NOW),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?)",
                        ("post-duplicate", "account-1", "external-1", NOW, None, NOW),
                    )

                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, ?)",
                        ("revision-missing-post", "missing-post", HASH_A, "body", NOW, "job-1"),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, ?)",
                        ("revision-missing-job", "post-1", HASH_B, "body", NOW, "missing-job"),
                    )
                for ordinal, invalid_hash in enumerate(("a" * 63, "A" * 64, "g" * 64)):
                    with self.subTest(invalid_hash=invalid_hash):
                        with self.assertRaises(sqlite3.IntegrityError):
                            connection.execute(
                                "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, ?)",
                                (
                                    f"revision-invalid-hash-{ordinal}",
                                    "post-1",
                                    invalid_hash,
                                    "body",
                                    NOW,
                                    "job-1",
                                ),
                            )

                connection.execute(
                    "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, ?)",
                    ("revision-1", "post-1", HASH_A, "body", NOW, "job-1"),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, ?)",
                        ("revision-duplicate", "post-1", HASH_A, "changed", NOW, "job-1"),
                    )

                for invalid_hash in ("b" * 63, "B" * 64, "z" * 64):
                    with self.subTest(result_manifest_sha256=invalid_hash):
                        with self.assertRaises(sqlite3.IntegrityError):
                            connection.execute(
                                """
                                UPDATE collection_jobs SET result_manifest_sha256 = ?
                                WHERE job_id = 'job-1'
                                """,
                                (invalid_hash,),
                            )
                connection.execute(
                    """
                    UPDATE collection_jobs
                    SET last_heartbeat_at = ?, submitted_at = ?, result_manifest_sha256 = ?
                    WHERE job_id = 'job-1'
                    """,
                    (NOW, NOW, HASH_B),
                )

                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "DELETE FROM platform_accounts WHERE account_id = 'account-1'"
                    )

    def test_v3_sha256_checks_require_exact_lowercase_hex_text_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))
            database.initialize()
            with database.transaction() as connection:
                self._insert_collection_fixture(connection)
                self._insert_post(connection)
                connection.execute(
                    "INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?)",
                    ("post-active", "account-1", "external-active", NOW, None, NOW),
                )

                def assert_rejected(
                    statement: str,
                    parameters: tuple[object, ...],
                    cleanup_statement: str,
                    cleanup_parameters: tuple[object, ...],
                ) -> None:
                    try:
                        connection.execute(statement, parameters)
                    except sqlite3.IntegrityError:
                        return
                    connection.execute(cleanup_statement, cleanup_parameters)
                    self.fail("invalid SHA-256 value was accepted")

                invalid_hashes = (
                    ("blob", sqlite3.Binary(b"a" * 64)),
                    ("embedded-nul", "a" * 64 + "\0g"),
                )
                for label, invalid_hash in invalid_hashes:
                    with self.subTest(column="post_revisions.content_hash", value=label):
                        assert_rejected(
                            "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, ?)",
                            (f"revision-{label}", "post-1", invalid_hash, "body", NOW, "job-1"),
                            "DELETE FROM post_revisions WHERE revision_id = ?",
                            (f"revision-{label}",),
                        )
                    with self.subTest(column="capture_evidence.sha256", value=label):
                        assert_rejected(
                            "INSERT INTO capture_evidence VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                f"evidence-{label}",
                                invalid_hash,
                                "image/png",
                                10,
                                f"evidence/{label}.png",
                                NOW,
                            ),
                            "DELETE FROM capture_evidence WHERE evidence_id = ?",
                            (f"evidence-{label}",),
                        )
                    with self.subTest(
                        column="content_dispositions.purged_content_hash",
                        value=label,
                    ):
                        assert_rejected(
                            "INSERT INTO content_dispositions VALUES (?, ?, ?, ?, ?)",
                            ("post-1", "purged", "privacy", NOW, invalid_hash),
                            "DELETE FROM content_dispositions WHERE post_id = ?",
                            ("post-1",),
                        )
                    with self.subTest(
                        column="collection_submissions.manifest_sha256",
                        value=label,
                    ):
                        assert_rejected(
                            """
                            INSERT INTO collection_submissions(
                                submission_id, job_id, handoff_version, collector_id,
                                manifest_sha256, accepted_manifest_json, receipt_json,
                                outcome_kind, accepted_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                f"submission-{label}",
                                "job-1",
                                1,
                                "collector-1",
                                invalid_hash,
                                "{}",
                                "{}",
                                "complete",
                                NOW,
                            ),
                            "DELETE FROM collection_submissions WHERE submission_id = ?",
                            (f"submission-{label}",),
                        )
                    with self.subTest(
                        column="collection_jobs.result_manifest_sha256",
                        value=label,
                    ):
                        assert_rejected(
                            """
                            UPDATE collection_jobs SET result_manifest_sha256 = ?
                            WHERE job_id = 'job-1'
                            """,
                            (invalid_hash,),
                            """
                            UPDATE collection_jobs SET result_manifest_sha256 = NULL
                            WHERE job_id = 'job-1'
                            """,
                            (),
                        )

                connection.execute(
                    "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, ?)",
                    ("revision-valid", "post-1", HASH_A, "body", NOW, "job-1"),
                )
                connection.execute(
                    "INSERT INTO capture_evidence VALUES (?, ?, ?, ?, ?, ?)",
                    ("evidence-valid", HASH_A, "image/png", 10, "evidence/valid.png", NOW),
                )
                connection.execute(
                    "INSERT INTO content_dispositions VALUES (?, ?, ?, ?, ?)",
                    ("post-1", "purged", "privacy", NOW, HASH_A),
                )
                connection.execute(
                    "INSERT INTO content_dispositions VALUES (?, ?, ?, ?, ?)",
                    ("post-active", "active", None, NOW, None),
                )
                connection.execute(
                    """
                    INSERT INTO collection_submissions(
                        submission_id, job_id, handoff_version, collector_id,
                        manifest_sha256, accepted_manifest_json, receipt_json,
                        outcome_kind, accepted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "submission-valid",
                        "job-1",
                        1,
                        "collector-1",
                        HASH_A,
                        "{}",
                        "{}",
                        "complete",
                        NOW,
                    ),
                )
                connection.execute(
                    """
                    UPDATE collection_jobs SET result_manifest_sha256 = ?
                    WHERE job_id = 'job-1'
                    """,
                    (HASH_A,),
                )

                valid_hashes = (
                    connection.execute(
                        "SELECT content_hash FROM post_revisions WHERE revision_id = 'revision-valid'"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT sha256 FROM capture_evidence WHERE evidence_id = 'evidence-valid'"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT purged_content_hash FROM content_dispositions WHERE post_id = 'post-1'"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT manifest_sha256 FROM collection_submissions WHERE submission_id = 'submission-valid'"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT result_manifest_sha256 FROM collection_jobs WHERE job_id = 'job-1'"
                    ).fetchone()[0],
                )
                self.assertEqual(valid_hashes, (HASH_A,) * 5)

                connection.execute(
                    """
                    UPDATE collection_jobs SET result_manifest_sha256 = NULL
                    WHERE job_id = 'job-1'
                    """
                )
                nullable_hashes = (
                    connection.execute(
                        "SELECT purged_content_hash FROM content_dispositions WHERE post_id = 'post-active'"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT result_manifest_sha256 FROM collection_jobs WHERE job_id = 'job-1'"
                    ).fetchone()[0],
                )
                self.assertEqual(nullable_hashes, (None, None))

    def test_v3_observations_and_capture_evidence_enforce_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))
            database.initialize()
            with database.transaction() as connection:
                self._insert_collection_fixture(connection)
                self._insert_post(connection)

                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO post_observations VALUES (?, ?, ?, ?, ?)",
                        ("observation-missing-post", "missing-post", "available", NOW, "job-1"),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO post_observations VALUES (?, ?, ?, ?, ?)",
                        ("observation-missing-job", "post-1", "available", NOW, "missing-job"),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO post_observations VALUES (?, ?, ?, ?, ?)",
                        ("observation-bad-status", "post-1", "unknown", NOW, "job-1"),
                    )

                connection.execute(
                    "INSERT INTO post_observations VALUES (?, ?, ?, ?, ?)",
                    ("observation-1", "post-1", "available", NOW, "job-1"),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO post_observations VALUES (?, ?, ?, ?, ?)",
                        ("observation-duplicate", "post-1", "available", NOW, "job-1"),
                    )

                for ordinal, invalid_hash in enumerate(("a" * 63, "A" * 64, "g" * 64)):
                    with self.subTest(evidence_sha256=invalid_hash):
                        with self.assertRaises(sqlite3.IntegrityError):
                            connection.execute(
                                "INSERT INTO capture_evidence VALUES (?, ?, ?, ?, ?, ?)",
                                (
                                    f"evidence-invalid-hash-{ordinal}",
                                    invalid_hash,
                                    "image/png",
                                    10,
                                    f"evidence/invalid-{ordinal}",
                                    NOW,
                                ),
                            )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO capture_evidence VALUES (?, ?, ?, ?, ?, ?)",
                        ("evidence-blank-media", HASH_B, "   ", 10, "evidence/blank-media", NOW),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO capture_evidence VALUES (?, ?, ?, ?, ?, ?)",
                        ("evidence-negative-size", HASH_B, "image/png", -1, "evidence/negative", NOW),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO capture_evidence VALUES (?, ?, ?, ?, ?, ?)",
                        ("evidence-blank-path", HASH_B, "image/png", 10, "   ", NOW),
                    )

                connection.execute(
                    "INSERT INTO capture_evidence VALUES (?, ?, ?, ?, ?, ?)",
                    ("evidence-1", HASH_A, "image/png", 10, "evidence/aa/file.png", NOW),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO capture_evidence VALUES (?, ?, ?, ?, ?, ?)",
                        ("evidence-duplicate-sha", HASH_A, "image/png", 11, "evidence/bb/file.png", NOW),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO capture_evidence VALUES (?, ?, ?, ?, ?, ?)",
                        ("evidence-duplicate-path", HASH_B, "image/png", 11, "evidence/aa/file.png", NOW),
                    )

    def test_v3_post_and_job_evidence_enforce_targets_and_uniqueness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))
            database.initialize()
            with database.transaction() as connection:
                self._insert_collection_fixture(connection)
                self._insert_post(connection)
                connection.execute(
                    "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, ?)",
                    ("revision-1", "post-1", HASH_A, "body", NOW, "job-1"),
                )
                connection.execute(
                    "INSERT INTO post_observations VALUES (?, ?, ?, ?, ?)",
                    ("observation-1", "post-1", "available", NOW, "job-1"),
                )
                connection.execute(
                    "INSERT INTO capture_evidence VALUES (?, ?, ?, ?, ?, ?)",
                    ("evidence-1", HASH_B, "image/png", 10, "evidence/bb/file.png", NOW),
                )

                for post_evidence_id, revision_id, observation_id in (
                    ("post-evidence-no-target", None, None),
                    ("post-evidence-two-targets", "revision-1", "observation-1"),
                ):
                    with self.subTest(post_evidence_id=post_evidence_id):
                        with self.assertRaises(sqlite3.IntegrityError):
                            connection.execute(
                                "INSERT INTO post_evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (
                                    post_evidence_id,
                                    revision_id,
                                    observation_id,
                                    "evidence-1",
                                    "job-1",
                                    "screenshot",
                                    NOW,
                                ),
                            )
                for values in (
                    ("post-evidence-missing-revision", "missing-revision", None, "evidence-1", "job-1"),
                    ("post-evidence-missing-observation", None, "missing-observation", "evidence-1", "job-1"),
                    ("post-evidence-missing-evidence", "revision-1", None, "missing-evidence", "job-1"),
                    ("post-evidence-missing-job", "revision-1", None, "evidence-1", "missing-job"),
                ):
                    with self.subTest(post_evidence_id=values[0]):
                        with self.assertRaises(sqlite3.IntegrityError):
                            connection.execute(
                                "INSERT INTO post_evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (*values, "screenshot", NOW),
                            )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO post_evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            "post-evidence-blank-kind",
                            "revision-1",
                            None,
                            "evidence-1",
                            "job-1",
                            "   ",
                            NOW,
                        ),
                    )

                connection.execute(
                    "INSERT INTO post_evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("post-evidence-revision", "revision-1", None, "evidence-1", "job-1", "screenshot", NOW),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO post_evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
                        ("post-evidence-revision-duplicate", "revision-1", None, "evidence-1", "job-1", "screenshot", NOW),
                    )
                connection.execute(
                    "INSERT INTO post_evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("post-evidence-observation", None, "observation-1", "evidence-1", "job-1", "screenshot", NOW),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO post_evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
                        ("post-evidence-observation-duplicate", None, "observation-1", "evidence-1", "job-1", "screenshot", NOW),
                    )

                for values in (
                    ("job-evidence-missing-job", "missing-job", None, "evidence-1", "manifest"),
                    ("job-evidence-missing-segment", "job-1", "missing-segment", "evidence-1", "manifest"),
                    ("job-evidence-missing-evidence", "job-1", None, "missing-evidence", "manifest"),
                ):
                    with self.subTest(job_evidence_id=values[0]):
                        with self.assertRaises(sqlite3.IntegrityError):
                            connection.execute(
                                """
                                INSERT INTO collection_job_evidence(
                                    job_evidence_id, job_id, segment_id, evidence_id,
                                    evidence_role, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?)
                                """,
                                (*values, NOW),
                            )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO collection_job_evidence(
                            job_evidence_id, job_id, segment_id, evidence_id,
                            evidence_role, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        ("job-evidence-blank-role", "job-1", None, "evidence-1", "   ", NOW),
                    )

                connection.execute(
                    """
                    INSERT INTO collection_job_evidence(
                        job_evidence_id, job_id, segment_id, evidence_id,
                        evidence_role, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("job-evidence-1", "job-1", "segment-1", "evidence-1", "manifest", NOW),
                )
                checkpoint_key = connection.execute(
                    "SELECT checkpoint_key FROM collection_job_evidence WHERE job_evidence_id = 'job-evidence-1'"
                ).fetchone()[0]
                self.assertEqual(checkpoint_key, "")
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO collection_job_evidence(
                            job_evidence_id, job_id, segment_id, evidence_id,
                            evidence_role, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        ("job-evidence-duplicate", "job-1", None, "evidence-1", "manifest", NOW),
                    )

                connection.execute("DELETE FROM post_revisions WHERE revision_id = 'revision-1'")
                remaining_targets = connection.execute(
                    "SELECT revision_id, observation_id FROM post_evidence"
                ).fetchall()
                self.assertEqual(
                    [tuple(row) for row in remaining_targets],
                    [(None, "observation-1")],
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "DELETE FROM capture_evidence WHERE evidence_id = 'evidence-1'"
                    )

    def test_v3_dispositions_checkpoints_and_submissions_enforce_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))
            database.initialize()
            with database.transaction() as connection:
                self._insert_collection_fixture(connection)
                self._insert_post(connection)

                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO content_dispositions VALUES (?, ?, ?, ?, ?)",
                        ("missing-post", "active", None, NOW, None),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO content_dispositions VALUES (?, ?, ?, ?, ?)",
                        ("post-1", "unknown", None, NOW, None),
                    )
                for state, reason, purged_hash in (
                    ("suppressed", None, None),
                    ("suppressed", "   ", None),
                    ("purged", None, HASH_A),
                    ("purged", "privacy", None),
                    ("purged", "privacy", "A" * 64),
                    ("purged", "privacy", "g" * 64),
                    ("purged", "privacy", "a" * 63),
                ):
                    with self.subTest(state=state, reason=reason, purged_hash=purged_hash):
                        with self.assertRaises(sqlite3.IntegrityError):
                            connection.execute(
                                "INSERT INTO content_dispositions VALUES (?, ?, ?, ?, ?)",
                                ("post-1", state, reason, NOW, purged_hash),
                            )
                connection.execute(
                    "INSERT INTO content_dispositions VALUES (?, ?, ?, ?, ?)",
                    ("post-1", "active", None, NOW, None),
                )

                def insert_checkpoint(
                    checkpoint_id: str,
                    *,
                    job_id: str = "job-1",
                    segment_id: str | None = "segment-1",
                    sequence: int = 0,
                    action_type: str = "initial",
                    triggered_remote_load: int = 0,
                    remote_action_ordinal: int | None = None,
                ) -> None:
                    connection.execute(
                        """
                        INSERT INTO collection_checkpoints(
                            checkpoint_id, job_id, segment_id, sequence, observed_at,
                            action_type, triggered_remote_load, remote_action_ordinal,
                            visible_post_ids_json, earliest_non_pinned_at,
                            latest_non_pinned_at, anchor_post_id, start_kind,
                            completion_reason, boundary_post_id, canonical_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            checkpoint_id,
                            job_id,
                            segment_id,
                            sequence,
                            NOW,
                            action_type,
                            triggered_remote_load,
                            remote_action_ordinal,
                            '["external-1"]',
                            NOW,
                            NOW,
                            "external-1",
                            "top",
                            None,
                            None,
                            "{}",
                            NOW,
                        ),
                    )

                for checkpoint_id, overrides in (
                    ("checkpoint-missing-job", {"job_id": "missing-job"}),
                    ("checkpoint-null-segment", {"segment_id": None}),
                    ("checkpoint-missing-segment", {"segment_id": "missing-segment"}),
                    ("checkpoint-negative-sequence", {"sequence": -1}),
                    ("checkpoint-blank-action", {"action_type": "   "}),
                    ("checkpoint-bad-trigger", {"triggered_remote_load": 2}),
                    ("checkpoint-bad-ordinal", {"remote_action_ordinal": 0}),
                ):
                    with self.subTest(checkpoint_id=checkpoint_id):
                        with self.assertRaises(sqlite3.IntegrityError):
                            insert_checkpoint(checkpoint_id, **overrides)
                insert_checkpoint("checkpoint-1")
                with self.assertRaises(sqlite3.IntegrityError):
                    insert_checkpoint("checkpoint-duplicate")

                def insert_submission(
                    submission_id: str,
                    *,
                    job_id: str = "job-1",
                    handoff_version: int = 1,
                    collector_id: str = "collector-1",
                    manifest_sha256: str = HASH_C,
                    outcome_kind: str = "complete",
                ) -> None:
                    connection.execute(
                        """
                        INSERT INTO collection_submissions(
                            submission_id, job_id, handoff_version, collector_id,
                            manifest_sha256, accepted_manifest_json, receipt_json,
                            outcome_kind, accepted_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            submission_id,
                            job_id,
                            handoff_version,
                            collector_id,
                            manifest_sha256,
                            "{}",
                            "{}",
                            outcome_kind,
                            NOW,
                        ),
                    )

                invalid_submissions = (
                    ("submission-missing-job", {"job_id": "missing-job"}),
                    ("submission-bad-version", {"handoff_version": 0}),
                    ("submission-blank-collector", {"collector_id": "   "}),
                    ("submission-short-hash", {"manifest_sha256": "c" * 63}),
                    ("submission-upper-hash", {"manifest_sha256": "C" * 64}),
                    ("submission-nonhex-hash", {"manifest_sha256": "z" * 64}),
                    ("submission-bad-outcome", {"outcome_kind": "unknown"}),
                )
                for submission_id, overrides in invalid_submissions:
                    with self.subTest(submission_id=submission_id):
                        with self.assertRaises(sqlite3.IntegrityError):
                            insert_submission(submission_id, **overrides)
                insert_submission("submission-1")
                with self.assertRaises(sqlite3.IntegrityError):
                    insert_submission("submission-duplicate-handoff", handoff_version=1)
                with self.assertRaises(sqlite3.IntegrityError):
                    insert_submission("submission-1", handoff_version=2)

    def test_v2_collection_segment_status_has_database_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))
            database.initialize()
            with database.transaction() as connection:
                connection.execute(
                    "INSERT INTO persons VALUES (?, ?, ?, ?)",
                    ("person-1", "Alice", "2026-07-11T00:00:00Z", "2026-07-11T00:00:00Z"),
                )
                connection.execute(
                    "INSERT INTO platform_accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "account-1",
                        "person-1",
                        "xueqiu",
                        "12345",
                        None,
                        "2026-07-11T00:00:00Z",
                        "2026-07-11T00:00:00Z",
                        "2026-07-11T00:00:00Z",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO collection_jobs(
                        job_id, account_id, mode, status, requested_start_at,
                        requested_end_at, created_at, updated_at
                    ) VALUES (?, ?, 'normal', 'pending_codex', ?, ?, ?, ?)
                    """,
                    (
                        "job-1",
                        "account-1",
                        "2026-07-01T00:00:00.000000Z",
                        "2026-07-02T00:00:00.000000Z",
                        "2026-07-11T00:00:00.000000Z",
                        "2026-07-11T00:00:00.000000Z",
                    ),
                )

                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO collection_segments(
                            segment_id, job_id, ordinal, start_at, end_at,
                            status, created_at, updated_at
                        ) VALUES (?, ?, 0, ?, ?, 'bogus', ?, ?)
                        """,
                        (
                            "segment-1",
                            "job-1",
                            "2026-07-01T00:00:00.000000Z",
                            "2026-07-02T00:00:00.000000Z",
                            "2026-07-11T00:00:00.000000Z",
                            "2026-07-11T00:00:00.000000Z",
                        ),
                    )

    def test_failed_v3_migration_leaves_no_version_or_partial_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "voicevault.db"
            AppDatabase(db_path=db_path, migrations=DEFAULT_MIGRATIONS[:2]).initialize()

            def failing_v3(connection: sqlite3.Connection) -> None:
                DEFAULT_MIGRATIONS[2].apply(connection)
                connection.execute("CREATE TABLE partial_state(value TEXT NOT NULL)")
                raise RuntimeError("migration failed")

            upgraded = AppDatabase(
                db_path=db_path,
                migrations=(
                    Migration(1, lambda connection: None),
                    Migration(2, lambda connection: None),
                    Migration(3, failing_v3),
                ),
            )

            with self.assertRaisesRegex(RuntimeError, "migration failed"):
                upgraded.initialize()

            with upgraded.connect() as connection:
                versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
                table_names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                job_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(collection_jobs)")
                }
            self.assertEqual(versions, [1, 2])
            self.assertTrue(ARCHIVE_TABLES.isdisjoint(table_names))
            self.assertNotIn("partial_state", table_names)
            self.assertTrue(
                {"last_heartbeat_at", "submitted_at", "result_manifest_sha256"}.isdisjoint(
                    job_columns
                )
            )

    def test_v3_to_v4_upgrade_preserves_archive_rows_and_adds_index_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "voicevault.db"
            v3_database = AppDatabase(db_path=db_path, migrations=DEFAULT_MIGRATIONS[:3])
            v3_database.initialize()
            with v3_database.transaction() as connection:
                self._insert_collection_fixture(connection)
                self._insert_post(connection)
                connection.execute(
                    "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, ?)",
                    ("revision-1", "post-1", HASH_A, "preserved body", NOW, "job-1"),
                )
                connection.execute(
                    "INSERT INTO content_dispositions VALUES (?, 'active', NULL, ?, NULL)",
                    ("post-1", NOW),
                )

            upgraded = AppDatabase(db_path=db_path)
            upgraded.initialize()
            upgraded.initialize()

            with upgraded.connect() as connection:
                versions = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                )
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                preserved = connection.execute(
                    "SELECT content_text FROM post_revisions WHERE revision_id = 'revision-1'"
                ).fetchone()[0]
                triggers = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                    )
                }

            self.assertEqual(versions, (1, 2, 3, 4, 5, 6, 7, 8))
            self.assertLessEqual(INDEX_TABLES, tables)
            self.assertEqual(preserved, "preserved body")
            self.assertLessEqual(
                {
                    "index_generation_chunks_person_insert",
                    "index_generation_chunks_person_update",
                },
                triggers,
            )

    def test_v4_indexes_foreign_keys_and_composite_generation_owner_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))
            database.initialize()
            with database.connect() as connection:
                indexes = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                }
                chunk_fks = {
                    (row["from"], row["table"], row["to"], row["on_delete"])
                    for row in connection.execute("PRAGMA foreign_key_list(knowledge_chunks)")
                }
                generation_chunk_fks = {
                    (row["from"], row["table"], row["to"], row["on_delete"])
                    for row in connection.execute(
                        "PRAGMA foreign_key_list(index_generation_chunks)"
                    )
                }
                head_rows = connection.execute(
                    "PRAGMA foreign_key_list(person_index_heads)"
                ).fetchall()
                generation_uniques = {
                    tuple(
                        column["name"]
                        for column in connection.execute(
                            f"PRAGMA index_info('{row['name']}')"
                        )
                    )
                    for row in connection.execute("PRAGMA index_list(index_generations)")
                    if row["unique"]
                }

            self.assertLessEqual(
                {
                    "knowledge_chunks_revision_rule_offsets_idx",
                    "index_generations_person_status_idx",
                    "index_generation_chunks_chunk_idx",
                },
                indexes,
            )
            self.assertEqual(
                chunk_fks,
                {("revision_id", "post_revisions", "revision_id", "CASCADE")},
            )
            self.assertEqual(
                generation_chunk_fks,
                {
                    ("generation_id", "index_generations", "generation_id", "CASCADE"),
                    ("chunk_id", "knowledge_chunks", "chunk_id", "CASCADE"),
                },
            )
            composite_head = [row for row in head_rows if row["table"] == "index_generations"]
            self.assertEqual(
                tuple((row["from"], row["to"]) for row in sorted(composite_head, key=lambda row: row["seq"])),
                (("generation_id", "generation_id"), ("person_id", "person_id")),
            )
            self.assertIn(("generation_id", "person_id"), generation_uniques)

    def test_v4_chunk_generation_and_head_constraints_reject_invalid_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))
            database.initialize()
            with database.transaction() as connection:
                self._insert_collection_fixture(connection)
                self._insert_post(connection)
                connection.execute(
                    "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, ?)",
                    ("revision-1", "post-1", HASH_A, "body", NOW, "job-1"),
                )
                valid_chunk = (
                    "chunk-1", "revision-1", "paragraph-window-v1", 0, 0, 4, "body", HASH_B,
                )
                connection.execute(
                    "INSERT INTO knowledge_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)", valid_chunk
                )
                invalid_chunks = (
                    ("chunk-bad-offset", "revision-1", "paragraph-window-v1", 1, 4, 4, "x", HASH_B),
                    ("chunk-bad-hash", "revision-1", "paragraph-window-v1", 1, 4, 5, "x", "A" * 64),
                    ("chunk-blank", "revision-1", "paragraph-window-v1", 1, 4, 5, "", HASH_C),
                    ("chunk-dup-ordinal", "revision-1", "paragraph-window-v1", 0, 4, 5, "x", HASH_C),
                    ("chunk-dup-offset", "revision-1", "paragraph-window-v1", 2, 0, 4, "body", HASH_C),
                )
                for values in invalid_chunks:
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            "INSERT INTO knowledge_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)", values
                        )

                connection.execute(
                    """
                    INSERT INTO index_generations(
                        generation_id, person_id, chunk_rule_version, status,
                        retrieval_mode, created_at
                    ) VALUES ('generation-1', 'person-1', 'paragraph-window-v1',
                              'pending', 'none', ?)
                    """,
                    (NOW,),
                )
                for column, value in (("status", "unknown"), ("retrieval_mode", "semantic_only")):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            f"UPDATE index_generations SET {column} = ? WHERE generation_id = 'generation-1'",
                            (value,),
                        )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE index_generations SET embedding_dimension = 0 WHERE generation_id = 'generation-1'"
                    )
                connection.execute(
                    "INSERT INTO index_generation_chunks VALUES ('generation-1', 'chunk-1')"
                )
                connection.execute(
                    "INSERT INTO person_index_heads VALUES ('person-1', 'generation-1', ?)",
                    (NOW,),
                )
                connection.execute(
                    """
                    INSERT INTO index_generations(
                        generation_id, person_id, chunk_rule_version, status,
                        retrieval_mode, created_at
                    ) VALUES ('generation-2', 'person-1', 'paragraph-window-v1',
                              'pending', 'none', ?)
                    """,
                    (NOW,),
                )
                connection.execute("INSERT INTO persons VALUES ('person-2', 'Bob', ?, ?)", (NOW, NOW))
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO person_index_heads VALUES ('person-2', 'generation-2', ?)",
                        (NOW,),
                    )

    def test_v4_generation_chunks_enforce_person_ownership_on_insert_and_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = AppDatabase(data_dir=Path(temp_dir))
            database.initialize()
            with database.transaction() as connection:
                self._insert_collection_fixture(connection)
                self._insert_post(connection)
                connection.execute(
                    "INSERT INTO post_revisions VALUES ('revision-1', 'post-1', ?, 'one', ?, 'job-1')",
                    (HASH_A, NOW),
                )
                connection.execute(
                    "INSERT INTO knowledge_chunks VALUES ('chunk-1', 'revision-1', 'paragraph-window-v1', 0, 0, 3, 'one', ?)",
                    (HASH_A,),
                )
                connection.execute("INSERT INTO persons VALUES ('person-2', 'Bob', ?, ?)", (NOW, NOW))
                connection.execute(
                    """
                    INSERT INTO platform_accounts(
                        account_id, person_id, platform, external_user_id,
                        archive_basis_confirmed_at, created_at, updated_at
                    ) VALUES ('account-2', 'person-2', 'xueqiu', '67890', ?, ?, ?)
                    """,
                    (NOW, NOW, NOW),
                )
                connection.execute(
                    "INSERT INTO posts VALUES ('post-2', 'account-2', 'external-2', ?, NULL, ?)",
                    (NOW, NOW),
                )
                connection.execute(
                    "INSERT INTO post_revisions VALUES ('revision-2', 'post-2', ?, 'two', ?, NULL)",
                    (HASH_B, NOW),
                )
                connection.execute(
                    "INSERT INTO knowledge_chunks VALUES ('chunk-2', 'revision-2', 'paragraph-window-v1', 0, 0, 3, 'two', ?)",
                    (HASH_B,),
                )
                for generation_id, person_id in (
                    ("generation-1", "person-1"),
                    ("generation-2", "person-2"),
                ):
                    connection.execute(
                        """
                        INSERT INTO index_generations(
                            generation_id, person_id, chunk_rule_version,
                            status, retrieval_mode, created_at
                        ) VALUES (?, ?, 'paragraph-window-v1', 'pending', 'none', ?)
                        """,
                        (generation_id, person_id, NOW),
                    )

                connection.execute(
                    "INSERT INTO index_generation_chunks VALUES ('generation-1', 'chunk-1')"
                )
                connection.execute(
                    "INSERT INTO index_generation_chunks VALUES ('generation-2', 'chunk-2')"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO index_generation_chunks VALUES ('generation-1', 'chunk-2')"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        UPDATE index_generation_chunks SET chunk_id = 'chunk-2'
                        WHERE generation_id = 'generation-1' AND chunk_id = 'chunk-1'
                        """
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        UPDATE index_generation_chunks SET generation_id = 'generation-2'
                        WHERE generation_id = 'generation-1' AND chunk_id = 'chunk-1'
                        """
                    )
                rows = connection.execute(
                    "SELECT generation_id, chunk_id FROM index_generation_chunks ORDER BY generation_id"
                ).fetchall()

            self.assertEqual(
                [tuple(row) for row in rows],
                [("generation-1", "chunk-1"), ("generation-2", "chunk-2")],
            )

    def test_failed_v4_migration_leaves_no_version_or_partial_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "voicevault.db"
            AppDatabase(db_path=db_path, migrations=DEFAULT_MIGRATIONS[:3]).initialize()

            def failing_v4(connection: sqlite3.Connection) -> None:
                DEFAULT_MIGRATIONS[3].apply(connection)
                connection.execute("CREATE TABLE partial_v4(value TEXT NOT NULL)")
                raise RuntimeError("v4 migration failed")

            upgraded = AppDatabase(
                db_path=db_path,
                migrations=DEFAULT_MIGRATIONS[:3] + (Migration(4, failing_v4),),
            )
            with self.assertRaisesRegex(RuntimeError, "v4 migration failed"):
                upgraded.initialize()
            with upgraded.connect() as connection:
                versions = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                )
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertEqual(versions, (1, 2, 3))
            self.assertTrue(INDEX_TABLES.isdisjoint(tables))
            self.assertNotIn("partial_v4", tables)

    def test_v4_to_v5_upgrade_is_lossless_and_adds_retrieval_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "voicevault.db"
            v4_database = AppDatabase(db_path=db_path, migrations=DEFAULT_MIGRATIONS[:4])
            v4_database.initialize()
            with v4_database.transaction() as connection:
                self._insert_collection_fixture(connection)
                self._insert_post(connection)
                connection.execute(
                    "INSERT INTO post_revisions VALUES ('revision-1', 'post-1', ?, 'body', ?, 'job-1')",
                    (HASH_A, NOW),
                )
                connection.execute(
                    "INSERT INTO knowledge_chunks VALUES ('chunk-1', 'revision-1', 'paragraph-window-v1', 0, 0, 4, 'body', ?)",
                    (HASH_A,),
                )

            upgraded = AppDatabase(db_path=db_path)
            upgraded.initialize()
            upgraded.initialize()
            with upgraded.connect() as connection:
                versions = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                )
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                body = connection.execute(
                    "SELECT content_text FROM post_revisions WHERE revision_id = 'revision-1'"
                ).fetchone()[0]
                triggers = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                    )
                }

            self.assertEqual(versions, (1, 2, 3, 4, 5, 6, 7, 8))
            self.assertLessEqual(RETRIEVAL_TABLES, tables)
            self.assertEqual(body, "body")
            self.assertLessEqual(
                {
                    "retrieval_evidence_lineage_insert",
                    "retrieval_evidence_lineage_update",
                    "retrieval_run_persons_immutable_update",
                    "retrieval_run_persons_mode_update",
                },
                triggers,
            )

    def test_failed_v5_migration_rolls_back_version_and_all_partial_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "voicevault.db"
            AppDatabase(db_path=db_path, migrations=DEFAULT_MIGRATIONS[:4]).initialize()

            def failing_v5(connection: sqlite3.Connection) -> None:
                DEFAULT_MIGRATIONS[4].apply(connection)
                connection.execute("CREATE TABLE partial_v5(value TEXT NOT NULL)")
                raise RuntimeError("v5 migration failed")

            upgraded = AppDatabase(
                db_path=db_path,
                migrations=DEFAULT_MIGRATIONS[:4] + (Migration(5, failing_v5),),
            )
            with self.assertRaisesRegex(RuntimeError, "v5 migration failed"):
                upgraded.initialize()
            with upgraded.connect() as connection:
                versions = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                )
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }

            self.assertEqual(versions, (1, 2, 3, 4))
            self.assertTrue(RETRIEVAL_TABLES.isdisjoint(tables))
            self.assertNotIn("partial_v5", tables)

    def test_v5_to_v6_upgrade_is_lossless_and_failure_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "voicevault.db"
            v5 = AppDatabase(db_path=db_path, migrations=DEFAULT_MIGRATIONS[:5])
            v5.initialize()
            with v5.transaction() as connection:
                connection.execute(
                    "INSERT INTO persons VALUES ('person-v5', 'Preserved', ?, ?)",
                    (NOW, NOW),
                )

            upgraded = AppDatabase(db_path=db_path)
            upgraded.initialize()
            with upgraded.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT display_name FROM persons WHERE person_id = 'person-v5'"
                    ).fetchone()[0],
                    "Preserved",
                )
                self.assertLessEqual(
                    QUESTION_TABLES,
                    {
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    },
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "voicevault.db"
            AppDatabase(db_path=db_path, migrations=DEFAULT_MIGRATIONS[:5]).initialize()

            def failing_v6(connection: sqlite3.Connection) -> None:
                DEFAULT_MIGRATIONS[5].apply(connection)
                connection.execute("CREATE TABLE partial_v6(value TEXT NOT NULL)")
                raise RuntimeError("v6 migration failed")

            broken = AppDatabase(
                db_path=db_path,
                migrations=DEFAULT_MIGRATIONS[:5] + (Migration(6, failing_v6),),
            )
            with self.assertRaisesRegex(RuntimeError, "v6 migration failed"):
                broken.initialize()
            with broken.connect() as connection:
                versions = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                )
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertEqual(versions, (1, 2, 3, 4, 5))
            self.assertTrue(QUESTION_TABLES.isdisjoint(tables))
            self.assertNotIn("partial_v6", tables)

    def test_v6_to_v7_upgrade_adds_integration_schema_and_failure_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "voicevault.db"
            AppDatabase(db_path=db_path, migrations=DEFAULT_MIGRATIONS[:6]).initialize()
            upgraded = AppDatabase(db_path=db_path)
            upgraded.initialize()
            with upgraded.connect() as connection:
                versions = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                )
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertEqual(versions, (1, 2, 3, 4, 5, 6, 7, 8))
            self.assertLessEqual(INTEGRATION_TABLES, tables)

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "voicevault.db"
            AppDatabase(db_path=db_path, migrations=DEFAULT_MIGRATIONS[:6]).initialize()

            def failing_v7(connection: sqlite3.Connection) -> None:
                DEFAULT_MIGRATIONS[6].apply(connection)
                connection.execute("CREATE TABLE partial_v7(value TEXT NOT NULL)")
                raise RuntimeError("v7 migration failed")

            broken = AppDatabase(
                db_path=db_path,
                migrations=DEFAULT_MIGRATIONS[:6] + (Migration(7, failing_v7),),
            )
            with self.assertRaisesRegex(RuntimeError, "v7 migration failed"):
                broken.initialize()
            with broken.connect() as connection:
                versions = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                )
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertEqual(versions, (1, 2, 3, 4, 5, 6))
            self.assertTrue(INTEGRATION_TABLES.isdisjoint(tables))
            self.assertNotIn("partial_v7", tables)

    def test_newer_database_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "voicevault.db"
            newer = AppDatabase(
                db_path=db_path,
                migrations=DEFAULT_MIGRATIONS
                + (Migration(9, lambda connection: connection.execute("CREATE TABLE future_state(value TEXT)")),),
            )
            newer.initialize()

            with self.assertRaises(DatabaseVersionError) as raised:
                AppDatabase(db_path=db_path).initialize()

            self.assertEqual(raised.exception.database_version, 9)
            self.assertEqual(raised.exception.code_version, 8)


if __name__ == "__main__":
    unittest.main()
