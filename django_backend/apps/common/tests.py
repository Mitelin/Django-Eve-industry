from __future__ import annotations

from io import StringIO
from unittest import TestCase
from unittest.mock import MagicMock, patch

from django.core.management import CommandError, call_command

from apps.common.db import DatabaseConfigurationError, is_postgres, require_postgres
from apps.common.locks import (
    AdvisoryLockError,
    AdvisoryLockKey,
    advisory_lock,
    advisory_unlock,
    build_advisory_lock_key,
    build_sync_lock_key,
    build_verify_lock_key,
    try_advisory_lock,
)


class _FakeCursor:
    def __init__(self, responses: list[tuple[bool]]):
        self.responses = responses
        self.executed: list[tuple[str, list[int]]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params: list[int]) -> None:
        self.executed.append((sql, params))

    def fetchone(self):
        return self.responses.pop(0)


class _FakeConnection:
    def __init__(self, vendor: str, responses: list[tuple[bool]]):
        self.vendor = vendor
        self._cursor = _FakeCursor(responses)

    def cursor(self):
        return self._cursor


class AdvisoryLockTests(TestCase):
    def test_build_advisory_lock_key_is_stable(self) -> None:
        first = build_advisory_lock_key("sync", "jobs", 123)
        second = build_advisory_lock_key("sync", "jobs", 123)
        self.assertEqual(first, second)

    def test_build_advisory_lock_key_changes_with_scope(self) -> None:
        sync_key = build_sync_lock_key("jobs", 123)
        verify_key = build_verify_lock_key("jobs", 123)
        self.assertNotEqual(sync_key, verify_key)

    def test_try_advisory_lock_uses_pg_function(self) -> None:
        connection = _FakeConnection("postgresql", [(True,)])
        key = AdvisoryLockKey(group_id=11, resource_id=22)

        acquired = try_advisory_lock(connection, key)

        self.assertTrue(acquired)
        self.assertEqual(
            connection._cursor.executed,
            [("SELECT pg_try_advisory_lock(%s, %s)", [11, 22])],
        )

    def test_advisory_unlock_uses_pg_function(self) -> None:
        connection = _FakeConnection("postgresql", [(True,)])
        key = AdvisoryLockKey(group_id=33, resource_id=44)

        released = advisory_unlock(connection, key)

        self.assertTrue(released)
        self.assertEqual(
            connection._cursor.executed,
            [("SELECT pg_advisory_unlock(%s, %s)", [33, 44])],
        )

    def test_advisory_lock_context_unlocks_after_use(self) -> None:
        connection = _FakeConnection("postgresql", [(True,), (True,)])
        key = AdvisoryLockKey(group_id=55, resource_id=66)

        with advisory_lock(connection, key) as held_key:
            self.assertEqual(held_key, key)

        self.assertEqual(
            connection._cursor.executed,
            [
                ("SELECT pg_try_advisory_lock(%s, %s)", [55, 66]),
                ("SELECT pg_advisory_unlock(%s, %s)", [55, 66]),
            ],
        )

    def test_try_advisory_lock_rejects_non_postgres(self) -> None:
        connection = _FakeConnection("sqlite", [])

        with self.assertRaises(AdvisoryLockError):
            try_advisory_lock(connection, AdvisoryLockKey(group_id=1, resource_id=2))


class DatabaseHelperTests(TestCase):
    @patch("apps.common.db.get_connection")
    def test_is_postgres_true_for_postgres_vendor(self, get_connection_mock: MagicMock) -> None:
        get_connection_mock.return_value.vendor = "postgresql"

        self.assertTrue(is_postgres())

    @patch("apps.common.db.get_connection")
    def test_require_postgres_raises_for_non_postgres(self, get_connection_mock: MagicMock) -> None:
        get_connection_mock.return_value.vendor = "sqlite"

        with self.assertRaises(DatabaseConfigurationError):
            require_postgres()


class CheckPostgresLocksCommandTests(TestCase):
    @patch("apps.common.management.commands.check_postgres_locks.advisory_lock")
    @patch("apps.common.management.commands.check_postgres_locks.require_postgres")
    def test_command_reports_success(self, require_postgres_mock: MagicMock, advisory_lock_mock: MagicMock) -> None:
        require_postgres_mock.return_value = object()
        advisory_lock_mock.return_value.__enter__.return_value = None
        advisory_lock_mock.return_value.__exit__.return_value = False
        stdout = StringIO()

        call_command("check_postgres_locks", stdout=stdout)

        self.assertIn("Advisory lock OK", stdout.getvalue())

    @patch("apps.common.management.commands.check_postgres_locks.require_postgres")
    def test_command_raises_on_configuration_error(self, require_postgres_mock: MagicMock) -> None:
        require_postgres_mock.side_effect = DatabaseConfigurationError("PostgreSQL is required")

        with self.assertRaisesRegex(CommandError, "PostgreSQL is required"):
            call_command("check_postgres_locks")
