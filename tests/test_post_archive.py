from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from voicevault.app_db import AppDatabase
from voicevault.coverage import serialize_utc
from voicevault.post_archive import (
    ArchiveIdentityConflict,
    ArchiveImportSummary,
    ArchiveIntegrityError,
    ArchiveRecord,
    ArchiveRepository,
    EvidenceMetadataConflict,
    EvidenceRecord,
    ObservationTargetNotFound,
    PostArchiveError,
    PurgedPostRejected,
    normalize_post_text,
    post_content_sha256,
)


UTC = timezone.utc
ACCEPTED_AT = datetime(2026, 7, 11, 12, tzinfo=UTC)
ARCHIVE_TABLES = (
    "posts",
    "post_revisions",
    "post_observations",
    "capture_evidence",
    "post_evidence",
    "content_dispositions",
    "collection_job_evidence",
)


def instant(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 7, day, hour, tzinfo=UTC)


class PostTextNormalizationTests(unittest.TestCase):
    def test_normalizes_unicode_line_endings_and_outer_whitespace(self) -> None:
        raw = "  Cafe\u0301\r\nsecond\rline  "

        normalized = normalize_post_text(raw)

        self.assertEqual(normalized, "Caf\u00e9\nsecond\nline")
        self.assertEqual(
            post_content_sha256(raw),
            hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        )

    def test_equivalent_nfd_crlf_and_nfc_lf_text_have_the_same_hash(self) -> None:
        self.assertEqual(
            post_content_sha256("  Cafe\u0301\r\nvoice  "),
            post_content_sha256("Caf\u00e9\nvoice"),
        )

    def test_rejects_text_that_is_empty_after_normalization(self) -> None:
        with self.assertRaises(PostArchiveError):
            normalize_post_text(" \r\n\t ")


class PostArchiveRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = AppDatabase(data_dir=Path(self.temp_dir.name))
        self.database.initialize()
        self.repository = ArchiveRepository(self.database)
        self.account_id = "account-1"
        self.job_id = "job-1"
        self.other_account_id = "account-2"
        self.other_job_id = "job-2"
        self._insert_collection_fixture(
            person_id="person-1",
            account_id=self.account_id,
            external_user_id="111",
            job_id=self.job_id,
        )
        self._insert_collection_fixture(
            person_id="person-2",
            account_id=self.other_account_id,
            external_user_id="222",
            job_id=self.other_job_id,
        )

    def _insert_collection_fixture(
        self,
        *,
        person_id: str,
        account_id: str,
        external_user_id: str,
        job_id: str,
    ) -> None:
        now = serialize_utc(ACCEPTED_AT)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO persons(person_id, display_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (person_id, person_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO platform_accounts(
                    account_id, person_id, platform, external_user_id,
                    archive_basis_confirmed_at, created_at, updated_at
                ) VALUES (?, ?, 'xueqiu', ?, ?, ?, ?)
                """,
                (account_id, person_id, external_user_id, now, now, now),
            )
            connection.execute(
                """
                INSERT INTO collection_jobs(
                    job_id, account_id, mode, status, requested_start_at,
                    requested_end_at, created_at, updated_at
                ) VALUES (?, ?, 'normal', 'running', ?, ?, ?, ?)
                """,
                (job_id, account_id, serialize_utc(instant(1)), serialize_utc(instant(10)), now, now),
            )

    def _available(
        self,
        external_post_id: str = "1001",
        *,
        content_text: str | None = "first voice",
        published_at: datetime | None = None,
        captured_at: datetime | None = None,
        canonical_url: str | None = None,
        evidence_keys: tuple[str, ...] = (),
    ) -> ArchiveRecord:
        return ArchiveRecord(
            external_post_id=external_post_id,
            published_at=published_at if published_at is not None else instant(2),
            captured_at=captured_at if captured_at is not None else instant(3),
            canonical_url=(
                canonical_url
                if canonical_url is not None
                else f"https://xueqiu.com/111/{external_post_id}"
            ),
            content_text=content_text,
            observation_status="available",
            evidence_keys=evidence_keys,
        )

    @staticmethod
    def _status_record(
        status: str,
        *,
        external_post_id: str = "1001",
        captured_at: datetime | None = None,
        published_at: datetime | None = None,
        canonical_url: str | None = None,
        content_text: str | None = None,
        evidence_keys: tuple[str, ...] = (),
    ) -> ArchiveRecord:
        return ArchiveRecord(
            external_post_id=external_post_id,
            published_at=published_at,
            captured_at=captured_at if captured_at is not None else instant(4),
            canonical_url=canonical_url,
            content_text=content_text,
            observation_status=status,
            evidence_keys=evidence_keys,
        )

    @staticmethod
    def _evidence(
        key: str = "screen",
        *,
        evidence_id: str = "evidence-a",
        sha256: str = "a" * 64,
        media_type: str = "image/png",
        byte_size: int = 12,
        relative_path: str = "evidence/sha256/aa/aaaaaaaa.png",
    ) -> EvidenceRecord:
        return EvidenceRecord(
            evidence_key=key,
            evidence_id=evidence_id,
            sha256=sha256,
            media_type=media_type,
            byte_size=byte_size,
            relative_path=relative_path,
        )

    def _import(
        self,
        records: tuple[ArchiveRecord, ...],
        *,
        evidence_by_key: dict[str, EvidenceRecord] | None = None,
        account_id: str | None = None,
        job_id: str | None = None,
        accepted_at: datetime = ACCEPTED_AT,
    ) -> ArchiveImportSummary:
        with self.database.transaction(immediate=True) as connection:
            return self.repository.import_records(
                connection,
                account_id=account_id or self.account_id,
                job_id=job_id or self.job_id,
                records=records,
                evidence_by_key=evidence_by_key or {},
                accepted_at=accepted_at,
            )

    def _counts(self) -> dict[str, int]:
        with self.database.connect() as connection:
            return {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ARCHIVE_TABLES
            }

    def test_available_replay_reuses_post_revision_observation_and_links(self) -> None:
        evidence = self._evidence()
        record = self._available(
            content_text="  Cafe\u0301\r\nvoice  ", evidence_keys=(evidence.evidence_key,)
        )

        first = self._import((record,), evidence_by_key={evidence.evidence_key: evidence})
        replay = self._import((record,), evidence_by_key={evidence.evidence_key: evidence})

        self.assertEqual(first, ArchiveImportSummary(1, 1, 0, 1))
        self.assertEqual(replay, ArchiveImportSummary(1, 0, 1, 0))
        self.assertEqual(
            self._counts(),
            {
                "posts": 1,
                "post_revisions": 1,
                "post_observations": 1,
                "capture_evidence": 1,
                "post_evidence": 1,
                "content_dispositions": 1,
                "collection_job_evidence": 0,
            },
        )
        with self.database.connect() as connection:
            stored = connection.execute(
                """
                SELECT p.external_post_id, p.published_at, p.canonical_url,
                       r.content_hash, r.content_text, r.captured_at,
                       o.status, o.observed_at, d.state,
                       pe.revision_id, pe.observation_id, pe.relation_kind
                FROM posts p
                JOIN post_revisions r ON r.post_id = p.post_id
                JOIN post_observations o ON o.post_id = p.post_id
                JOIN content_dispositions d ON d.post_id = p.post_id
                JOIN post_evidence pe ON pe.revision_id = r.revision_id
                """
            ).fetchone()
        self.assertEqual(stored["external_post_id"], record.external_post_id)
        self.assertEqual(stored["published_at"], serialize_utc(record.published_at))
        self.assertEqual(stored["canonical_url"], record.canonical_url)
        self.assertEqual(stored["content_text"], "Caf\u00e9\nvoice")
        self.assertEqual(stored["content_hash"], post_content_sha256(record.content_text))
        self.assertEqual(stored["captured_at"], serialize_utc(record.captured_at))
        self.assertEqual(stored["status"], "available")
        self.assertEqual(stored["observed_at"], serialize_utc(record.captured_at))
        self.assertEqual(stored["state"], "active")
        self.assertIsNotNone(stored["revision_id"])
        self.assertIsNone(stored["observation_id"])
        self.assertEqual(stored["relation_kind"], "content")

    def test_equivalent_text_is_duplicate_but_changed_text_appends_immutable_revision(self) -> None:
        original = self._available(content_text="  Cafe\u0301\r\nvoice  ")
        equivalent = replace(
            original,
            content_text="Caf\u00e9\nvoice",
            captured_at=instant(4),
        )
        changed = replace(equivalent, content_text="Caf\u00e9\nchanged", captured_at=instant(5))

        self.assertEqual(self._import((original,)), ArchiveImportSummary(1, 1, 0, 1))
        self.assertEqual(self._import((equivalent,)), ArchiveImportSummary(1, 0, 1, 1))
        self.assertEqual(self._import((changed,)), ArchiveImportSummary(1, 1, 0, 1))

        with self.database.connect() as connection:
            revisions = connection.execute(
                "SELECT content_hash, content_text FROM post_revisions ORDER BY rowid"
            ).fetchall()
        self.assertEqual(
            [(row["content_hash"], row["content_text"]) for row in revisions],
            [
                (post_content_sha256(original.content_text), "Caf\u00e9\nvoice"),
                (post_content_sha256(changed.content_text), "Caf\u00e9\nchanged"),
            ],
        )

    def test_post_count_uses_unique_batch_identities(self) -> None:
        record = self._available()

        summary = self._import((record, record))

        self.assertEqual(summary, ArchiveImportSummary(1, 1, 1, 1))

    def test_same_external_post_id_is_scoped_to_account(self) -> None:
        record = self._available()
        other_record = replace(record, canonical_url="https://xueqiu.com/222/1001")

        self._import((record,))
        self._import(
            (other_record,), account_id=self.other_account_id, job_id=self.other_job_id
        )

        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT account_id, external_post_id FROM posts ORDER BY account_id"
            ).fetchall()
        self.assertEqual(
            [(row["account_id"], row["external_post_id"]) for row in rows],
            [(self.account_id, "1001"), (self.other_account_id, "1001")],
        )

    def test_identity_conflicts_reject_published_at_or_url_without_overwrite(self) -> None:
        original = self._available()
        self._import((original,))
        before = self._counts()
        conflicts = (
            replace(original, published_at=instant(1), captured_at=instant(4)),
            replace(
                original,
                canonical_url="https://xueqiu.com/111/conflict",
                captured_at=instant(4),
            ),
        )

        for conflict in conflicts:
            with self.subTest(conflict=conflict):
                with self.assertRaises(ArchiveIdentityConflict):
                    self._import((conflict,))
                self.assertEqual(self._counts(), before)

        with self.database.connect() as connection:
            identity = connection.execute(
                "SELECT published_at, canonical_url FROM posts"
            ).fetchone()
        self.assertEqual(identity["published_at"], serialize_utc(original.published_at))
        self.assertEqual(identity["canonical_url"], original.canonical_url)

    def test_rejects_invalid_available_records_and_naive_datetimes(self) -> None:
        valid = self._available()
        naive = datetime(2026, 7, 3, 0)
        cases = (
            ("empty external id", replace(valid, external_post_id="  "), ACCEPTED_AT),
            ("missing published time", replace(valid, published_at=None), ACCEPTED_AT),
            ("missing URL", replace(valid, canonical_url=None), ACCEPTED_AT),
            ("empty URL", replace(valid, canonical_url="  "), ACCEPTED_AT),
            ("missing content", replace(valid, content_text=None), ACCEPTED_AT),
            ("empty normalized content", replace(valid, content_text=" \r\n "), ACCEPTED_AT),
            ("unknown status", replace(valid, observation_status="edited"), ACCEPTED_AT),
            ("naive published time", replace(valid, published_at=naive), ACCEPTED_AT),
            ("naive captured time", replace(valid, captured_at=naive), ACCEPTED_AT),
            ("naive accepted time", valid, naive),
        )

        for name, record, accepted_at in cases:
            with self.subTest(name=name):
                with self.assertRaises(PostArchiveError):
                    self._import((record,), accepted_at=accepted_at)
        self.assertEqual(self._counts(), {table: 0 for table in ARCHIVE_TABLES})

    def test_deleted_and_unavailable_require_an_existing_target(self) -> None:
        for status in ("deleted", "unavailable"):
            with self.subTest(status=status):
                with self.assertRaises(ObservationTargetNotFound):
                    self._import((self._status_record(status),))
        self.assertEqual(self._counts(), {table: 0 for table in ARCHIVE_TABLES})

    def test_status_observations_and_recovery_preserve_revisions(self) -> None:
        available = self._available()
        status_evidence = self._evidence(
            "status-proof",
            evidence_id="status-evidence",
            sha256="b" * 64,
            relative_path="evidence/sha256/bb/bbbbbbbb.png",
        )
        deleted = self._status_record(
            "deleted", captured_at=instant(4), evidence_keys=(status_evidence.evidence_key,)
        )
        unavailable = self._status_record(
            "unavailable", captured_at=instant(5), evidence_keys=(status_evidence.evidence_key,)
        )
        recovered = replace(available, captured_at=instant(6))

        self._import((available,))
        self.assertEqual(
            self._import(
                (deleted,), evidence_by_key={status_evidence.evidence_key: status_evidence}
            ),
            ArchiveImportSummary(1, 0, 0, 1),
        )
        self.assertEqual(
            self._import(
                (unavailable,), evidence_by_key={status_evidence.evidence_key: status_evidence}
            ),
            ArchiveImportSummary(1, 0, 0, 1),
        )
        self.assertEqual(self._import((recovered,)), ArchiveImportSummary(1, 0, 1, 1))

        with self.database.connect() as connection:
            revisions = connection.execute(
                "SELECT content_text FROM post_revisions"
            ).fetchall()
            observations = connection.execute(
                "SELECT status FROM post_observations ORDER BY observed_at"
            ).fetchall()
            status_links = connection.execute(
                """
                SELECT pe.revision_id, pe.observation_id, pe.relation_kind, o.status
                FROM post_evidence pe
                JOIN post_observations o ON o.observation_id = pe.observation_id
                ORDER BY o.observed_at
                """
            ).fetchall()
        self.assertEqual([row["content_text"] for row in revisions], ["first voice"])
        self.assertEqual(
            [row["status"] for row in observations],
            ["available", "deleted", "unavailable", "available"],
        )
        self.assertEqual(len(status_links), 2)
        self.assertTrue(all(row["revision_id"] is None for row in status_links))
        self.assertTrue(all(row["observation_id"] is not None for row in status_links))
        self.assertTrue(all(row["relation_kind"] == "status" for row in status_links))

    def test_status_observations_require_no_content_and_validate_optional_identity(self) -> None:
        original = self._available()
        self._import((original,))
        before = self._counts()
        invalid = (
            self._status_record("deleted", content_text="must not be present"),
            self._status_record("deleted", published_at=instant(1)),
            self._status_record(
                "unavailable", canonical_url="https://xueqiu.com/111/conflict"
            ),
        )

        for record in invalid:
            with self.subTest(record=record):
                expected = (
                    PostArchiveError
                    if record.content_text is not None
                    else ArchiveIdentityConflict
                )
                with self.assertRaises(expected):
                    self._import((record,))
        self.assertEqual(self._counts(), before)

    def test_purged_tombstone_rejects_every_new_observation_without_change(self) -> None:
        available = self._available()
        self._import((available,))
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE content_dispositions
                SET state = 'purged', reason = 'public removal request',
                    changed_at = ?, purged_content_hash = ?
                """,
                (serialize_utc(instant(4)), post_content_sha256(available.content_text)),
            )
        before = self._counts()
        records = (
            replace(available, content_text="new text", captured_at=instant(5)),
            self._status_record("deleted", captured_at=instant(5)),
            self._status_record("unavailable", captured_at=instant(5)),
        )

        for record in records:
            with self.subTest(status=record.observation_status):
                with self.assertRaises(PurgedPostRejected):
                    self._import((record,))
        self.assertEqual(self._counts(), before)
        with self.database.connect() as connection:
            state = connection.execute(
                "SELECT state FROM content_dispositions"
            ).fetchone()[0]
            content = connection.execute(
                "SELECT content_text FROM post_revisions"
            ).fetchone()[0]
        self.assertEqual(state, "purged")
        self.assertEqual(content, "first voice")

    def test_stored_hash_content_mismatch_raises_integrity_error(self) -> None:
        record = self._available()
        self._import((record,))
        with self.database.transaction() as connection:
            connection.execute("UPDATE post_revisions SET content_text = 'tampered'")
        before = self._counts()

        with self.assertRaises(ArchiveIntegrityError):
            self._import((record,))

        self.assertEqual(self._counts(), before)

    def test_same_sha_evidence_is_reused_and_shared_across_posts(self) -> None:
        first = self._evidence("first-key", evidence_id="evidence-first")
        second = replace(first, evidence_key="second-key", evidence_id="evidence-second")
        records = (
            self._available("1001", evidence_keys=(first.evidence_key,)),
            self._available("1002", evidence_keys=(second.evidence_key,)),
        )

        summary = self._import(
            records,
            evidence_by_key={first.evidence_key: first, second.evidence_key: second},
        )

        self.assertEqual(summary, ArchiveImportSummary(2, 2, 0, 2))
        with self.database.connect() as connection:
            evidence_rows = connection.execute(
                "SELECT evidence_id, sha256 FROM capture_evidence"
            ).fetchall()
            linked_ids = connection.execute(
                "SELECT evidence_id FROM post_evidence ORDER BY rowid"
            ).fetchall()
        self.assertEqual(
            [(row["evidence_id"], row["sha256"]) for row in evidence_rows],
            [("evidence-first", first.sha256)],
        )
        self.assertEqual([row["evidence_id"] for row in linked_ids], ["evidence-first"] * 2)

    def test_same_sha_with_conflicting_metadata_is_rejected(self) -> None:
        evidence = self._evidence()
        record = self._available(evidence_keys=(evidence.evidence_key,))
        self._import((record,), evidence_by_key={evidence.evidence_key: evidence})
        before = self._counts()
        conflicts = (
            replace(evidence, evidence_id="media-conflict", media_type="image/jpeg"),
            replace(evidence, evidence_id="size-conflict", byte_size=evidence.byte_size + 1),
            replace(
                evidence,
                evidence_id="path-conflict",
                relative_path="evidence/sha256/aa/other.png",
            ),
        )

        for conflict in conflicts:
            with self.subTest(conflict=conflict):
                with self.assertRaises(EvidenceMetadataConflict):
                    self._import(
                        (record,), evidence_by_key={conflict.evidence_key: conflict}
                    )
                self.assertEqual(self._counts(), before)

    def test_evidence_requires_declared_keys_valid_sha_and_safe_relative_paths(self) -> None:
        missing = self._available(evidence_keys=("missing",))
        with self.assertRaises(PostArchiveError):
            self._import((missing,))

        invalid_evidence = (
            ("blank key", {"": self._evidence("")}),
            ("key mismatch", {"screen": self._evidence("other")}),
            ("invalid SHA", {"screen": self._evidence(sha256="A" * 64)}),
            ("absolute posix path", {"screen": self._evidence(relative_path="/tmp/a.png")}),
            ("absolute windows path", {"screen": self._evidence(relative_path="C:\\tmp\\a.png")}),
            ("parent path", {"screen": self._evidence(relative_path="../a.png")}),
            ("nested parent path", {"screen": self._evidence(relative_path="safe/../a.png")}),
            ("windows parent path", {"screen": self._evidence(relative_path="safe\\..\\a.png")}),
        )
        for name, evidence_by_key in invalid_evidence:
            with self.subTest(name=name):
                with self.assertRaises(PostArchiveError):
                    self._import((), evidence_by_key=evidence_by_key)
        self.assertEqual(self._counts(), {table: 0 for table in ARCHIVE_TABLES})

    def test_evidence_path_must_be_canonical_posix_relative_path(self) -> None:
        invalid_paths = (
            ("backslash", "evidence\\a.png"),
            ("empty segment", "evidence//a.png"),
            ("dot segment", "evidence/./a.png"),
            ("parent segment", "evidence/../a.png"),
            ("trailing separator", "evidence/a.png/"),
            ("absolute path", "/evidence/a.png"),
            ("UNC path", "\\\\server\\share\\a.png"),
            ("drive path", "C:/evidence/a.png"),
            ("null byte", "evidence/\x00/a.png"),
        )

        for index, (name, relative_path) in enumerate(invalid_paths, start=1):
            with self.subTest(name=name):
                evidence = self._evidence(
                    evidence_id=f"invalid-path-{index}",
                    sha256=f"{index:x}" * 64,
                    relative_path=relative_path,
                )
                with self.assertRaises(PostArchiveError):
                    self._import((), evidence_by_key={evidence.evidence_key: evidence})
        self.assertEqual(self._counts(), {table: 0 for table in ARCHIVE_TABLES})

    def test_windows_equivalent_paths_for_different_digests_roll_back_all_archive_tables(
        self,
    ) -> None:
        canonical = self._evidence(
            "canonical",
            evidence_id="evidence-canonical",
            sha256="c" * 64,
            relative_path="evidence/a.png",
        )
        windows_alias = self._evidence(
            "windows-alias",
            evidence_id="evidence-windows-alias",
            sha256="d" * 64,
            relative_path="evidence\\a.png",
        )
        records = (
            self._available("canonical", evidence_keys=(canonical.evidence_key,)),
            self._available("windows-alias", evidence_keys=(windows_alias.evidence_key,)),
        )

        with self.assertRaises(PostArchiveError):
            self._import(
                records,
                evidence_by_key={
                    canonical.evidence_key: canonical,
                    windows_alias.evidence_key: windows_alias,
                },
            )

        self.assertEqual(
            self._counts(),
            {
                "posts": 0,
                "post_revisions": 0,
                "post_observations": 0,
                "capture_evidence": 0,
                "post_evidence": 0,
                "content_dispositions": 0,
                "collection_job_evidence": 0,
            },
        )

    def test_second_record_failure_rolls_back_every_first_record_write(self) -> None:
        existing = self._available("existing")
        self._import((existing,))
        before = self._counts()
        evidence = self._evidence()
        first = self._available("new", evidence_keys=(evidence.evidence_key,))
        conflicting_second = replace(
            existing,
            canonical_url="https://xueqiu.com/111/conflict",
            captured_at=instant(5),
        )

        with self.assertRaises(ArchiveIdentityConflict):
            self._import(
                (first, conflicting_second),
                evidence_by_key={evidence.evidence_key: evidence},
            )

        self.assertEqual(self._counts(), before)
        with self.database.connect() as connection:
            new_post = connection.execute(
                "SELECT 1 FROM posts WHERE external_post_id = 'new'"
            ).fetchone()
        self.assertIsNone(new_post)

    def test_outer_abort_rolls_back_successful_import_because_repository_never_commits(self) -> None:
        evidence = self._evidence()
        record = self._available(evidence_keys=(evidence.evidence_key,))

        with self.assertRaisesRegex(RuntimeError, "caller abort"):
            with self.database.transaction(immediate=True) as connection:
                self.repository.import_records(
                    connection,
                    account_id=self.account_id,
                    job_id=self.job_id,
                    records=(record,),
                    evidence_by_key={evidence.evidence_key: evidence},
                    accepted_at=ACCEPTED_AT,
                )
                raise RuntimeError("caller abort")

        self.assertEqual(self._counts(), {table: 0 for table in ARCHIVE_TABLES})

    def test_repository_uses_supplied_connection_without_opening_or_committing(self) -> None:
        connection = self.database.connect()
        self.addCleanup(connection.close)
        connection.execute("BEGIN IMMEDIATE")
        record = self._available()

        with patch.object(
            self.database,
            "connect",
            side_effect=AssertionError("repository opened a connection"),
        ):
            self.repository.import_records(
                connection,
                account_id=self.account_id,
                job_id=self.job_id,
                records=(record,),
                evidence_by_key={},
                accepted_at=ACCEPTED_AT,
            )

        self.assertTrue(connection.in_transaction)
        connection.rollback()
        self.assertEqual(self._counts(), {table: 0 for table in ARCHIVE_TABLES})


if __name__ == "__main__":
    unittest.main()
