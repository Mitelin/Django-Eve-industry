from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from apps.corp_sync.models import (
    CorpAssetSnapshot,
    CorpJobSnapshot,
    SyncRun,
    WalletJournalSnapshot,
    WalletTransactionSnapshot,
)
from apps.corp_sync.services import CorporationSyncService, SyncCoordinator, SyncExecutionError


class _FakeResponse:
    def __init__(self, payload, *, headers=None):
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


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


class CorporationSyncServiceTests(TestCase):
    def test_sync_assets_paginates_and_stores_snapshots(self) -> None:
        client = MagicMock()
        client.get.side_effect = [
            _FakeResponse(
                [
                    {
                        "item_id": 1,
                        "type_id": 34,
                        "location_id": 100,
                        "location_type": "station",
                        "location_flag": "CorpDeliveries",
                        "quantity": 10,
                        "is_singleton": False,
                        "is_blueprint_copy": False,
                    }
                ],
                headers={"x-pages": "2"},
            ),
            _FakeResponse(
                [
                    {
                        "item_id": 2,
                        "type_id": 35,
                        "location_id": 101,
                        "location_type": "item",
                        "location_flag": "AutoFit",
                        "quantity": 5,
                        "is_singleton": True,
                        "is_blueprint_copy": True,
                    }
                ],
                headers={"x-pages": "2"},
            ),
        ]
        service = CorporationSyncService(esi_client=client)

        sync_run = service.sync_assets(123, "token")

        self.assertEqual(sync_run.status, "ok")
        self.assertEqual(sync_run.rows_written, 2)
        self.assertEqual(CorpAssetSnapshot.objects.filter(sync_run=sync_run).count(), 2)

    def test_sync_jobs_stores_job_snapshots(self) -> None:
        client = MagicMock()
        client.get.return_value = _FakeResponse(
            [
                {
                    "job_id": 99,
                    "activity_id": 1,
                    "blueprint_id": 1001,
                    "blueprint_type_id": 100,
                    "product_type_id": 200,
                    "runs": 4,
                    "status": "active",
                    "installer_id": 42,
                    "output_location_id": 777,
                    "start_date": "2026-03-06T10:00:00Z",
                    "end_date": "2026-03-06T12:00:00Z",
                    "completed_date": None,
                }
            ],
            headers={"x-pages": "1"},
        )
        service = CorporationSyncService(esi_client=client)

        sync_run = service.sync_jobs(123, "token")

        self.assertEqual(sync_run.status, "ok")
        self.assertEqual(sync_run.rows_written, 1)
        snapshot = CorpJobSnapshot.objects.get(sync_run=sync_run)
        self.assertEqual(snapshot.job_id, 99)
        self.assertEqual(snapshot.installer_id, 42)
        self.assertEqual(snapshot.output_location_id, 777)

    def test_sync_jobs_marks_failed_run_when_esi_errors(self) -> None:
        client = MagicMock()
        client.get.side_effect = RuntimeError("esi failure")
        service = CorporationSyncService(esi_client=client)

        with self.assertRaises(SyncExecutionError):
            service.sync_jobs(123, "token")

        sync_run = SyncRun.objects.get(kind="jobs", corporation_id=123)
        self.assertEqual(sync_run.status, "failed")
        self.assertEqual(sync_run.error_text, "esi failure")

    def test_sync_wallet_journal_stores_snapshots(self) -> None:
        client = MagicMock()
        client.get.return_value = _FakeResponse(
            [
                {
                    "id": 501,
                    "amount": 12.34,
                    "balance": 55.0,
                    "context_id": 1001,
                    "context_id_type": "market_transaction_id",
                    "date": "2026-03-06T10:00:00Z",
                    "description": "Sale",
                    "first_party_id": 9001,
                    "reason": "",
                    "ref_type": "market_transaction",
                    "second_party_id": 9002,
                    "tax": 0.5,
                    "tax_receiver_id": 9003,
                }
            ],
            headers={"x-pages": "1"},
        )
        service = CorporationSyncService(esi_client=client)

        sync_run = service.sync_wallet_journal(123, 7, "token")

        self.assertEqual(sync_run.status, "ok")
        self.assertEqual(sync_run.rows_written, 1)
        snapshot = WalletJournalSnapshot.objects.get(sync_run=sync_run)
        self.assertEqual(snapshot.entry_id, 501)
        self.assertEqual(snapshot.ref_type, "market_transaction")

    def test_sync_wallet_transactions_stores_snapshots(self) -> None:
        client = MagicMock()
        client.get.return_value = _FakeResponse(
            [
                {
                    "transaction_id": 601,
                    "client_id": 8001,
                    "date": "2026-03-06T10:00:00Z",
                    "is_buy": True,
                    "journal_ref_id": 501,
                    "location_id": 7001,
                    "quantity": 4,
                    "type_id": 34,
                    "unit_price": 4.2,
                }
            ],
            headers={"x-pages": "1"},
        )
        service = CorporationSyncService(esi_client=client)

        sync_run = service.sync_wallet_transactions(123, 7, "token")

        self.assertEqual(sync_run.status, "ok")
        self.assertEqual(sync_run.rows_written, 1)
        snapshot = WalletTransactionSnapshot.objects.get(sync_run=sync_run)
        self.assertEqual(snapshot.transaction_id, 601)
        self.assertTrue(snapshot.is_buy)
