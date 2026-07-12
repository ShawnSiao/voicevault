from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from voicevault.app_db import AppDatabase
from voicevault import person_archive
from voicevault.person_archive import (
    AccountNotFound,
    AccountOwnershipConflict,
    ArchiveStorageError,
    PersonRepository,
    PlatformAccountRepository,
)


class PersonArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = AppDatabase(data_dir=Path(self.temp_dir.name))
        self.database.initialize()
        self.persons = PersonRepository(self.database)
        self.accounts = PlatformAccountRepository(self.database)

    def test_create_and_list_person_with_aliases(self) -> None:
        person = self.persons.create("  Alice Chen  ", aliases=["Alice", "陈爱丽"])

        listed = self.persons.list()

        self.assertEqual(person.display_name, "Alice Chen")
        self.assertEqual(person.aliases, ("Alice", "陈爱丽"))
        self.assertEqual(listed, [person])

    def test_add_alias_returns_updated_person_without_duplicate_rows(self) -> None:
        person = self.persons.create("Alice Chen")

        updated = self.persons.add_alias(person.person_id, "Alice")
        unchanged = self.persons.add_alias(person.person_id, "Alice")

        self.assertEqual(updated.aliases, ("Alice",))
        self.assertEqual(unchanged.aliases, ("Alice",))

    def test_one_person_can_bind_multiple_accounts_and_unconfirmed_is_explicit(self) -> None:
        person = self.persons.create("Alice Chen")

        xueqiu = self.accounts.bind(
            person.person_id,
            platform="xueqiu",
            external_user_id="12345",
            display_name="Alice on Xueqiu",
        )
        rss = self.accounts.bind(
            person.person_id,
            platform="rss",
            external_user_id="alice-feed",
            archive_basis_confirmed_at="2026-07-11T02:03:04Z",
        )

        self.assertIsNone(xueqiu.archive_basis_confirmed_at)
        self.assertFalse(xueqiu.can_collect)
        self.assertEqual(rss.archive_basis_confirmed_at, "2026-07-11T02:03:04Z")
        self.assertTrue(rss.can_collect)
        self.assertEqual(self.accounts.list_for_person(person.person_id), [xueqiu, rss])

    def test_account_can_be_read_by_id_with_stable_missing_error(self) -> None:
        person = self.persons.create("Alice")
        account = self.accounts.bind(
            person.person_id,
            platform="xueqiu",
            external_user_id="12345",
            archive_basis_confirmed_at="2026-07-11T00:00:00Z",
        )

        self.assertEqual(self.accounts.get(account.account_id), account)
        with self.assertRaises(AccountNotFound):
            self.accounts.get("missing-account")

    def test_snowball_is_canonicalized_and_global_account_ownership_conflicts_are_stable(self) -> None:
        alice = self.persons.create("Alice")
        bob = self.persons.create("Bob")
        account = self.accounts.bind(alice.person_id, platform="snowball", external_user_id=" 12345 ")

        with self.assertRaises(AccountOwnershipConflict) as raised:
            self.accounts.bind(bob.person_id, platform="XUEQIU", external_user_id="12345")

        self.assertEqual(account.platform, "xueqiu")
        self.assertEqual(account.external_user_id, "12345")
        self.assertEqual(raised.exception.platform, "xueqiu")
        self.assertEqual(raised.exception.external_user_id, "12345")
        self.assertEqual(raised.exception.owner_person_id, alice.person_id)
        self.assertIsNone(raised.exception.__cause__)

    def test_xueqiu_external_user_id_is_trimmed_numeric_and_rejects_unsafe_values(self) -> None:
        person = self.persons.create("Alice")

        account = self.accounts.bind(
            person.person_id, platform="snowball", external_user_id=" 0012345 "
        )

        self.assertEqual(account.platform, "xueqiu")
        self.assertEqual(account.external_user_id, "0012345")
        for unsafe in (
            "https://xueqiu.com/u/123?cookie=bad",
            "/u/123",
            "123?cookie=bad",
            "123; rm -rf",
            "123 cookie=bad",
            "１２３",
        ):
            with self.subTest(external_user_id=unsafe):
                with self.assertRaises(person_archive.InvalidExternalUserId):
                    self.accounts.bind(
                        person.person_id, platform="xueqiu", external_user_id=unsafe
                    )

    def test_future_platform_external_user_id_uses_conservative_opaque_rule(self) -> None:
        person = self.persons.create("Alice")

        account = self.accounts.bind(
            person.person_id, platform="weibo", external_user_id=" alice.feed-1_2 "
        )

        self.assertEqual(account.external_user_id, "alice.feed-1_2")
        for unsafe in (
            "alice feed",
            "../alice",
            "alice/path",
            "alice\\path",
            "alice?cookie=bad",
            "alice;whoami",
            "https://weibo.com/alice",
            "alice\ncommand",
        ):
            with self.subTest(external_user_id=unsafe):
                with self.assertRaises(person_archive.InvalidExternalUserId):
                    self.accounts.bind(
                        person.person_id, platform="weibo", external_user_id=unsafe
                    )

    def test_rebinding_same_account_returns_existing_account(self) -> None:
        person = self.persons.create("Alice")
        existing = self.accounts.bind(
            person.person_id,
            platform="snowball",
            external_user_id="12345",
            display_name="Original",
        )

        retried = self.accounts.bind(
            person.person_id,
            platform="XUEQIU",
            external_user_id="12345",
            display_name="Must not overwrite",
            archive_basis_confirmed_at="2026-07-11T00:00:00Z",
        )

        self.assertEqual(retried, existing)
        self.assertEqual(self.accounts.list_for_person(person.person_id), [existing])

    def test_account_id_constraint_is_not_mapped_to_ownership_conflict(self) -> None:
        person = self.persons.create("Alice")
        existing = self.accounts.bind(person.person_id, platform="xueqiu", external_user_id="111")

        with patch("voicevault.person_archive.uuid.uuid4", return_value=existing.account_id):
            with self.assertRaises(ArchiveStorageError) as raised:
                self.accounts.bind(person.person_id, platform="xueqiu", external_user_id="222")

        self.assertIsInstance(raised.exception.__cause__, sqlite3.IntegrityError)


if __name__ == "__main__":
    unittest.main()
