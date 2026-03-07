from __future__ import annotations

from datetime import date
from typing import Any

from django.utils import timezone

from apps.common.cutover import generate_cutover_readiness_report
from apps.common.models import ReportSnapshot
from apps.common.shadow import generate_shadow_summary_report


def persist_report_snapshot(
    *,
    report_name: str,
    payload: dict[str, Any],
    incident_count: int,
    go_no_go: bool | None,
    snapshot_date: date | None = None,
) -> ReportSnapshot:
    snapshot_date = snapshot_date or timezone.localdate()
    snapshot, _created = ReportSnapshot.objects.update_or_create(
        snapshot_date=snapshot_date,
        report_name=report_name,
        defaults={
            "payload": payload,
            "incident_count": incident_count,
            "go_no_go": go_no_go,
        },
    )
    return snapshot


def persist_daily_report_snapshots(*, snapshot_date: date | None = None) -> list[ReportSnapshot]:
    snapshot_date = snapshot_date or timezone.localdate()
    shadow = generate_shadow_summary_report()
    cutover = generate_cutover_readiness_report()

    stored: list[ReportSnapshot] = []
    for report_name, payload, incident_count, go_no_go in [
        ("shadow_summary", shadow, int(shadow.get("incidentCount") or 0), None),
        ("cutover_readiness", cutover, int(cutover["shadow"].get("incidentCount") or 0), bool(cutover.get("goNoGo"))),
    ]:
        snapshot = persist_report_snapshot(
            report_name=report_name,
            payload=payload,
            incident_count=incident_count,
            go_no_go=go_no_go,
            snapshot_date=snapshot_date,
        )
        stored.append(snapshot)
    return stored


def persist_all_report_snapshots(*, snapshot_date: date | None = None) -> list[ReportSnapshot]:
    snapshot_date = snapshot_date or timezone.localdate()
    stored = persist_daily_report_snapshots(snapshot_date=snapshot_date)

    from apps.common.pilot import generate_cutover_pilot_readiness_report

    pilot = generate_cutover_pilot_readiness_report()
    pilot_snapshot = persist_report_snapshot(
        report_name="cutover_pilot_readiness",
        payload=pilot,
        incident_count=int(len(pilot.get("expansionBlockers") or [])),
        go_no_go=bool(pilot.get("pilotExpansionGoNoGo")),
        snapshot_date=snapshot_date,
    )
    stored.append(pilot_snapshot)

    from apps.common.preflight import generate_cutover_preflight_report

    preflight = generate_cutover_preflight_report(persist=False)
    preflight_snapshot = persist_report_snapshot(
        report_name="cutover_preflight",
        payload=preflight,
        incident_count=int((preflight.get("current") or {}).get("incidentCount") or 0),
        go_no_go=bool((preflight.get("readiness") or {}).get("goNoGo")),
        snapshot_date=snapshot_date,
    )
    stored.append(preflight_snapshot)
    return stored


def list_recent_report_snapshots(*, limit: int = 14, report_name: str = "") -> list[dict[str, Any]]:
    snapshots = ReportSnapshot.objects.order_by("-snapshot_date", "report_name")
    if report_name:
        snapshots = snapshots.filter(report_name=report_name)
    snapshots = snapshots[:limit]
    return [
        {
            "id": snapshot.id,
            "snapshotDate": snapshot.snapshot_date.isoformat(),
            "reportName": snapshot.report_name,
            "incidentCount": snapshot.incident_count,
            "goNoGo": snapshot.go_no_go,
            "payload": snapshot.payload,
            "updatedAt": snapshot.updated_at.isoformat().replace("+00:00", "Z"),
        }
        for snapshot in snapshots
    ]


def list_cutover_readiness_trend(*, limit: int = 14) -> list[dict[str, Any]]:
    snapshots = ReportSnapshot.objects.filter(report_name="cutover_readiness").order_by("-snapshot_date")[:limit]
    trend: list[dict[str, Any]] = []
    for snapshot in snapshots:
        payload = snapshot.payload or {}
        role_assignments = payload.get("roleAssignments") or {}
        script_signoffs = payload.get("scriptSignoffs") or {}
        trend.append(
            {
                "snapshotDate": snapshot.snapshot_date.isoformat(),
                "goNoGo": snapshot.go_no_go,
                "incidentCount": snapshot.incident_count,
                "assignedRoles": int(role_assignments.get("assignedCount") or 0),
                "requiredRoles": int(role_assignments.get("requiredCount") or 0),
                "validatedSignoffs": int(script_signoffs.get("validatedCount") or 0),
                "requiredSignoffs": int(script_signoffs.get("requiredCount") or 0),
                "blockerCount": len(payload.get("blockers") or []),
                "mode": payload.get("mode") or "",
            }
        )
    return trend


def list_cutover_pilot_readiness_trend(*, limit: int = 14) -> list[dict[str, Any]]:
    snapshots = ReportSnapshot.objects.filter(report_name="cutover_pilot_readiness").order_by("-snapshot_date")[:limit]
    trend: list[dict[str, Any]] = []
    for snapshot in snapshots:
        payload = snapshot.payload or {}
        activity = payload.get("activitySummary") or {}
        trend.append(
            {
                "snapshotDate": snapshot.snapshot_date.isoformat(),
                "pilotStage": payload.get("pilotStage") or "pre_pilot",
                "pilotStartGoNoGo": bool(payload.get("pilotStartGoNoGo")),
                "pilotExpansionGoNoGo": bool(payload.get("pilotExpansionGoNoGo")),
                "pilotUserCount": len(payload.get("pilotUserIds") or []),
                "claimCount": int(activity.get("claimCount") or 0),
                "tempDoneCount": int(activity.get("tempDoneCount") or 0),
                "verifiedOkCount": int(activity.get("verifiedOkCount") or 0),
                "verifyMissCount": int(activity.get("verifyMissCount") or 0),
                "expansionBlockerCount": len(payload.get("expansionBlockers") or []),
            }
        )
    return trend