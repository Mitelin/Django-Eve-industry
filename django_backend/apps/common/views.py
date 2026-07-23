from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from apps.common.cutover import generate_cutover_readiness_report
from apps.common.history import (
    list_cutover_pilot_readiness_trend,
    list_cutover_readiness_trend,
    list_recent_report_snapshots,
    persist_all_report_snapshots,
)
from apps.common.models import RollbackEvidence, ScriptSignoff
from apps.common.ownership import (
    get_required_cutover_roles,
    sync_missing_cutover_role_assignments,
    update_cutover_role_assignment,
)
from apps.common.phase9_exit import generate_phase9_exit_report
from apps.common.phase10_prep import generate_phase10_prep_report
from apps.common.pilot import generate_cutover_pilot_readiness_report
from apps.common.preflight import generate_cutover_preflight_report
from apps.common.rollback import get_rollback_evidence_summary, update_rollback_evidence
from apps.common.sde_import import (
    SdeArchiveValidationError,
    SdeImportError,
    get_sde_import_summary,
    import_sde_from_url,
    import_sde_from_upload,
)
from apps.common.signoffs import get_script_signoff_summary, sync_missing_required_script_signoffs, update_script_signoff
from apps.common.shadow import generate_shadow_summary_report


PROCESS_STARTED_AT = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")


def _read_runtime_version() -> dict[str, str | bool]:
    repo_root = Path(settings.BASE_DIR).parent

    def _git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(repo_root), *args],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()

    try:
        return {
            "source": "git",
            "commit": _git("rev-parse", "HEAD"),
            "shortCommit": _git("rev-parse", "--short", "HEAD"),
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(_git("status", "--porcelain")),
        }
    except (OSError, subprocess.SubprocessError):
        return {
            "source": "unknown",
            "commit": "unknown",
            "shortCommit": "unknown",
            "branch": "unknown",
            "dirty": False,
        }


@require_GET
def runtime_version(_request: HttpRequest):
    return JsonResponse(
        {
            **_read_runtime_version(),
            "processStartedAt": PROCESS_STARTED_AT,
            "schedulerEnabled": False,
            "schedulerRunning": False,
            "activityScheduler": {
                "running": False,
                "lastStartedAt": None,
                "lastFinishedAt": None,
                "lastSuccessAt": None,
                "lastResultCount": None,
                "lastError": None,
            },
            "nextActivityRunAt": None,
        }
    )


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
def cutover_phase9_exit_report(_request):
    return JsonResponse(generate_phase9_exit_report())


@require_GET
def cutover_phase10_prep_report(_request):
    return JsonResponse(generate_phase10_prep_report())


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


@require_GET
def sde_import_status(request: HttpRequest):
    limit = int(request.GET.get("limit") or 10)
    return JsonResponse(get_sde_import_summary(limit=limit))


@require_POST
def import_sde_from_url_view(request: HttpRequest):
    body = _parse_json_body(request)
    archive_url = str(body.get("archiveUrl") or "").strip()
    triggered_by = str(body.get("triggeredBy") or body.get("changedBy") or "").strip()
    force_reimport = bool(body.get("forceReimport"))

    if not archive_url:
        return JsonResponse({"error": "archiveUrl is required"}, status=400)

    try:
        result = import_sde_from_url(
            archive_url=archive_url,
            triggered_by=triggered_by,
            force_reimport=force_reimport,
        )
    except SdeArchiveValidationError as exc:
        return JsonResponse(
            {
                "error": str(exc),
                **get_sde_import_summary(limit=10),
            },
            status=400,
        )
    except SdeImportError as exc:
        return JsonResponse(
            {
                "error": str(exc),
                **get_sde_import_summary(limit=10),
            },
            status=500,
        )

    return JsonResponse(result)


@require_POST
def import_sde_upload_view(request: HttpRequest):
    uploaded_file = request.FILES.get("archive")
    triggered_by = str(request.POST.get("triggeredBy") or request.POST.get("changedBy") or "").strip()
    force_reimport = str(request.POST.get("forceReimport") or "").strip().lower() in {"1", "true", "yes", "on"}

    if uploaded_file is None:
        return JsonResponse({"error": "archive file is required"}, status=400)

    try:
        result = import_sde_from_upload(
            uploaded_file=uploaded_file,
            triggered_by=triggered_by,
            force_reimport=force_reimport,
        )
    except SdeArchiveValidationError as exc:
        return JsonResponse(
            {
                "error": str(exc),
                **get_sde_import_summary(limit=10),
            },
            status=400,
        )
    except SdeImportError as exc:
        return JsonResponse(
            {
                "error": str(exc),
                **get_sde_import_summary(limit=10),
            },
            status=500,
        )

    return JsonResponse(result)


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


@require_POST
def update_cutover_rollback_evidence(request: HttpRequest):
    body = _parse_json_body(request)
    evidence_type = str(body.get("evidenceType") or "").strip()
    evidence_date_raw = str(body.get("evidenceDate") or "").strip()
    changed_by = str(body.get("changedBy") or body.get("recordedBy") or "").strip()
    notes = str(body.get("notes") or "").strip()

    allowed_types = {choice for choice, _label in RollbackEvidence.EvidenceType.choices}
    if evidence_type not in allowed_types:
        return JsonResponse(
            {"error": f"evidenceType must be one of: {', '.join(sorted(allowed_types))}"},
            status=400,
        )

    parsed_date = None
    if evidence_date_raw:
        try:
            parsed_date = date.fromisoformat(evidence_date_raw)
        except ValueError:
            return JsonResponse({"error": "evidenceDate must be a valid ISO date (YYYY-MM-DD)"}, status=400)

    update_rollback_evidence(
        evidence_type=evidence_type,
        evidence_date=parsed_date,
        changed_by=changed_by,
        notes=notes,
    )
    readiness = generate_cutover_readiness_report()
    return JsonResponse(
        {
            "rollbackSummary": get_rollback_evidence_summary(),
            "readiness": readiness,
        }
    )