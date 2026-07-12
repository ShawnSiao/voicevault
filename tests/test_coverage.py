from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from voicevault.app_db import AppDatabase
from voicevault.coverage import (
    CoverageAccountNotFound,
    CoverageAccountUnconfirmed,
    CoverageRepository,
    UtcInterval,
    insert_validated_complete,
    merge_intervals,
    page_date_range_to_utc,
    serialize_utc,
    subtract_intervals,
)
from voicevault.person_archive import PersonRepository, PlatformAccountRepository


def instant(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 7, day, hour, tzinfo=timezone.utc)


class CoverageMathTests(unittest.TestCase):
    def test_inclusive_shanghai_day_becomes_utc_half_open_interval(self) -> None:
        interval = page_date_range_to_utc("2026-07-11", "2026-07-11")

        self.assertEqual(serialize_utc(interval.start_at), "2026-07-10T16:00:00Z")
        self.assertEqual(serialize_utc(interval.end_at), "2026-07-11T16:00:00Z")

    def test_multi_day_leap_range_includes_the_final_page_day(self) -> None:
        interval = page_date_range_to_utc("2024-02-29", "2024-03-01")

        self.assertEqual(serialize_utc(interval.start_at), "2024-02-28T16:00:00Z")
        self.assertEqual(serialize_utc(interval.end_at), "2024-03-01T16:00:00Z")

    def test_reverse_page_date_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "End date must not be before start date"):
            page_date_range_to_utc("2026-07-12", "2026-07-11")

    def test_overlapping_and_adjacent_intervals_are_merged_in_stable_order(self) -> None:
        intervals = [
            UtcInterval(instant(5), instant(7)),
            UtcInterval(instant(1), instant(3)),
            UtcInterval(instant(3), instant(5)),
            UtcInterval(instant(8), instant(9)),
            UtcInterval(instant(8, 12), instant(10)),
        ]

        self.assertEqual(
            merge_intervals(intervals),
            [UtcInterval(instant(1), instant(7)), UtcInterval(instant(8), instant(10))],
        )

    def test_covered_ranges_are_clipped_and_subtracted_from_request(self) -> None:
        requested = UtcInterval(instant(2), instant(10))
        covered = [
            UtcInterval(instant(1), instant(3)),
            UtcInterval(instant(4), instant(6)),
            UtcInterval(instant(6), instant(8)),
            UtcInterval(instant(9), instant(11)),
        ]

        self.assertEqual(
            subtract_intervals(requested, covered),
            [UtcInterval(instant(3), instant(4)), UtcInterval(instant(8), instant(9))],
        )


class CoverageRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = AppDatabase(data_dir=Path(self.temp_dir.name))
        self.database.initialize()
        person = PersonRepository(self.database).create("Alice")
        accounts = PlatformAccountRepository(self.database)
        self.account = accounts.bind(
            person.person_id,
            platform="xueqiu",
            external_user_id="12345",
            archive_basis_confirmed_at="2026-07-11T00:00:00Z",
        )
        self.unconfirmed_account = accounts.bind(
            person.person_id,
            platform="xueqiu",
            external_user_id="67890",
        )
        self.coverage = CoverageRepository(self.database)

    def test_no_written_proof_means_the_entire_request_is_missing(self) -> None:
        requested = UtcInterval(instant(1), instant(5))

        self.assertEqual(self.coverage.missing(self.account.account_id, requested), [requested])

    def test_unconfirmed_account_cannot_record_complete(self) -> None:
        requested = UtcInterval(instant(1), instant(5))

        with self.assertRaises(CoverageAccountUnconfirmed):
            self.coverage.record_validated_complete(self.unconfirmed_account.account_id, requested)

        self.assertEqual(self.coverage.missing(self.unconfirmed_account.account_id, requested), [requested])

    def test_missing_for_unknown_account_raises(self) -> None:
        with self.assertRaises(CoverageAccountNotFound):
            self.coverage.missing("missing-account", UtcInterval(instant(1), instant(5)))

    def test_merged_for_unknown_account_raises(self) -> None:
        with self.assertRaises(CoverageAccountNotFound):
            self.coverage.merged("missing-account")

    def test_record_for_unknown_account_raises(self) -> None:
        with self.assertRaises(CoverageAccountNotFound):
            self.coverage.record_validated_complete("missing-account", UtcInterval(instant(1), instant(5)))

    def test_production_coverage_schema_has_collection_job_reference(self) -> None:
        with self.database.connect() as connection:
            columns = connection.execute("PRAGMA table_info(coverage_intervals)").fetchall()

        self.assertIn("job_id", {row["name"] for row in columns})

    def test_explicit_complete_intervals_are_idempotent_merged_and_subtracted(self) -> None:
        first = UtcInterval(instant(1), instant(3))
        second = UtcInterval(instant(3), instant(5))
        requested = UtcInterval(instant(1), instant(7))

        self.coverage.record_validated_complete(self.account.account_id, first)
        self.coverage.record_validated_complete(self.account.account_id, first)
        self.coverage.record_validated_complete(self.account.account_id, second)

        self.assertEqual(self.coverage.merged(self.account.account_id), [UtcInterval(instant(1), instant(5))])
        self.assertEqual(self.coverage.missing(self.account.account_id, requested), [UtcInterval(instant(5), instant(7))])

    def test_connection_primitive_records_job_and_never_commits_outer_transaction(self) -> None:
        job_id = "coverage-job"
        now = instant(11)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO collection_jobs(
                    job_id, account_id, mode, status, requested_start_at,
                    requested_end_at, created_at, updated_at
                ) VALUES (?, ?, 'normal', 'running', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    self.account.account_id,
                    instant(1).isoformat(),
                    instant(5).isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        interval = UtcInterval(instant(1), instant(3))

        with self.database.transaction(immediate=True) as connection:
            self.assertTrue(
                insert_validated_complete(
                    connection,
                    account_id=self.account.account_id,
                    interval=interval,
                    job_id=job_id,
                    recorded_at=now,
                )
            )
            self.assertFalse(
                insert_validated_complete(
                    connection,
                    account_id=self.account.account_id,
                    interval=interval,
                    job_id=job_id,
                    recorded_at=now,
                )
            )
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT job_id FROM coverage_intervals WHERE account_id = ?",
                (self.account.account_id,),
            ).fetchone()
        self.assertEqual(row["job_id"], job_id)

        rolled_back = UtcInterval(instant(3), instant(5))
        with self.assertRaisesRegex(RuntimeError, "outer abort"):
            with self.database.transaction(immediate=True) as connection:
                insert_validated_complete(
                    connection,
                    account_id=self.account.account_id,
                    interval=rolled_back,
                    job_id=job_id,
                    recorded_at=now,
                )
                raise RuntimeError("outer abort")
        self.assertEqual(self.coverage.missing(self.account.account_id, UtcInterval(instant(1), instant(5))), [rolled_back])


if __name__ == "__main__":
    unittest.main()
