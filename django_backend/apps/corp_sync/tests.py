from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from apps.corp_sync.models import SyncRun
from apps.corp_sync.services import SyncCoordinator, SyncExecutionError


class SyncCoordinatorTests(TestCase):
    def test_run_marks_success(self) -> None:
        coordinator = SyncCoordinator()

        sync_run = coordinator.run("jobs", 123, lambda: 7)

        self.assertEqual(sync_run.status, "ok")
        self.assertEqual(sync_run.rows_written, 7)
        self.assertIsNotNone(sync_run.finished_at)

    def test_run_marks_failure(self) -> None:
        coordinator = SyncCoordinator()

        with self.assertRaises(SyncExecutionError):
            coordinator.run("assets", 123, lambda: (_ for _ in ()).throw(RuntimeError("sync failed")))

        sync_run = SyncRun.objects.get(kind="assets", corporation_id=123)
        self.assertEqual(sync_run.status, "failed")
        self.assertEqual(sync_run.error_text, "sync failed")
        self.assertIsNotNone(sync_run.finished_at)

    @patch("apps.corp_sync.services.advisory_lock")
    @patch("apps.corp_sync.services.is_postgres")
    def test_run_uses_advisory_lock_on_postgres(self, is_postgres_mock: MagicMock, advisory_lock_mock: MagicMock) -> None:
        is_postgres_mock.return_value = True
        advisory_lock_mock.return_value.__enter__.return_value = None
        advisory_lock_mock.return_value.__exit__.return_value = False
        coordinator = SyncCoordinator()

        coordinator.run("wallet_journal", 123, lambda: 3, wallet_division=7)

        self.assertTrue(advisory_lock_mock.called)

    def test_get_freshness_is_stale_without_success(self) -> None:
        coordinator = SyncCoordinator()

        freshness = coordinator.get_freshness("jobs", 123)

        self.assertTrue(freshness.is_stale)
        self.assertIsNone(freshness.last_success_at)
        self.assertIsNone(freshness.age_seconds)

    def test_get_freshness_uses_latest_success(self) -> None:
        finished_at = timezone.now() - timedelta(minutes=15)
        SyncRun.objects.create(
            kind="jobs",
            corporation_id=123,
            status="ok",
            rows_written=10,
            finished_at=finished_at,
        )
        coordinator = SyncCoordinator()

        freshness = coordinator.get_freshness("jobs", 123, stale_after=timedelta(hours=1))

        self.assertFalse(freshness.is_stale)
        self.assertIsNotNone(freshness.last_success_at)
        self.assertGreaterEqual(freshness.age_seconds, 0)
