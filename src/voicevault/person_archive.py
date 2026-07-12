from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .app_db import AppDatabase


class ArchiveDomainError(Exception):
    """Base class for stable person-archive domain failures."""


class ArchiveStorageError(ArchiveDomainError):
    pass


class PersonNotFound(ArchiveDomainError):
    def __init__(self, person_id: str) -> None:
        self.person_id = person_id
        super().__init__(f"Person not found: {person_id}")


class AccountNotFound(ArchiveDomainError):
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        super().__init__(f"Platform account not found: {account_id}")


class InvalidExternalUserId(ArchiveDomainError):
    def __init__(self, platform: str) -> None:
        self.platform = platform
        super().__init__(f"Invalid external user ID for platform: {platform}")


class AccountOwnershipConflict(ArchiveDomainError):
    def __init__(self, platform: str, external_user_id: str, owner_person_id: str) -> None:
        self.platform = platform
        self.external_user_id = external_user_id
        self.owner_person_id = owner_person_id
        super().__init__(f"Platform account already belongs to a person: {platform}/{external_user_id}")


@dataclass(frozen=True)
class Person:
    person_id: str
    display_name: str
    aliases: tuple[str, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PlatformAccount:
    account_id: str
    person_id: str
    platform: str
    external_user_id: str
    display_name: str | None
    archive_basis_confirmed_at: str | None
    created_at: str
    updated_at: str

    @property
    def can_collect(self) -> bool:
        return self.archive_basis_confirmed_at is not None


def canonicalize_platform(platform: str) -> str:
    normalized = platform.strip().lower()
    if not normalized:
        raise ValueError("Platform is required.")
    if normalized == "snowball":
        return "xueqiu"
    return normalized


def canonicalize_external_user_id(platform: str, external_user_id: str) -> str:
    if not isinstance(external_user_id, str):
        raise InvalidExternalUserId(platform)
    normalized = external_user_id.strip()
    if platform == "xueqiu":
        valid = re.fullmatch(r"[0-9]+", normalized)
    else:
        valid = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", normalized)
    if valid is None:
        raise InvalidExternalUserId(platform)
    return normalized


class PersonRepository:
    def __init__(self, database: AppDatabase) -> None:
        self.database = database

    def create(self, display_name: str, *, aliases: Iterable[str] = ()) -> Person:
        normalized_name = _required_text(display_name, "Display name")
        normalized_aliases = tuple(dict.fromkeys(_required_text(alias, "Alias") for alias in aliases))
        person_id = str(uuid.uuid4())
        now = _now_utc()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO persons(person_id, display_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (person_id, normalized_name, now, now),
            )
            for alias in normalized_aliases:
                connection.execute(
                    "INSERT INTO person_aliases(alias_id, person_id, alias, created_at) VALUES (?, ?, ?, ?)",
                    (str(uuid.uuid4()), person_id, alias, now),
                )
        return Person(person_id, normalized_name, normalized_aliases, now, now)

    def add_alias(self, person_id: str, alias: str) -> Person:
        normalized_alias = _required_text(alias, "Alias")
        with self.database.transaction() as connection:
            self._require_row(connection, person_id)
            connection.execute(
                "INSERT OR IGNORE INTO person_aliases(alias_id, person_id, alias, created_at) VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), person_id, normalized_alias, _now_utc()),
            )
        return self.get(person_id)

    def get(self, person_id: str) -> Person:
        with self.database.connect() as connection:
            row = self._require_row(connection, person_id)
            return self._person_from_row(connection, row)

    def list(self) -> list[Person]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM persons ORDER BY created_at, rowid").fetchall()
            return [self._person_from_row(connection, row) for row in rows]

    @staticmethod
    def _require_row(connection: sqlite3.Connection, person_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM persons WHERE person_id = ?", (person_id,)).fetchone()
        if row is None:
            raise PersonNotFound(person_id)
        return row

    @staticmethod
    def _person_from_row(connection: sqlite3.Connection, row: sqlite3.Row) -> Person:
        aliases = tuple(
            alias_row[0]
            for alias_row in connection.execute(
                "SELECT alias FROM person_aliases WHERE person_id = ? ORDER BY rowid", (row["person_id"],)
            )
        )
        return Person(
            person_id=row["person_id"],
            display_name=row["display_name"],
            aliases=aliases,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class PlatformAccountRepository:
    def __init__(self, database: AppDatabase) -> None:
        self.database = database

    def bind(
        self,
        person_id: str,
        *,
        platform: str,
        external_user_id: str,
        display_name: str | None = None,
        archive_basis_confirmed_at: str | datetime | None = None,
    ) -> PlatformAccount:
        normalized_platform = canonicalize_platform(platform)
        normalized_external_id = canonicalize_external_user_id(
            normalized_platform, external_user_id
        )
        normalized_display_name = display_name.strip() if display_name and display_name.strip() else None
        confirmed_at = _normalize_utc_timestamp(archive_basis_confirmed_at)
        account_id = str(uuid.uuid4())
        now = _now_utc()
        try:
            with self.database.transaction() as connection:
                PersonRepository._require_row(connection, person_id)
                existing = self._find_account(connection, normalized_platform, normalized_external_id)
                if existing is not None:
                    return self._resolve_existing(existing, person_id, normalized_platform, normalized_external_id)
                connection.execute(
                    """
                    INSERT INTO platform_accounts(
                        account_id, person_id, platform, external_user_id, display_name,
                        archive_basis_confirmed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        person_id,
                        normalized_platform,
                        normalized_external_id,
                        normalized_display_name,
                        confirmed_at,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as error:
            with self.database.connect() as connection:
                existing = self._find_account(connection, normalized_platform, normalized_external_id)
            if existing is not None:
                return self._resolve_existing(existing, person_id, normalized_platform, normalized_external_id)
            raise ArchiveStorageError("Could not bind platform account.") from error
        return PlatformAccount(
            account_id=account_id,
            person_id=person_id,
            platform=normalized_platform,
            external_user_id=normalized_external_id,
            display_name=normalized_display_name,
            archive_basis_confirmed_at=confirmed_at,
            created_at=now,
            updated_at=now,
        )

    def list_for_person(self, person_id: str) -> list[PlatformAccount]:
        with self.database.connect() as connection:
            PersonRepository._require_row(connection, person_id)
            rows = connection.execute(
                "SELECT * FROM platform_accounts WHERE person_id = ? ORDER BY created_at, rowid", (person_id,)
            ).fetchall()
        return [_account_from_row(row) for row in rows]

    def get(self, account_id: str) -> PlatformAccount:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM platform_accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
        if row is None:
            raise AccountNotFound(account_id)
        return _account_from_row(row)

    @staticmethod
    def _find_account(
        connection: sqlite3.Connection,
        platform: str,
        external_user_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM platform_accounts WHERE platform = ? AND external_user_id = ?",
            (platform, external_user_id),
        ).fetchone()

    @staticmethod
    def _resolve_existing(
        row: sqlite3.Row,
        person_id: str,
        platform: str,
        external_user_id: str,
    ) -> PlatformAccount:
        account = _account_from_row(row)
        if account.person_id != person_id:
            raise AccountOwnershipConflict(platform, external_user_id, account.person_id) from None
        return account


def _account_from_row(row: sqlite3.Row) -> PlatformAccount:
    external_user_id = canonicalize_external_user_id(row["platform"], row["external_user_id"])
    return PlatformAccount(
        account_id=row["account_id"],
        person_id=row["person_id"],
        platform=row["platform"],
        external_user_id=external_user_id,
        display_name=row["display_name"],
        archive_basis_confirmed_at=row["archive_basis_confirmed_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _required_text(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} is required.")
    return normalized


def _normalize_utc_timestamp(value: str | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None:
        raise ValueError("Archive basis confirmation time must include a timezone.")
    return _serialize_utc(parsed)


def _now_utc() -> str:
    return _serialize_utc(datetime.now(timezone.utc))


def _serialize_utc(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    timespec = "microseconds" if utc.microsecond else "seconds"
    return utc.isoformat(timespec=timespec).replace("+00:00", "Z")
