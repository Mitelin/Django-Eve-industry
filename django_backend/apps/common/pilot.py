from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db.models import Count, Max, Min, Q
from django.utils import timezone

from apps.common.preflight import _build_manual_action_item
from apps.common.preflight import generate_cutover_preflight_report
from apps.workforce.models import WorkEvent
from apps.workforce.models import WorkItem


def _role_is_assigned(role_assignments: dict[str, Any], role_name: str) -> bool:
    items = list(role_assignments.get("items") or [])
    return any(item.get("roleName") == role_name and item.get("assigned") for item in items)


def _pilot_event_filter(*, event_type: str, pilot_user_ids: list[int]) -> Q:
    if event_type in {"CLAIMED", "TEMP_DONE"}:
        return Q(actor_id__in=pilot_user_ids) | Q(details__assignedUserId__in=pilot_user_ids)
    return Q(details__assignedUserId__in=pilot_user_ids) | Q(work_item__assigned_to_id__in=pilot_user_ids)


def _event_count_and_latest(*, event_type: str, pilot_user_ids: list[int]) -> dict[str, Any]:
    queryset = WorkEvent.objects.filter(event_type=event_type).filter(_pilot_event_filter(event_type=event_type, pilot_user_ids=pilot_user_ids))

    aggregate = queryset.aggregate(count=Count("id"), first_at=Min("created_at"), latest_at=Max("created_at"))
    return {
        "count": int(aggregate["count"] or 0),
        "firstAt": aggregate["first_at"].isoformat() if aggregate["first_at"] else None,
        "latestAt": aggregate["latest_at"].isoformat() if aggregate["latest_at"] else None,
    }


def _build_pilot_operational_summary(*, pilot_user_ids: list[int]) -> dict[str, Any]:
    verification_sla_minutes = int(settings.CUTOVER_PILOT_VERIFICATION_SLA_MINUTES or 30)
    sla_deadline = timezone.now() - timedelta(minutes=verification_sla_minutes)

    verify_miss_count = WorkEvent.objects.filter(event_type="VERIFY_MISS").filter(
        _pilot_event_filter(event_type="VERIFY_MISS", pilot_user_ids=pilot_user_ids)
    ).count()
    verified_ok_count = WorkEvent.objects.filter(event_type="VERIFIED_OK").filter(
        _pilot_event_filter(event_type="VERIFIED_OK", pilot_user_ids=pilot_user_ids)
    ).count()
    escalated_count = WorkEvent.objects.filter(event_type="ESCALATED").filter(
        _pilot_event_filter(event_type="ESCALATED", pilot_user_ids=pilot_user_ids)
    ).count()
    director_intervention_count = WorkEvent.objects.filter(
        event_type__in=["DIRECTOR_REQUEUED", "DIRECTOR_RELEASED"]
    ).filter(
        _pilot_event_filter(event_type="DIRECTOR_REQUEUED", pilot_user_ids=pilot_user_ids)
    ).count()
    manual_intervention_count = WorkEvent.objects.filter(details__source="manual_action").filter(
        _pilot_event_filter(event_type="DIRECTOR_REQUEUED", pilot_user_ids=pilot_user_ids)
    ).count()
    recommended_intervention_count = WorkEvent.objects.filter(details__source="recommended_action").filter(
        _pilot_event_filter(event_type="DIRECTOR_REQUEUED", pilot_user_ids=pilot_user_ids)
    ).count()
    temp_done_past_sla_count = WorkItem.objects.filter(
        status="temp_done",
        assigned_to_id__in=pilot_user_ids,
        updated_at__lt=sla_deadline,
    ).count()
    failed_open_count = WorkItem.objects.filter(status="failed", assigned_to_id__in=pilot_user_ids).count()
    verification_attempt_count = verified_ok_count + verify_miss_count
    verify_miss_rate_percent = round((verify_miss_count / verification_attempt_count) * 100, 1) if verification_attempt_count else 0.0

    return {
        "verificationSlaMinutes": verification_sla_minutes,
        "verificationAttemptCount": verification_attempt_count,
        "verifyMissCount": verify_miss_count,
        "verifyMissRatePercent": verify_miss_rate_percent,
        "escalatedCount": escalated_count,
        "directorInterventionCount": director_intervention_count,
        "manualInterventionCount": manual_intervention_count,
        "recommendedInterventionCount": recommended_intervention_count,
        "tempDonePastSlaCount": temp_done_past_sla_count,
        "failedOpenCount": failed_open_count,
        "incidentCount": verify_miss_count + escalated_count + temp_done_past_sla_count + failed_open_count,
    }


def _build_pilot_policy_summary(*, operational_summary: dict[str, Any]) -> dict[str, Any]:
    max_verify_miss_rate_percent = float(settings.CUTOVER_PILOT_MAX_VERIFY_MISS_RATE_PERCENT or 0)
    max_escalated_count = int(settings.CUTOVER_PILOT_MAX_ESCALATED_COUNT or 0)
    max_manual_intervention_count = int(settings.CUTOVER_PILOT_MAX_MANUAL_INTERVENTION_COUNT or 0)

    verify_miss_rate_percent = float(operational_summary.get("verifyMissRatePercent") or 0.0)
    escalated_count = int(operational_summary.get("escalatedCount") or 0)
    manual_intervention_count = int(operational_summary.get("manualInterventionCount") or 0)

    verify_miss_rate_within_policy = verify_miss_rate_percent <= max_verify_miss_rate_percent
    escalated_count_within_policy = escalated_count <= max_escalated_count
    manual_intervention_count_within_policy = manual_intervention_count <= max_manual_intervention_count

    breach_reasons: list[str] = []
    if not verify_miss_rate_within_policy:
        breach_reasons.append(
            "Pilot verify-miss rate exceeds policy threshold; hold rollout expansion until retry pressure is back within policy."
        )
    if not escalated_count_within_policy:
        breach_reasons.append(
            "Pilot escalations exceed policy threshold; hold rollout expansion until escalation pressure is back within policy."
        )
    if not manual_intervention_count_within_policy:
        breach_reasons.append(
            "Pilot manual interventions exceed policy threshold; hold rollout expansion until operator intervention pressure is back within policy."
        )

    return {
        "thresholds": {
            "maxVerifyMissRatePercent": max_verify_miss_rate_percent,
            "maxEscalatedCount": max_escalated_count,
            "maxManualInterventionCount": max_manual_intervention_count,
        },
        "status": {
            "verifyMissRateWithinPolicy": verify_miss_rate_within_policy,
            "escalatedCountWithinPolicy": escalated_count_within_policy,
            "manualInterventionCountWithinPolicy": manual_intervention_count_within_policy,
            "withinPolicy": verify_miss_rate_within_policy
            and escalated_count_within_policy
            and manual_intervention_count_within_policy,
        },
        "breachReasons": breach_reasons,
    }


def _dedupe_action_items(action_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    for item in action_items:
        code = str(item.get("code") or "").strip()
        if not code or code in seen_codes:
            continue
        deduped.append(item)
        seen_codes.add(code)
    return deduped


def _build_pilot_recommended_action_items(
    *,
    preflight: dict[str, Any],
    operational_summary: dict[str, Any],
    policy_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    action_items = list(preflight.get("recommendedActionItems") or [])
    policy_status = policy_summary.get("status") or {}

    if operational_summary.get("tempDonePastSlaCount"):
        action_items.append(
            _build_manual_action_item(
                code="clear_pilot_verification_backlog",
                label="Clear pilot TEMP_DONE backlog that is past the verification SLA.",
                guidance_title="Recover pilot verification SLA posture",
                guidance_steps=[
                    "Open the affected pilot project in Director Flight Deck and isolate TEMP_DONE items that are older than the SLA window.",
                    "Verify, requeue, or release the blocked pilot work until TEMP_DONE past-SLA count returns to zero.",
                    "Persist pilot evidence again after recovery so the stored trend shows the cleared verification backlog.",
                ],
                action_type="focusWorkforceBlockers",
            )
        )
    if operational_summary.get("failedOpenCount"):
        action_items.append(
            _build_manual_action_item(
                code="clear_pilot_failed_backlog",
                label="Clear failed pilot work items before continuing rollout.",
                guidance_title="Recover failed pilot work backlog",
                guidance_steps=[
                    "Open the affected pilot project in Director Flight Deck and inspect failed pilot work items.",
                    "Requeue, release, or redispatch the failed items until no pilot failures remain open.",
                    "Persist pilot evidence again after cleanup so the next snapshot reflects the recovered posture.",
                ],
                action_type="focusWorkforceBlockers",
            )
        )
    if not policy_status.get("verifyMissRateWithinPolicy", True):
        action_items.append(
            _build_manual_action_item(
                code="reduce_pilot_retry_pressure",
                label="Reduce pilot verify-miss pressure before rollout expansion.",
                guidance_title="Reduce pilot retry pressure",
                guidance_steps=[
                    "Open the affected pilot project and review items that recently hit VERIFY_MISS or repeated requeue loops.",
                    "Correct the underlying validation or assignment issue and drive the pilot cycle back to VERIFIED_OK without additional misses.",
                    "Persist pilot evidence again after the retry pressure drops below the configured verify-miss policy threshold.",
                ],
                action_type="focusWorkforceBlockers",
            )
        )
    if not policy_status.get("escalatedCountWithinPolicy", True):
        action_items.append(
            _build_manual_action_item(
                code="clear_pilot_escalations",
                label="Clear pilot escalations before rollout expansion.",
                guidance_title="Resolve pilot escalations",
                guidance_steps=[
                    "Open the affected pilot project and review escalated work items and their recovery history.",
                    "Assign the correct owner, unblock the root cause, and close the escalation path before expanding the pilot wave.",
                    "Persist pilot evidence again after escalation count returns within the configured policy threshold.",
                ],
                action_type="focusWorkforceBlockers",
            )
        )
    if not policy_status.get("manualInterventionCountWithinPolicy", True):
        action_items.append(
            _build_manual_action_item(
                code="stabilize_pilot_manual_interventions",
                label="Reduce manual operator interventions before rollout expansion.",
                guidance_title="Stabilize pilot operator intervention pressure",
                guidance_steps=[
                    "Open the affected pilot project and compare manual cleanup actions against recommendation-driven handling in Recent Events.",
                    "Remove the recurring bottleneck that is forcing manual director intervention and confirm the workflow can progress with normal recommendation paths.",
                    "Persist pilot evidence again after manual intervention count returns within the configured policy threshold.",
                ],
                action_type="focusWorkforceBlockers",
            )
        )

    return _dedupe_action_items(action_items)


def _build_recommended_actions(action_items: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("label") or "") for item in action_items if item.get("label")]


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


def build_cutover_pilot_readiness_report(*, preflight: dict[str, Any]) -> dict[str, Any]:
    readiness = preflight["readiness"]
    role_assignments = readiness.get("roleAssignments") or {}
    rollback_summary = readiness.get("rollbackSummary") or {}
    rollback_status = rollback_summary.get("status") or {}
    pilot_user_ids = [int(user_id) for user_id in (settings.CUTOVER_PILOT_USER_IDS or [])]

    claim_summary = _event_count_and_latest(event_type="CLAIMED", pilot_user_ids=pilot_user_ids)
    temp_done_summary = _event_count_and_latest(event_type="TEMP_DONE", pilot_user_ids=pilot_user_ids)
    verified_ok_summary = _event_count_and_latest(event_type="VERIFIED_OK", pilot_user_ids=pilot_user_ids)
    verify_miss_summary = _event_count_and_latest(event_type="VERIFY_MISS", pilot_user_ids=pilot_user_ids)

    verification_attempt_count = verified_ok_summary["count"] + verify_miss_summary["count"]
    operational_summary = _build_pilot_operational_summary(pilot_user_ids=pilot_user_ids)
    policy_summary = _build_pilot_policy_summary(operational_summary=operational_summary)
    recommended_action_items = _build_pilot_recommended_action_items(
        preflight=preflight,
        operational_summary=operational_summary,
        policy_summary=policy_summary,
    )
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
        "rollbackEvidenceGateSatisfied": bool(rollback_status.get("gateSatisfied", True)),
        "firstPilotClaimObserved": claim_summary["count"] > 0,
        "firstPilotTempDoneObserved": temp_done_summary["count"] > 0,
        "firstPilotVerificationObserved": verification_attempt_count > 0,
        "firstPilotCycleCompleted": verified_ok_summary["count"] > 0,
        "firstPilotCycleClean": verified_ok_summary["count"] > 0 and verify_miss_summary["count"] == 0,
        "pilotVerificationSlaHealthy": operational_summary["tempDonePastSlaCount"] == 0,
        "pilotRetryFree": operational_summary["verifyMissCount"] == 0,
        "pilotEscalationFree": operational_summary["escalatedCount"] == 0,
        "pilotFailureBacklogClear": operational_summary["failedOpenCount"] == 0,
        "pilotPolicyWithinThresholds": bool((policy_summary.get("status") or {}).get("withinPolicy")),
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
    if not checklist["rollbackEvidenceGateSatisfied"]:
        start_blockers.append("Rollback runbook review or rollback drill evidence is missing or stale.")
    if not checklist["syncHealthy"]:
        start_blockers.append("Sync freshness is not healthy enough to begin the pilot wave.")
    if not checklist["workforceHealthy"]:
        start_blockers.append("Workforce posture still shows failed or stale execution risk.")
    if not checklist["pilotVerificationSlaHealthy"]:
        start_blockers.append("Pilot already has TEMP_DONE work beyond the verification SLA window.")
    if not checklist["pilotEscalationFree"]:
        start_blockers.append("Pilot already has escalated work items that must be cleared before continuing.")

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
    if operational_summary["tempDonePastSlaCount"] > 0:
        expansion_blockers.append("Pilot still has TEMP_DONE work beyond the verification SLA window.")
    if operational_summary["escalatedCount"] > 0:
        expansion_blockers.append("Pilot recorded escalated work items; clear them before expanding rollout.")
    if operational_summary["failedOpenCount"] > 0:
        expansion_blockers.append("Pilot still has failed work items open; clear them before expanding rollout.")
    expansion_blockers.extend(list(policy_summary.get("breachReasons") or []))

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
        "operationalSummary": operational_summary,
        "policySummary": policy_summary,
        "rollbackSummary": rollback_summary,
        "recommendedActions": _build_recommended_actions(recommended_action_items),
        "recommendedActionItems": recommended_action_items,
        "startBlockers": start_blockers,
        "expansionBlockers": expansion_blockers,
        "preflight": {
            "effectiveGoNoGo": bool(preflight.get("effectiveGoNoGo")),
            "preflightBlockers": list(preflight.get("preflightBlockers") or []),
            "recommendedActions": list(preflight.get("recommendedActions") or []),
            "recommendedActionItems": list(preflight.get("recommendedActionItems") or []),
        },
    }


def generate_cutover_pilot_readiness_report() -> dict[str, Any]:
    preflight = generate_cutover_preflight_report(persist=False, trend_limit=6, include_pilot=False)
    return build_cutover_pilot_readiness_report(preflight=preflight)