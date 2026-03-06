from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from django.utils import timezone

from apps.common.db import get_connection, is_postgres
from apps.common.locks import advisory_lock, build_sync_lock_key
from apps.corp_sync.models import SyncRun


class SyncExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class SyncFreshness:
    kind: str
    corporation_id: int
    wallet_division: int | None
    last_success_at: object | None
    age_seconds: int | None
    is_stale: bool


class SyncCoordinator:
    def __init__(self, connection_alias: str = "default"):
        self.connection_alias = connection_alias

    def run(
        self,
        kind: str,
        corporation_id: int,
        handler: Callable[[], int],
        *,
        wallet_division: int | None = None,
    ) -> SyncRun:
        sync_run = SyncRun.objects.create(
            kind=kind,
            corporation_id=corporation_id,
            wallet_division=wallet_division,
            status="started",
        )
        connection = get_connection(self.connection_alias)
        lock_key = build_sync_lock_key(kind, corporation_id, wallet_division or 0)
        lock_context = advisory_lock(connection, lock_key) if is_postgres(self.connection_alias) else nullcontext()

        try:
            with lock_context:
                rows_written = int(handler())
        except Exception as exc:
            sync_run.status = "failed"
            sync_run.error_text = str(exc)
            sync_run.finished_at = timezone.now()
            sync_run.save(update_fields=["status", "error_text", "finished_at", "updated_at"])
            raise SyncExecutionError(str(exc)) from exc

        sync_run.status = "ok"
        sync_run.rows_written = rows_written
        sync_run.finished_at = timezone.now()
        sync_run.error_text = ""
        sync_run.save(update_fields=["status", "rows_written", "finished_at", "error_text", "updated_at"])
        return sync_run

    def get_freshness(
        self,
        kind: str,
        corporation_id: int,
        *,
        wallet_division: int | None = None,
        stale_after: timedelta = timedelta(hours=1),
    ) -> SyncFreshness:
        latest = (
            SyncRun.objects.filter(
                kind=kind,
                corporation_id=corporation_id,
                wallet_division=wallet_division,
                status="ok",
            )
            .order_by("-finished_at", "-created_at")
            .first()
        )
        if latest is None or latest.finished_at is None:
            return SyncFreshness(
                kind=kind,
                corporation_id=corporation_id,
                wallet_division=wallet_division,
                last_success_at=None,
                age_seconds=None,
                is_stale=True,
            )

        age = timezone.now() - latest.finished_at
        return SyncFreshness(
            kind=kind,
            corporation_id=corporation_id,
            wallet_division=wallet_division,
            last_success_at=latest.finished_at,
            age_seconds=max(int(age.total_seconds()), 0),
            is_stale=age > stale_after,
        )
