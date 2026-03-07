from __future__ import annotations

from typing import Any

from django.conf import settings

from apps.common.ownership import get_cutover_role_summary
from apps.common.signoffs import get_script_signoff_summary
from apps.common.shadow import generate_shadow_summary_report


def generate_cutover_readiness_report() -> dict[str, Any]:
    shadow = generate_shadow_summary_report()
    role_summary = get_cutover_role_summary()
    script_signoffs = get_script_signoff_summary()
    sync_failed = [
        incident for incident in shadow["incidents"] if incident["scope"] == "sync" and incident["severity"] == "critical"
    ]
    workforce_failed = [
        incident
        for incident in shadow["incidents"]
        if incident["scope"] == "workforce" and incident["code"] in {"failed_work_items", "project_jobs_stale"}
    ]

    roles = {item["roleName"]: item["assignedTo"] for item in role_summary["items"]}
    checklist = {
        "plannerParityGreen": shadow["planner"]["allGoldenMatched"] and shadow["planner"]["allLegacyMatched"],
        "syncHealthy": shadow["sync"]["staleCount"] == 0 and not sync_failed,
        "workforceHealthy": shadow["workforce"]["queue"]["failed"] == 0 and shadow["workforce"]["staleProjectCount"] == 0,
        "criticalScriptsSignedOff": script_signoffs["allRequiredValidated"],
        "compatibilityModeRetained": settings.CUTOVER_COMPATIBILITY_MODE,
        "assignmentWritesEnabled": not settings.CUTOVER_READ_ONLY_ASSIGNMENT,
        "rollbackReadOnlyAvailable": True,
        "pilotUsersConfigured": bool(settings.CUTOVER_PILOT_USER_IDS),
        "pilotUserGuardEnabled": settings.CUTOVER_MODE != "assisted" or bool(settings.CUTOVER_PILOT_USER_IDS),
        "rolesAssigned": role_summary["allRequiredAssigned"],
    }

    blockers: list[str] = []
    if not checklist["plannerParityGreen"]:
        blockers.append("Planner parity is not fully green.")
    if not checklist["syncHealthy"]:
        blockers.append("Sync posture has stale or failed feeds.")
    if not checklist["workforceHealthy"]:
        blockers.append("Workforce posture has failed work items or stale project freshness.")
    if script_signoffs["blockedCount"]:
        blockers.append("One or more critical script sign-offs are blocked.")
    if not checklist["compatibilityModeRetained"] and not checklist["criticalScriptsSignedOff"]:
        blockers.append("Legacy compatibility mode is disabled before script sign-off.")
    if settings.CUTOVER_MODE == "primary" and not checklist["criticalScriptsSignedOff"]:
        blockers.append("Primary mode requires all critical scripts to be signed off.")
    if not settings.CUTOVER_COMPATIBILITY_MODE and not checklist["criticalScriptsSignedOff"]:
        blockers.append("Compatibility mode cannot be disabled until all critical scripts are signed off.")
    if settings.CUTOVER_MODE in {"assisted", "primary"} and not checklist["assignmentWritesEnabled"]:
        blockers.append("Cutover mode requires assignment writes, but read-only assignment is still enabled.")
    if settings.CUTOVER_MODE == "assisted" and not checklist["pilotUsersConfigured"]:
        blockers.append("Assisted mode is active but no pilot users are configured.")
    if not checklist["rolesAssigned"]:
        blockers.append("Cutover and rollback ownership is incomplete.")

    return {
        "mode": settings.CUTOVER_MODE,
        "readOnlyAssignment": settings.CUTOVER_READ_ONLY_ASSIGNMENT,
        "compatibilityMode": settings.CUTOVER_COMPATIBILITY_MODE,
        "pilotUserIds": settings.CUTOVER_PILOT_USER_IDS,
        "roles": roles,
        "roleAssignments": role_summary,
        "checklist": checklist,
        "scriptSignoffs": script_signoffs,
        "blockers": blockers,
        "goNoGo": not blockers,
        "shadow": {
            "incidentCount": shadow["incidentCount"],
            "planner": {
                "scenarioCount": shadow["planner"]["scenarioCount"],
                "matchedGolden": shadow["planner"]["matchedGolden"],
                "matchedLegacy": shadow["planner"]["matchedLegacy"],
            },
            "sync": {
                "staleCount": shadow["sync"]["staleCount"],
                "corporationCount": shadow["sync"]["corporationCount"],
            },
            "workforce": {
                "failed": shadow["workforce"]["queue"]["failed"],
                "tempDone": shadow["workforce"]["queue"]["tempDone"],
                "staleProjectCount": shadow["workforce"]["staleProjectCount"],
                "recentEventSources": shadow["workforce"].get("recentEventSources") or {
                    "total": 0,
                    "recommended": 0,
                    "manual": 0,
                    "system": 0,
                },
            },
        },
    }