from __future__ import annotations

import json
from typing import Any

from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from apps.common.cutover import generate_cutover_readiness_report
from apps.common.history import (
    list_cutover_pilot_readiness_trend,
    list_cutover_readiness_trend,
    list_recent_report_snapshots,
    persist_all_report_snapshots,
)
from apps.common.models import ScriptSignoff
from apps.common.ownership import (
    get_required_cutover_roles,
    sync_missing_cutover_role_assignments,
    update_cutover_role_assignment,
)
from apps.common.pilot import generate_cutover_pilot_readiness_report
from apps.common.preflight import generate_cutover_preflight_report
from apps.common.signoffs import get_script_signoff_summary, sync_missing_required_script_signoffs, update_script_signoff
from apps.common.shadow import generate_shadow_summary_report


def _parse_json_body(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    return json.loads(request.body.decode("utf-8"))


@require_GET
def shadow_summary_report(_request):
    return JsonResponse(generate_shadow_summary_report())


@require_GET
def cutover_readiness_report(_request):
    return JsonResponse(generate_cutover_readiness_report())


@require_GET
def cutover_pilot_readiness_report(_request):
    return JsonResponse(generate_cutover_pilot_readiness_report())


@require_GET
def report_snapshot_history(request):
    limit = int(request.GET.get("limit") or 14)
    report_name = (request.GET.get("reportName") or "").strip()
    return JsonResponse({"snapshots": list_recent_report_snapshots(limit=limit, report_name=report_name)})


@require_POST
def persist_report_history(_request: HttpRequest):
    snapshots = persist_all_report_snapshots()
    return JsonResponse(
        {
            "storedSnapshots": [
                {
                    "reportName": snapshot.report_name,
                    "snapshotDate": snapshot.snapshot_date.isoformat(),
                }
                for snapshot in snapshots
            ]
        }
    )


@require_GET
def cutover_script_signoffs(_request):
    return JsonResponse(get_script_signoff_summary())


@require_GET
def cutover_trend_report(request):
    limit = int(request.GET.get("limit") or 14)
    return JsonResponse({"trend": list_cutover_readiness_trend(limit=limit)})


@require_GET
def cutover_pilot_trend_report(request):
    limit = int(request.GET.get("limit") or 14)
    return JsonResponse({"trend": list_cutover_pilot_readiness_trend(limit=limit)})


@require_GET
def cutover_preflight_report(request):
    trend_limit = int(request.GET.get("trendLimit") or 7)
    persist = (request.GET.get("persist") or "0") == "1"
    return JsonResponse(generate_cutover_preflight_report(persist=persist, trend_limit=trend_limit))


@require_POST
def update_cutover_script_signoff(request: HttpRequest):
    body = _parse_json_body(request)
    script_name = str(body.get("scriptName") or "").strip()
    status = str(body.get("status") or "").strip().lower()
    signed_off_by = str(body.get("signedOffBy") or body.get("changedBy") or "").strip()
    notes = str(body.get("notes") or "").strip()

    if not script_name:
        return JsonResponse({"error": "scriptName is required"}, status=400)

    allowed_statuses = {choice for choice, _label in ScriptSignoff.Status.choices}
    if status not in allowed_statuses:
        return JsonResponse(
            {"error": f"status must be one of: {', '.join(sorted(allowed_statuses))}"},
            status=400,
        )

    update_script_signoff(
        script_name=script_name,
        status=status,
        signed_off_by=signed_off_by,
        notes=notes,
    )
    readiness = generate_cutover_readiness_report()
    return JsonResponse(
        {
            "scriptSignoffs": get_script_signoff_summary(),
            "readiness": readiness,
        }
    )


@require_POST
def sync_missing_cutover_script_signoffs(request: HttpRequest):
    body = _parse_json_body(request)
    changed_by = str(body.get("changedBy") or body.get("signedOffBy") or "").strip()
    notes = str(body.get("notes") or "").strip()

    synced = sync_missing_required_script_signoffs(changed_by=changed_by, notes=notes)
    readiness = generate_cutover_readiness_report()
    return JsonResponse(
        {
            "syncedScripts": [signoff.script_name for signoff in synced],
            "scriptSignoffs": get_script_signoff_summary(),
            "readiness": readiness,
        }
    )


@require_POST
def update_cutover_role_owner(request: HttpRequest):
    body = _parse_json_body(request)
    role_name = str(body.get("roleName") or "").strip()
    assigned_to = str(body.get("assignedTo") or "").strip()
    changed_by = str(body.get("changedBy") or "").strip()
    notes = str(body.get("notes") or "").strip()

    if not role_name:
        return JsonResponse({"error": "roleName is required"}, status=400)

    allowed_roles = set(get_required_cutover_roles().keys())
    if role_name not in allowed_roles:
        return JsonResponse(
            {"error": f"roleName must be one of: {', '.join(sorted(allowed_roles))}"},
            status=400,
        )

    update_cutover_role_assignment(
        role_name=role_name,
        assigned_to=assigned_to,
        changed_by=changed_by,
        notes=notes,
    )
    readiness = generate_cutover_readiness_report()
    return JsonResponse(
        {
            "roleAssignments": readiness["roleAssignments"],
            "readiness": readiness,
        }
    )


@require_POST
def sync_missing_cutover_roles(request: HttpRequest):
    body = _parse_json_body(request)
    changed_by = str(body.get("changedBy") or "").strip()
    notes = str(body.get("notes") or "").strip()

    synced = sync_missing_cutover_role_assignments(changed_by=changed_by, notes=notes)
    readiness = generate_cutover_readiness_report()
    return JsonResponse(
        {
            "syncedRoles": [assignment.role_name for assignment in synced],
            "roleAssignments": readiness["roleAssignments"],
            "readiness": readiness,
        }
    )