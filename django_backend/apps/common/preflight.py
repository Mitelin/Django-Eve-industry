from __future__ import annotations

from datetime import date
from typing import Any

from apps.common.cutover import generate_cutover_readiness_report
from apps.common.history import (
    list_cutover_readiness_trend,
    list_recent_report_snapshots,
    persist_daily_report_snapshots,
    persist_report_snapshot,
)


def _build_manual_action_item(
    *,
    code: str,
    label: str,
    guidance_title: str,
    guidance_steps: list[str],
    action_type: str = "manual",
    target_setting: str = "",
    target_evidence_type: str = "",
) -> dict[str, Any]:
    return {
        "code": code,
        "label": label,
        "actionType": action_type,
        "targetSetting": target_setting,
        "targetEvidenceType": target_evidence_type,
        "guidanceTitle": guidance_title,
        "guidanceSteps": guidance_steps,
    }


def _build_recommended_action_items(
    readiness: dict[str, Any],
    *,
    has_stored_preflight_baseline: bool,
    workforce_provenance_warning: str = "",
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    role_assignments = readiness.get("roleAssignments") or {}
    script_signoffs = readiness.get("scriptSignoffs") or {}
    checklist = readiness.get("checklist") or {}
    role_items = list(role_assignments.get("items") or [])
    signoff_items = list(script_signoffs.get("items") or [])
    first_missing_role = next((item for item in role_items if not item.get("assigned")), None)
    first_unsigned_script = next(
        (item for item in signoff_items if item.get("required") and item.get("status") != "validated"),
        None,
    )
    has_env_backed_role_defaults = any(item.get("source") == "env" for item in role_items)
    has_missing_required_signoff_rows = any(item.get("required") and item.get("source") == "missing" for item in signoff_items)

    if (not has_stored_preflight_baseline) or has_env_backed_role_defaults or has_missing_required_signoff_rows:
        actions.append(
            {
                "code": "bootstrap_governance",
                "label": "Bootstrap governance evidence and missing inventory in one step.",
                "actionType": "bootstrapGovernance",
            }
        )

    if not has_stored_preflight_baseline:
        actions.append(
            {
                "code": "persist_preflight_baseline",
                "label": "Persist evidence to establish a stored preflight comparison baseline.",
                "actionType": "persistEvidence",
            }
        )

    if not checklist.get("rolesAssigned"):
        actions.append(
            {
                "code": "assign_missing_roles",
                "label": "Assign missing cutover owners before assisted rollout.",
                "actionType": "syncMissingRoleOwners"
                if has_env_backed_role_defaults
                else "focusRoleOwner",
                "targetRoleName": (first_missing_role or {}).get("roleName") or "",
            }
        )
    if not checklist.get("criticalScriptsSignedOff"):
        actions.append(
            {
                "code": "validate_critical_scripts",
                "label": "Record critical script validation before removing compatibility shims.",
                "actionType": "syncMissingScriptSignoffs" if has_missing_required_signoff_rows else "focusScriptSignoff",
                "targetScriptName": (first_unsigned_script or {}).get("scriptName") or "",
                "targetStatus": "validated",
            }
        )
    if readiness.get("mode") == "assisted" and not checklist.get("pilotUsersConfigured"):
        actions.append(
            _build_manual_action_item(
                code="configure_pilot_users",
                label="Configure CUTOVER_PILOT_USER_IDS before assisted pilot expansion.",
                guidance_title="Configure assisted pilot user scope",
                guidance_steps=[
                    "Set CUTOVER_PILOT_USER_IDS to the approved worker character IDs for the pilot wave.",
                    "Persist evidence again after the setting change so the stored preflight baseline reflects the new scope.",
                    "Re-check claim, release, and temp-done flows with one pilot user before expanding coverage.",
                ],
                target_setting="CUTOVER_PILOT_USER_IDS",
            )
        )
    rollback_summary = readiness.get("rollbackSummary") or {}
    rollback_status = rollback_summary.get("status") or {}
    if readiness.get("mode") in {"assisted", "primary"} and not rollback_status.get("gateSatisfied", True):
        actions.append(
            _build_manual_action_item(
                code="refresh_rollback_evidence",
                label="Refresh rollback runbook review and rollback drill evidence before rollout continues.",
                guidance_title="Refresh rollback evidence",
                guidance_steps=[
                    "Review the cutover runbook with the current incident commander and rollback approver, then record the runbook review date in the rollback evidence panel.",
                    "Run or confirm the latest rollback drill and record the most recent successful rollback exercise date in the same panel.",
                    "Keep both evidence dates within CUTOVER_ROLLBACK_EVIDENCE_MAX_AGE_DAYS and persist evidence again so preflight reflects the refreshed recovery posture.",
                ],
                action_type="focusRollbackEvidence",
                target_setting="CUTOVER_RUNBOOK_REVIEWED_AT,CUTOVER_ROLLBACK_TESTED_AT",
                target_evidence_type="runbook_review",
            )
        )
    if not checklist.get("syncHealthy"):
        actions.append(
            _build_manual_action_item(
                code="resolve_sync_posture",
                label="Resolve stale or failed sync feeds before go-live.",
                guidance_title="Recover sync posture before rollout",
                guidance_steps=[
                    "Inspect the Shadow Summary and Cutover Readiness panels for the affected corporation and sync kind.",
                    "Run the matching corp sync refresh or fix the upstream ESI/auth issue until stale and failed feeds clear.",
                    "Persist evidence again after recovery so the latest preflight snapshot captures the healthy sync state.",
                ],
                action_type="focusShadowSync",
            )
        )
    if not checklist.get("workforceHealthy"):
        actions.append(
            _build_manual_action_item(
                code="clear_workforce_blockers",
                label="Clear failed or stale workforce items before assisted cutover.",
                guidance_title="Clear workforce execution blockers",
                guidance_steps=[
                    "Open the affected project in the Director Flight Deck and review failed items, stale jobs data, and temp-done age.",
                    "Retry, release, or re-dispatch blocked work until failed items and stale project warnings are gone.",
                    "Persist evidence again after cleanup so the stored preflight reflects the recovered workforce posture.",
                ],
                action_type="focusWorkforceBlockers",
            )
        )
    if workforce_provenance_warning:
        actions.append(
            _build_manual_action_item(
                code="review_manual_intervention_growth",
                label="Review rising manual workforce interventions before expanding assisted rollout.",
                guidance_title="Review manual intervention growth",
                guidance_steps=[
                    workforce_provenance_warning,
                    "Inspect the Event Provenance and preflight diff panels to confirm whether manual cleanup is growing faster than recommendation-driven handling.",
                    "Review the affected project bottlenecks in Director Flight Deck and decide whether rollout should pause until workforce cleanup posture stabilizes.",
                    "Persist a fresh preflight snapshot after mitigation or escalation so the next comparison reflects the new operating state.",
                ],
            )
        )
    if role_assignments.get("unassignedCount") == 0 and script_signoffs.get("pendingCount") == 0 and readiness.get("blockers"):
        actions.append(
            _build_manual_action_item(
                code="review_remaining_blockers",
                label="Review remaining blockers manually; governance prerequisites are already satisfied.",
                guidance_title="Review residual non-governance blockers",
                guidance_steps=[
                    "Use the blocker list and diff panel to identify what changed since the last stored preflight baseline.",
                    "Clear the remaining operational blocker or document why the rollout should still stay NO-GO.",
                    "Persist a new evidence snapshot once the decision or remediation is complete.",
                ],
            )
        )
    return actions


def _build_recommended_actions(action_items: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("label") or "") for item in action_items if item.get("label")]


def _find_action_item_by_code(action_items: list[dict[str, Any]], code: str) -> dict[str, Any] | None:
    return next((item for item in action_items if item.get("code") == code), None)


def _find_action_item_by_label(action_items: list[dict[str, Any]], label: str) -> dict[str, Any] | None:
    return next((item for item in action_items if item.get("label") == label), None)


def _find_first_action_item_by_codes(action_items: list[dict[str, Any]], codes: list[str]) -> dict[str, Any] | None:
    for code in codes:
        action_item = _find_action_item_by_code(action_items, code)
        if action_item:
            return action_item
    return None


def _match_action_item_for_blocker(blocker: str, action_items: list[dict[str, Any]]) -> dict[str, Any] | None:
    blocker_action_map = {
        "Sync posture has stale or failed feeds.": "resolve_sync_posture",
        "Workforce posture has failed work items or stale project freshness.": "clear_workforce_blockers",
        "One or more critical script sign-offs are blocked.": "validate_critical_scripts",
        "Legacy compatibility mode is disabled before script sign-off.": "validate_critical_scripts",
        "Primary mode requires all critical scripts to be signed off.": "validate_critical_scripts",
        "Compatibility mode cannot be disabled until all critical scripts are signed off.": "validate_critical_scripts",
        "Assisted mode is active but no pilot users are configured.": "configure_pilot_users",
        "Cutover and rollback ownership is incomplete.": "assign_missing_roles",
        "Rollback runbook review or rollback drill evidence is missing or stale.": "refresh_rollback_evidence",
    }
    if blocker == "Pilot rollout evidence is not yet ready for assisted expansion.":
        return _find_first_action_item_by_codes(
            action_items,
            [
                "reduce_pilot_retry_pressure",
                "clear_pilot_escalations",
                "clear_pilot_verification_backlog",
                "clear_pilot_failed_backlog",
                "stabilize_pilot_manual_interventions",
                "clear_workforce_blockers",
            ],
        )
    action_code = blocker_action_map.get(blocker)
    if not action_code:
        return None
    return _find_action_item_by_code(action_items, action_code)


def _build_change_detail_rows(
    *,
    blockers_added: list[str],
    blockers_removed: list[str],
    actions_added: list[str],
    actions_removed: list[str],
    workforce_provenance_delta: dict[str, int],
    workforce_provenance_warning: str,
    rollback_change_rows: list[dict[str, Any]],
    recommended_action_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        *[
            {
                "label": "Blocker added",
                "value": item,
                "tone": "bad",
                "actionItem": _match_action_item_for_blocker(item, recommended_action_items),
            }
            for item in blockers_added
        ],
        *[
            {
                "label": "Blocker removed",
                "value": item,
                "tone": "good",
                "actionItem": None,
            }
            for item in blockers_removed
        ],
        *[
            {
                "label": "Action added",
                "value": item,
                "tone": "warn",
                "actionItem": _find_action_item_by_label(recommended_action_items, item),
            }
            for item in actions_added
        ],
        *[
            {
                "label": "Action removed",
                "value": item,
                "tone": "good",
                "actionItem": None,
            }
            for item in actions_removed
        ],
        *([
            {
                "label": "Manual interventions increased",
                "value": f"manual={workforce_provenance_delta['manual']:+d}",
                "tone": "bad",
                "actionItem": _find_action_item_by_code(recommended_action_items, "review_manual_intervention_growth")
                or _find_action_item_by_code(recommended_action_items, "clear_workforce_blockers"),
            }
        ] if workforce_provenance_delta.get("manual", 0) > 0 else []),
        *([
            {
                "label": "Recommended interventions decreased",
                "value": f"recommended={workforce_provenance_delta['recommended']:+d}",
                "tone": "warn",
                "actionItem": _find_action_item_by_code(recommended_action_items, "review_manual_intervention_growth")
                or _find_action_item_by_code(recommended_action_items, "clear_workforce_blockers"),
            }
        ] if workforce_provenance_delta.get("recommended", 0) < 0 else []),
        *([
            {
                "label": "Workforce provenance warning",
                "value": workforce_provenance_warning,
                "tone": "bad",
                "actionItem": _find_action_item_by_code(recommended_action_items, "review_manual_intervention_growth")
                or _find_action_item_by_code(recommended_action_items, "clear_workforce_blockers"),
            }
        ] if workforce_provenance_warning else []),
        *rollback_change_rows,
    ]


def _build_rollback_comparison_rows(
    *,
    current_rollback_summary: dict[str, Any],
    previous_rollback_summary: dict[str, Any],
    has_stored_preflight_baseline: bool,
    recommended_action_items: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    current_status = current_rollback_summary.get("status") or {}
    previous_status = previous_rollback_summary.get("status") or {}
    current_runbook_reviewed_at = current_rollback_summary.get("runbookReviewedAt") or ""
    previous_runbook_reviewed_at = previous_rollback_summary.get("runbookReviewedAt") or ""
    current_rollback_tested_at = current_rollback_summary.get("rollbackTestedAt") or ""
    previous_rollback_tested_at = previous_rollback_summary.get("rollbackTestedAt") or ""
    rollback_action_item = _find_action_item_by_code(recommended_action_items, "refresh_rollback_evidence")

    comparison = {
        "previousGateSatisfied": bool(previous_status.get("gateSatisfied")) if has_stored_preflight_baseline else None,
        "currentGateSatisfied": bool(current_status.get("gateSatisfied")),
        "gateChanged": has_stored_preflight_baseline
        and bool(previous_status.get("gateSatisfied")) != bool(current_status.get("gateSatisfied")),
        "previousRunbookReviewedAt": previous_runbook_reviewed_at if has_stored_preflight_baseline else None,
        "currentRunbookReviewedAt": current_runbook_reviewed_at,
        "runbookChanged": has_stored_preflight_baseline and previous_runbook_reviewed_at != current_runbook_reviewed_at,
        "previousRollbackTestedAt": previous_rollback_tested_at if has_stored_preflight_baseline else None,
        "currentRollbackTestedAt": current_rollback_tested_at,
        "rollbackTestChanged": has_stored_preflight_baseline and previous_rollback_tested_at != current_rollback_tested_at,
    }

    if not has_stored_preflight_baseline:
        return comparison, []

    rows: list[dict[str, Any]] = []
    if comparison["gateChanged"]:
        current_gate_satisfied = bool(current_status.get("gateSatisfied"))
        rows.append(
            {
                "label": "Rollback gate changed",
                "value": f"{'satisfied' if bool(previous_status.get('gateSatisfied')) else 'blocked'} -> {'satisfied' if current_gate_satisfied else 'blocked'}",
                "tone": "good" if current_gate_satisfied else "bad",
                "actionItem": None if current_gate_satisfied else rollback_action_item,
            }
        )
    if comparison["runbookChanged"]:
        rows.append(
            {
                "label": "Runbook evidence updated",
                "value": f"{previous_runbook_reviewed_at or 'missing'} -> {current_runbook_reviewed_at or 'missing'}",
                "tone": "good" if current_runbook_reviewed_at else "warn",
                "actionItem": None if current_runbook_reviewed_at else rollback_action_item,
            }
        )
    if comparison["rollbackTestChanged"]:
        rows.append(
            {
                "label": "Rollback drill evidence updated",
                "value": f"{previous_rollback_tested_at or 'missing'} -> {current_rollback_tested_at or 'missing'}",
                "tone": "good" if current_rollback_tested_at else "warn",
                "actionItem": None if current_rollback_tested_at else rollback_action_item,
            }
        )
    return comparison, rows


def _get_workforce_provenance_from_readiness(readiness: dict[str, Any]) -> dict[str, int]:
    return (
        (((readiness.get("shadow") or {}).get("workforce") or {}).get("recentEventSources"))
        or {"total": 0, "recommended": 0, "manual": 0, "system": 0}
    )


def _build_workforce_provenance_comparison(
    *,
    current_workforce_provenance: dict[str, Any],
    previous_workforce_provenance: dict[str, Any],
    has_stored_preflight_baseline: bool,
) -> tuple[dict[str, int], str]:
    workforce_provenance_delta = {
        "total": int(current_workforce_provenance.get("total") or 0)
        - int(previous_workforce_provenance.get("total") or 0),
        "recommended": int(current_workforce_provenance.get("recommended") or 0)
        - int(previous_workforce_provenance.get("recommended") or 0),
        "manual": int(current_workforce_provenance.get("manual") or 0)
        - int(previous_workforce_provenance.get("manual") or 0),
        "system": int(current_workforce_provenance.get("system") or 0)
        - int(previous_workforce_provenance.get("system") or 0),
    }
    if not has_stored_preflight_baseline:
        return workforce_provenance_delta, ""

    manual_now = int(current_workforce_provenance.get("manual") or 0)
    recommended_now = int(current_workforce_provenance.get("recommended") or 0)
    manual_prev = int(previous_workforce_provenance.get("manual") or 0)
    recommended_prev = int(previous_workforce_provenance.get("recommended") or 0)
    workforce_provenance_warning = ""
    if manual_now > recommended_now and manual_now > manual_prev:
        workforce_provenance_warning = (
            "Manual workforce interventions now exceed recommended-action interventions and increased versus the stored baseline."
        )
    elif workforce_provenance_delta["manual"] > 0 and recommended_now <= recommended_prev:
        workforce_provenance_warning = (
            "Manual workforce interventions increased without a matching rise in recommended-action handling."
        )
    return workforce_provenance_delta, workforce_provenance_warning


def _build_preflight_relevant_pilot_blockers(pilot: dict[str, Any], *, mode: str) -> list[str]:
    if mode != "assisted":
        return []
    if (pilot.get("pilotStage") or "pre_pilot") == "pre_pilot":
        return []
    if pilot.get("pilotExpansionGoNoGo"):
        return []
    return ["Pilot rollout evidence is not yet ready for assisted expansion."]


def _snapshot_summary(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not snapshot:
        return None
    return {key: value for key, value in snapshot.items() if key != "payload"}


def _build_changes_vs_stored_preflight(
    *,
    readiness: dict[str, Any],
    effective_go_no_go: bool,
    preflight_blockers: list[str],
    recommended_actions: list[str],
    recommended_action_items: list[dict[str, Any]],
    latest_preflight_payload: dict[str, Any],
) -> dict[str, Any]:
    previous_readiness = latest_preflight_payload.get("readiness") or {}
    previous_current = latest_preflight_payload.get("current") or {}
    current_rollback_summary = readiness.get("rollbackSummary") or {}
    previous_rollback_summary = latest_preflight_payload.get("rollbackSummary") or previous_readiness.get("rollbackSummary") or {}
    previous_blockers = list(latest_preflight_payload.get("preflightBlockers") or previous_readiness.get("blockers") or [])
    current_blockers = list(preflight_blockers)
    previous_actions = list(latest_preflight_payload.get("recommendedActions") or [])
    current_workforce_provenance = _get_workforce_provenance_from_readiness(readiness)
    previous_workforce_provenance = previous_current.get("workforceProvenance") or {
        "total": 0,
        "recommended": 0,
        "manual": 0,
        "system": 0,
    }
    blockers_added = [item for item in current_blockers if item not in previous_blockers]
    blockers_removed = [item for item in previous_blockers if item not in current_blockers]
    actions_added = [item for item in recommended_actions if item not in previous_actions]
    actions_removed = [item for item in previous_actions if item not in recommended_actions]
    workforce_provenance_delta, workforce_provenance_warning = _build_workforce_provenance_comparison(
        current_workforce_provenance=current_workforce_provenance,
        previous_workforce_provenance=previous_workforce_provenance,
        has_stored_preflight_baseline=bool(latest_preflight_payload),
    )
    rollback_comparison, rollback_change_rows = _build_rollback_comparison_rows(
        current_rollback_summary=current_rollback_summary,
        previous_rollback_summary=previous_rollback_summary,
        has_stored_preflight_baseline=bool(latest_preflight_payload),
        recommended_action_items=recommended_action_items,
    )

    return {
        "hasStoredBaseline": bool(latest_preflight_payload),
        "previousGoNoGo": latest_preflight_payload.get("effectiveGoNoGo", previous_readiness.get("goNoGo"))
        if latest_preflight_payload
        else None,
        "currentGoNoGo": effective_go_no_go,
        "goNoGoChanged": bool(latest_preflight_payload)
        and latest_preflight_payload.get("effectiveGoNoGo", previous_readiness.get("goNoGo")) != effective_go_no_go,
        "previousMode": previous_readiness.get("mode") if latest_preflight_payload else None,
        "currentMode": readiness.get("mode"),
        "modeChanged": bool(latest_preflight_payload)
        and previous_readiness.get("mode") != readiness.get("mode"),
        "blockersAdded": blockers_added,
        "blockersRemoved": blockers_removed,
        "actionsAdded": actions_added,
        "actionsRemoved": actions_removed,
        "rollbackComparison": rollback_comparison,
        "previousWorkforceProvenance": previous_workforce_provenance if latest_preflight_payload else None,
        "currentWorkforceProvenance": current_workforce_provenance,
        "workforceProvenanceDelta": workforce_provenance_delta,
        "workforceProvenanceWarning": workforce_provenance_warning,
        "detailRows": _build_change_detail_rows(
            blockers_added=blockers_added,
            blockers_removed=blockers_removed,
            actions_added=actions_added,
            actions_removed=actions_removed,
            workforce_provenance_delta=workforce_provenance_delta,
            workforce_provenance_warning=workforce_provenance_warning,
            rollback_change_rows=rollback_change_rows,
            recommended_action_items=recommended_action_items,
        ),
    }


def generate_cutover_preflight_report(*, persist: bool = False, trend_limit: int = 7, include_pilot: bool = True) -> dict[str, Any]:
    stored = persist_daily_report_snapshots() if persist else []
    readiness = generate_cutover_readiness_report()
    trend = list_cutover_readiness_trend(limit=trend_limit)
    latest_snapshot = list_recent_report_snapshots(limit=1, report_name="cutover_readiness")
    latest_preflight_snapshot = list_recent_report_snapshots(limit=1, report_name="cutover_preflight")
    latest_trend = trend[0] if trend else None
    latest_snapshot_item = latest_snapshot[0] if latest_snapshot else None
    latest_preflight_item = latest_preflight_snapshot[0] if latest_preflight_snapshot else None
    latest_preflight_payload = (latest_preflight_item or {}).get("payload") or {}

    current_assigned_roles = int((readiness.get("roleAssignments") or {}).get("assignedCount") or 0)
    current_required_roles = int((readiness.get("roleAssignments") or {}).get("requiredCount") or 0)
    current_validated_signoffs = int((readiness.get("scriptSignoffs") or {}).get("validatedCount") or 0)
    current_required_signoffs = int((readiness.get("scriptSignoffs") or {}).get("requiredCount") or 0)
    current_blocker_count = len(readiness.get("blockers") or [])
    current_incident_count = int((readiness.get("shadow") or {}).get("incidentCount") or 0)
    workforce_provenance = _get_workforce_provenance_from_readiness(readiness)
    previous_workforce_provenance = ((latest_preflight_payload.get("current") or {}).get("workforceProvenance")) or {
        "total": 0,
        "recommended": 0,
        "manual": 0,
        "system": 0,
    }
    _workforce_provenance_delta, workforce_provenance_warning = _build_workforce_provenance_comparison(
        current_workforce_provenance=workforce_provenance,
        previous_workforce_provenance=previous_workforce_provenance,
        has_stored_preflight_baseline=bool(latest_preflight_payload),
    )

    deltas = {
        "assignedRoles": current_assigned_roles - int((latest_trend or {}).get("assignedRoles") or 0),
        "validatedSignoffs": current_validated_signoffs - int((latest_trend or {}).get("validatedSignoffs") or 0),
        "blockerCount": current_blocker_count - int((latest_trend or {}).get("blockerCount") or 0),
        "incidentCount": current_incident_count - int((latest_trend or {}).get("incidentCount") or 0),
    }
    recommended_action_items = _build_recommended_action_items(
        readiness,
        has_stored_preflight_baseline=bool(latest_preflight_payload),
        workforce_provenance_warning=workforce_provenance_warning,
    )
    recommended_actions = _build_recommended_actions(recommended_action_items)
    preflight_blockers = list(readiness.get("blockers") or [])
    if readiness.get("mode") in {"assisted", "primary"} and workforce_provenance_warning:
        preflight_blockers.append("Manual workforce interventions are rising above recommendation-driven handling.")
    base_effective_go_no_go = not preflight_blockers

    pilot = {
        "pilotStage": "pre_pilot",
        "pilotStartGoNoGo": True,
        "pilotExpansionGoNoGo": True,
        "pilotUserIds": [],
        "operationalSummary": {"incidentCount": 0},
        "policySummary": {"status": {"withinPolicy": True}},
        "recommendedActions": recommended_actions,
        "recommendedActionItems": recommended_action_items,
    }
    pilot_preflight_blockers: list[str] = []
    effective_go_no_go = base_effective_go_no_go

    if include_pilot:
        pilot_payload = {
            "readiness": readiness,
            "effectiveGoNoGo": base_effective_go_no_go,
            "preflightBlockers": list(preflight_blockers),
            "recommendedActions": recommended_actions,
            "recommendedActionItems": recommended_action_items,
        }

        from apps.common.pilot import build_cutover_pilot_readiness_report

        pilot = build_cutover_pilot_readiness_report(preflight=pilot_payload)
        pilot_preflight_blockers = _build_preflight_relevant_pilot_blockers(pilot, mode=str(readiness.get("mode") or ""))
        preflight_blockers.extend(pilot_preflight_blockers)
        effective_go_no_go = not preflight_blockers
        recommended_action_items = list(pilot.get("recommendedActionItems") or recommended_action_items)
        recommended_actions = list(pilot.get("recommendedActions") or recommended_actions)

    payload = {
        "readiness": readiness,
        "effectiveGoNoGo": effective_go_no_go,
        "preflightBlockers": preflight_blockers,
        "rollbackSummary": readiness.get("rollbackSummary") or {},
        "pilot": {
            "pilotStage": pilot.get("pilotStage") or "pre_pilot",
            "pilotStartGoNoGo": bool(pilot.get("pilotStartGoNoGo")),
            "pilotExpansionGoNoGo": bool(pilot.get("pilotExpansionGoNoGo")),
            "pilotUserCount": len(pilot.get("pilotUserIds") or []),
            "incidentCount": int((pilot.get("operationalSummary") or {}).get("incidentCount") or 0),
            "policyBlocked": not bool(((pilot.get("policySummary") or {}).get("status") or {}).get("withinPolicy", True)),
            "preflightBlockers": pilot_preflight_blockers,
            "recommendedActions": recommended_actions,
        },
        "current": {
            "assignedRoles": current_assigned_roles,
            "requiredRoles": current_required_roles,
            "validatedSignoffs": current_validated_signoffs,
            "requiredSignoffs": current_required_signoffs,
            "blockerCount": current_blocker_count,
            "preflightBlockerCount": len(preflight_blockers),
            "incidentCount": current_incident_count,
            "pilotStage": pilot.get("pilotStage") or "pre_pilot",
            "pilotExpansionGoNoGo": bool(pilot.get("pilotExpansionGoNoGo")),
            "pilotPolicyBlocked": not bool(((pilot.get("policySummary") or {}).get("status") or {}).get("withinPolicy", True)),
            "pilotIncidentCount": int((pilot.get("operationalSummary") or {}).get("incidentCount") or 0),
            "workforceProvenance": workforce_provenance,
        },
        "latestStoredSnapshot": _snapshot_summary(latest_snapshot_item),
        "latestStoredPreflightSnapshot": _snapshot_summary(latest_preflight_item),
        "trend": trend,
        "deltasVsLatestSnapshot": deltas,
        "recommendedActions": recommended_actions,
        "recommendedActionItems": recommended_action_items,
        "changesVsStoredPreflight": _build_changes_vs_stored_preflight(
            readiness=readiness,
            effective_go_no_go=effective_go_no_go,
            preflight_blockers=preflight_blockers,
            recommended_actions=recommended_actions,
            recommended_action_items=recommended_action_items,
            latest_preflight_payload=latest_preflight_payload,
        ),
        "storedSnapshots": [
            {
                "reportName": snapshot.report_name,
                "snapshotDate": snapshot.snapshot_date.isoformat(),
            }
            for snapshot in stored
        ],
    }
    if persist:
        preflight_snapshot = _persist_cutover_preflight_snapshot(payload)
        payload["storedSnapshots"].append(
            {
                "reportName": preflight_snapshot.report_name,
                "snapshotDate": preflight_snapshot.snapshot_date.isoformat(),
            }
        )
        refreshed_preflight_snapshot = list_recent_report_snapshots(limit=1, report_name="cutover_preflight")
        payload["latestStoredPreflightSnapshot"] = _snapshot_summary(
            refreshed_preflight_snapshot[0] if refreshed_preflight_snapshot else None
        )
    return payload


def _persist_cutover_preflight_snapshot(payload: dict[str, Any], *, snapshot_date: date | None = None):
    return persist_report_snapshot(
        report_name="cutover_preflight",
        payload=payload,
        incident_count=int((payload.get("current") or {}).get("incidentCount") or 0),
        go_no_go=bool(payload.get("effectiveGoNoGo")),
        snapshot_date=snapshot_date,
    )