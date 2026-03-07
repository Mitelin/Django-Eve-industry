from __future__ import annotations

from typing import Any

from apps.accounts.models import Character
from apps.corp_sync.models import SyncRun
from apps.corp_sync.services import SyncCoordinator
from apps.industry_planner.models import Project
from apps.industry_planner.shadow import generate_shadow_planner_report
from apps.workforce.models import WorkEvent, WorkItem
from apps.workforce.services import WorkforceService


def _build_recent_workforce_event_sources(*, limit: int = 25) -> dict[str, int]:
    summary = {
        "total": 0,
        "recommended": 0,
        "manual": 0,
        "system": 0,
    }
    for event in WorkEvent.objects.order_by("-created_at", "-id")[:limit]:
        summary["total"] += 1
        source = (event.details or {}).get("source")
        if source == "recommended_action":
            summary["recommended"] += 1
        elif source == "manual_action":
            summary["manual"] += 1
        else:
            summary["system"] += 1
    return summary


def generate_shadow_summary_report() -> dict[str, Any]:
    planner = generate_shadow_planner_report()["planner"]
    sync_coordinator = SyncCoordinator()
    workforce_service = WorkforceService(sync_coordinator=sync_coordinator)
    incidents: list[dict[str, Any]] = []

    corporation_ids = sorted(
        {
            int(corporation_id)
            for corporation_id in Character.objects.exclude(corporation_id__isnull=True).values_list(
                "corporation_id", flat=True
            )
            if corporation_id is not None
        }
    )

    sync_kinds = ["assets", "jobs", "wallet_journal", "wallet_transactions"]
    sync_corporations: list[dict[str, Any]] = []
    stale_sync_count = 0
    for corporation_id in corporation_ids:
        by_kind: dict[str, Any] = {}
        for kind in sync_kinds:
            freshness = sync_coordinator.get_freshness(kind, corporation_id)
            latest_run = (
                SyncRun.objects.filter(kind=kind, corporation_id=corporation_id)
                .order_by("-created_at", "-id")
                .first()
            )
            by_kind[kind] = {
                "kind": kind,
                "lastSuccessAt": freshness.last_success_at.isoformat().replace("+00:00", "Z")
                if freshness.last_success_at
                else None,
                "ageSeconds": freshness.age_seconds,
                "isStale": freshness.is_stale,
                "latestRun": {
                    "status": latest_run.status if latest_run else None,
                    "rowsWritten": latest_run.rows_written if latest_run else None,
                    "finishedAt": latest_run.finished_at.isoformat().replace("+00:00", "Z")
                    if latest_run and latest_run.finished_at
                    else None,
                    "errorText": latest_run.error_text if latest_run else "",
                },
            }
            if freshness.is_stale:
                stale_sync_count += 1
                incidents.append(
                    {
                        "scope": "sync",
                        "severity": "warning",
                        "code": "sync_stale",
                        "message": f"Corporation {corporation_id} has stale {kind} sync data.",
                        "corporationId": corporation_id,
                        "kind": kind,
                    }
                )
            if latest_run and latest_run.status == "failed":
                incidents.append(
                    {
                        "scope": "sync",
                        "severity": "critical",
                        "code": "sync_failed",
                        "message": f"Corporation {corporation_id} latest {kind} sync failed.",
                        "corporationId": corporation_id,
                        "kind": kind,
                        "errorText": latest_run.error_text,
                    }
                )
        sync_corporations.append({"corporationId": corporation_id, "byKind": by_kind})

    queue = {
        "ready": WorkItem.objects.filter(status="ready").count(),
        "assigned": WorkItem.objects.filter(status="assigned").count(),
        "tempDone": WorkItem.objects.filter(status="temp_done").count(),
        "verified": WorkItem.objects.filter(status="verified").count(),
        "failed": WorkItem.objects.filter(status="failed").count(),
        "cancelled": WorkItem.objects.filter(status="cancelled").count(),
    }

    project_freshness: list[dict[str, Any]] = []
    stale_project_count = 0
    for project in Project.objects.order_by("-priority", "name"):
        if not project.work_items.exists():
            continue
        freshness = workforce_service.get_project_jobs_freshness(project=project)
        if freshness.is_stale:
            stale_project_count += 1
            incidents.append(
                {
                    "scope": "workforce",
                    "severity": "warning",
                    "code": "project_jobs_stale",
                    "message": f"Project {project.name} is backed by stale jobs sync data.",
                    "projectId": project.id,
                }
            )
        project_freshness.append(
            {
                "projectId": project.id,
                "projectName": project.name,
                "corporationId": freshness.corporation_id,
                "lastSuccessAt": freshness.last_success_at,
                "ageSeconds": freshness.age_seconds,
                "isStale": freshness.is_stale,
            }
        )

    if not planner["allGoldenMatched"] or not planner["allLegacyMatched"]:
        incidents.append(
            {
                "scope": "planner",
                "severity": "critical",
                "code": "planner_parity_drift",
                "message": "Planner shadow report detected parity drift against golden or legacy baselines.",
            }
        )

    if queue["failed"] > 0:
        incidents.append(
            {
                "scope": "workforce",
                "severity": "critical",
                "code": "failed_work_items",
                "message": f"There are {queue['failed']} failed work items awaiting director attention.",
            }
        )

    if queue["tempDone"] > 0:
        incidents.append(
            {
                "scope": "workforce",
                "severity": "warning",
                "code": "temp_done_pending",
                "message": f"There are {queue['tempDone']} TEMP_DONE work items pending verification outcome.",
            }
        )

    return {
        "planner": planner,
        "sync": {
            "corporationCount": len(sync_corporations),
            "staleCount": stale_sync_count,
            "corporations": sync_corporations,
        },
        "workforce": {
            "queue": queue,
            "staleProjectCount": stale_project_count,
            "projectFreshness": project_freshness,
            "recentEventSources": _build_recent_workforce_event_sources(),
        },
        "incidents": incidents,
        "incidentCount": len(incidents),
    }