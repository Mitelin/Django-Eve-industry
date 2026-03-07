from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db.models import Count, Max, Min

from apps.common.preflight import generate_cutover_preflight_report
from apps.workforce.models import WorkEvent


def _role_is_assigned(role_assignments: dict[str, Any], role_name: str) -> bool:
    items = list(role_assignments.get("items") or [])
    return any(item.get("roleName") == role_name and item.get("assigned") for item in items)


def _event_count_and_latest(*, event_type: str, pilot_user_ids: list[int]) -> dict[str, Any]:
    queryset = WorkEvent.objects.filter(event_type=event_type)
    if event_type in {"CLAIMED", "TEMP_DONE"}:
        queryset = queryset.filter(actor_id__in=pilot_user_ids)
    else:
        queryset = queryset.filter(work_item__assigned_to_id__in=pilot_user_ids)

    aggregate = queryset.aggregate(count=Count("id"), first_at=Min("created_at"), latest_at=Max("created_at"))
    return {
        "count": int(aggregate["count"] or 0),
        "firstAt": aggregate["first_at"].isoformat() if aggregate["first_at"] else None,
        "latestAt": aggregate["latest_at"].isoformat() if aggregate["latest_at"] else None,
    }


def _get_pilot_stage(*, claim_count: int, temp_done_count: int, verified_ok_count: int, verify_miss_count: int) -> str:
    if verified_ok_count and not verify_miss_count:
        return "cycle_verified"
    if verified_ok_count and verify_miss_count:
        return "cycle_verified_with_retries"
    if verify_miss_count:
        return "verification_failed"
    if temp_done_count:
        return "awaiting_verification"
    if claim_count:
        return "claim_started"
    return "pre_pilot"


def generate_cutover_pilot_readiness_report() -> dict[str, Any]:
    preflight = generate_cutover_preflight_report(persist=False, trend_limit=6)
    readiness = preflight["readiness"]
    role_assignments = readiness.get("roleAssignments") or {}
    pilot_user_ids = [int(user_id) for user_id in (settings.CUTOVER_PILOT_USER_IDS or [])]

    claim_summary = _event_count_and_latest(event_type="CLAIMED", pilot_user_ids=pilot_user_ids)
    temp_done_summary = _event_count_and_latest(event_type="TEMP_DONE", pilot_user_ids=pilot_user_ids)
    verified_ok_summary = _event_count_and_latest(event_type="VERIFIED_OK", pilot_user_ids=pilot_user_ids)
    verify_miss_summary = _event_count_and_latest(event_type="VERIFY_MISS", pilot_user_ids=pilot_user_ids)

    verification_attempt_count = verified_ok_summary["count"] + verify_miss_summary["count"]
    pilot_stage = _get_pilot_stage(
        claim_count=claim_summary["count"],
        temp_done_count=temp_done_summary["count"],
        verified_ok_count=verified_ok_summary["count"],
        verify_miss_count=verify_miss_summary["count"],
    )
    latest_pilot_event_at = max(
        [
            event_at
            for event_at in [
                claim_summary["latestAt"],
                temp_done_summary["latestAt"],
                verified_ok_summary["latestAt"],
                verify_miss_summary["latestAt"],
            ]
            if event_at
        ],
        default=None,
    )

    checklist = {
        "effectivePreflightGoNoGo": bool(preflight.get("effectiveGoNoGo")),
        "assignmentWritesEnabled": bool((readiness.get("checklist") or {}).get("assignmentWritesEnabled")),
        "pilotUsersConfigured": bool((readiness.get("checklist") or {}).get("pilotUsersConfigured")),
        "syncHealthy": bool((readiness.get("checklist") or {}).get("syncHealthy")),
        "workforceHealthy": bool((readiness.get("checklist") or {}).get("workforceHealthy")),
        "incidentCommanderOnDuty": _role_is_assigned(role_assignments, "incidentCommander"),
        "rollbackApproverOnDuty": _role_is_assigned(role_assignments, "rollbackApprover"),
        "firstPilotClaimObserved": claim_summary["count"] > 0,
        "firstPilotTempDoneObserved": temp_done_summary["count"] > 0,
        "firstPilotVerificationObserved": verification_attempt_count > 0,
        "firstPilotCycleCompleted": verified_ok_summary["count"] > 0,
        "firstPilotCycleClean": verified_ok_summary["count"] > 0 and verify_miss_summary["count"] == 0,
    }

    start_blockers: list[str] = []
    if readiness.get("mode") != "assisted":
        start_blockers.append("Pilot readiness is only actionable while cutover mode remains assisted.")
    if not checklist["effectivePreflightGoNoGo"]:
        start_blockers.append("Preflight is still NO-GO for assisted rollout.")
    if not checklist["assignmentWritesEnabled"]:
        start_blockers.append("Assignment writes are still read-only for the pilot cohort.")
    if not checklist["pilotUsersConfigured"]:
        start_blockers.append("Pilot user scope is not configured.")
    if not checklist["incidentCommanderOnDuty"]:
        start_blockers.append("Incident commander coverage is not assigned.")
    if not checklist["rollbackApproverOnDuty"]:
        start_blockers.append("Rollback approver coverage is not assigned.")
    if not checklist["syncHealthy"]:
        start_blockers.append("Sync freshness is not healthy enough to begin the pilot wave.")
    if not checklist["workforceHealthy"]:
        start_blockers.append("Workforce posture still shows failed or stale execution risk.")

    expansion_blockers = list(start_blockers)
    if not checklist["firstPilotClaimObserved"]:
        expansion_blockers.append("Pilot has not yet claimed a Django-assigned work item.")
    if not checklist["firstPilotTempDoneObserved"]:
        expansion_blockers.append("Pilot has not yet reached TEMP_DONE on a Django-assigned work item.")
    if not checklist["firstPilotVerificationObserved"]:
        expansion_blockers.append("Pilot verification has not yet been attempted end-to-end.")
    if not checklist["firstPilotCycleCompleted"]:
        expansion_blockers.append("Pilot cycle has not yet produced a verified completion.")
    if verify_miss_summary["count"] > 0:
        expansion_blockers.append("Pilot cycle recorded verification misses; resolve them before expanding rollout.")

    return {
        "mode": readiness.get("mode"),
        "pilotUserIds": pilot_user_ids,
        "pilotStage": pilot_stage,
        "pilotStartGoNoGo": not start_blockers,
        "pilotExpansionGoNoGo": not expansion_blockers,
        "checklist": checklist,
        "activitySummary": {
            "claimCount": claim_summary["count"],
            "tempDoneCount": temp_done_summary["count"],
            "verifiedOkCount": verified_ok_summary["count"],
            "verifyMissCount": verify_miss_summary["count"],
            "verificationAttemptCount": verification_attempt_count,
            "firstClaimAt": claim_summary["firstAt"],
            "firstTempDoneAt": temp_done_summary["firstAt"],
            "firstVerifiedOkAt": verified_ok_summary["firstAt"],
            "firstVerifyMissAt": verify_miss_summary["firstAt"],
            "latestPilotEventAt": latest_pilot_event_at,
        },
        "startBlockers": start_blockers,
        "expansionBlockers": expansion_blockers,
        "preflight": {
            "effectiveGoNoGo": bool(preflight.get("effectiveGoNoGo")),
            "preflightBlockers": list(preflight.get("preflightBlockers") or []),
            "recommendedActions": list(preflight.get("recommendedActions") or []),
        },
    }