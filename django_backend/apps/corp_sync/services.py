from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Iterable

from django.utils.dateparse import parse_datetime
from django.utils import timezone

from apps.common.db import get_connection, is_postgres
from apps.common.locks import advisory_lock, build_sync_lock_key
from apps.corp_sync.esi import CorporationEsiClient, parse_x_pages
from apps.corp_sync.models import CorpAssetSnapshot, CorpJobSnapshot, SyncRun


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


class CorporationSyncService:
    def __init__(
        self,
        coordinator: SyncCoordinator | None = None,
        esi_client: CorporationEsiClient | None = None,
    ):
        self.coordinator = coordinator or SyncCoordinator()
        self.esi_client = esi_client or CorporationEsiClient()

    def close(self) -> None:
        self.esi_client.close()

    def sync_assets(self, corporation_id: int, access_token: str) -> SyncRun:
        def handler() -> int:
            sync_run = SyncRun.objects.filter(kind="assets", corporation_id=corporation_id).order_by("-created_at").first()
            if sync_run is None:
                raise SyncExecutionError("Missing sync run for assets handler")
            items = self._fetch_paginated(
                f"/corporations/{corporation_id}/assets/",
                access_token,
                extra_params={},
            )
            snapshots = [
                CorpAssetSnapshot(
                    sync_run=sync_run,
                    item_id=int(item["item_id"]),
                    type_id=int(item["type_id"]),
                    location_id=int(item["location_id"]),
                    location_type=str(item.get("location_type") or ""),
                    location_flag=str(item.get("location_flag") or ""),
                    quantity=int(item.get("quantity") or 0),
                    is_singleton=bool(item.get("is_singleton")),
                    is_blueprint_copy=bool(item.get("is_blueprint_copy")),
                )
                for item in items
            ]
            CorpAssetSnapshot.objects.bulk_create(snapshots)
            return len(snapshots)

        return self.coordinator.run("assets", corporation_id, handler)

    def sync_jobs(self, corporation_id: int, access_token: str, *, include_completed: bool = True) -> SyncRun:
        def handler() -> int:
            sync_run = SyncRun.objects.filter(kind="jobs", corporation_id=corporation_id).order_by("-created_at").first()
            if sync_run is None:
                raise SyncExecutionError("Missing sync run for jobs handler")
            items = self._fetch_paginated(
                f"/corporations/{corporation_id}/industry/jobs/",
                access_token,
                extra_params={"include_completed": str(include_completed).lower()},
            )
            snapshots = [
                CorpJobSnapshot(
                    sync_run=sync_run,
                    job_id=int(item["job_id"]),
                    activity_id=int(item["activity_id"]),
                    blueprint_id=int(item["blueprint_id"]) if item.get("blueprint_id") is not None else None,
                    blueprint_type_id=int(item["blueprint_type_id"]),
                    product_type_id=int(item["product_type_id"]),
                    runs=int(item.get("runs") or 0),
                    status=str(item.get("status") or ""),
                    installer_id=int(item["installer_id"]) if item.get("installer_id") is not None else None,
                    output_location_id=(
                        int(item["output_location_id"]) if item.get("output_location_id") is not None else None
                    ),
                    start_date=_parse_esi_datetime(item.get("start_date")),
                    end_date=_parse_esi_datetime(item.get("end_date")),
                    completed_date=_parse_esi_datetime(item.get("completed_date")),
                )
                for item in items
            ]
            CorpJobSnapshot.objects.bulk_create(snapshots)
            return len(snapshots)

        return self.coordinator.run("jobs", corporation_id, handler)

    def _fetch_paginated(self, path: str, access_token: str, *, extra_params: dict[str, str]) -> list[dict]:
        page = 1
        max_page = 1
        items: list[dict] = []
        while page <= max_page:
            response = self.esi_client.get(
                path,
                access_token,
                params={"datasource": "tranquility", "page": str(page), **extra_params},
            )
            response.raise_for_status()
            max_page = parse_x_pages(response)
            items.extend(list(response.json()))
            page += 1
        return items


def _parse_esi_datetime(value: str | None):
    if not value:
        return None
    return parse_datetime(value)
