from __future__ import annotations

from typing import Any

from apps.common.cutover import generate_cutover_readiness_report
from apps.common.pilot import generate_cutover_pilot_readiness_report
from apps.common.preflight import generate_cutover_preflight_report


def _build_check(*, code: str, label: str, satisfied: bool, blocking: bool, detail: str = "") -> dict[str, Any]:
    return {
        "code": code,
        "label": label,
        "satisfied": bool(satisfied),
        "blocking": bool(blocking),
        "detail": detail,
    }


def _dedupe_text(items: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        deduped.append(value)
        seen.add(value)
    return deduped


def _dedupe_action_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    for item in items:
        code = str((item or {}).get("code") or "").strip()
        if not code or code in seen_codes:
            continue
        deduped.append(item)
        seen_codes.add(code)
    return deduped


def generate_phase9_exit_report() -> dict[str, Any]:
    readiness = generate_cutover_readiness_report()
    pilot = generate_cutover_pilot_readiness_report()
    preflight = generate_cutover_preflight_report(persist=False, trend_limit=6)

    readiness_checklist = readiness.get("checklist") or {}
    pilot_checklist = pilot.get("checklist") or {}

    checks = [
        _build_check(
            code="readiness_go_no_go",
            label="Cutover readiness is green.",
            satisfied=bool(readiness.get("goNoGo")),
            blocking=True,
            detail=f"mode={readiness.get('mode') or 'n/a'}",
        ),
        _build_check(
            code="preflight_go_no_go",
            label="Preflight effective GO/NO-GO is green.",
            satisfied=bool(preflight.get("effectiveGoNoGo")),
            blocking=True,
            detail=f"blockers={len(preflight.get('preflightBlockers') or [])}",
        ),
        _build_check(
            code="pilot_exit_ready",
            label="Pilot posture is ready to exit assisted mode.",
            satisfied=bool(pilot.get("pilotExpansionGoNoGo")),
            blocking=True,
            detail=f"stage={pilot.get('pilotStage') or 'pre_pilot'}",
        ),
        _build_check(
            code="critical_scripts_signed_off",
            label="Critical scripts are signed off for compatibility retirement.",
            satisfied=bool(readiness_checklist.get("criticalScriptsSignedOff")),
            blocking=True,
            detail=f"validated={((readiness.get('scriptSignoffs') or {}).get('validatedCount') or 0)}/{((readiness.get('scriptSignoffs') or {}).get('requiredCount') or 0)}",
        ),
        _build_check(
            code="rollback_gate_satisfied",
            label="Rollback evidence gate is satisfied.",
            satisfied=bool(readiness_checklist.get("rollbackEvidenceGateSatisfied")),
            blocking=True,
            detail=f"current={bool(readiness_checklist.get('rollbackEvidenceCurrent'))}",
        ),
        _build_check(
            code="assignment_writes_enabled",
            label="Assignment writes are enabled for primary operations.",
            satisfied=bool(readiness_checklist.get("assignmentWritesEnabled")),
            blocking=True,
            detail="primary mode cannot run in read-only assignment mode",
        ),
        _build_check(
            code="compatibility_can_retire",
            label="Compatibility shims can be retired safely.",
            satisfied=bool(readiness_checklist.get("criticalScriptsSignedOff")),
            blocking=False,
            detail="requires critical script sign-off before disabling legacy compatibility",
        ),
        _build_check(
            code="pilot_cycle_completed",
            label="At least one verified pilot cycle completed.",
            satisfied=bool(pilot_checklist.get("firstPilotCycleCompleted")),
            blocking=True,
            detail=f"verified={((pilot.get('activitySummary') or {}).get('verifiedOkCount') or 0)}",
        ),
    ]

    blocking_checks = [check for check in checks if check["blocking"]]
    blocking_failures = [check for check in blocking_checks if not check["satisfied"]]

    assisted_exit_ready = not blocking_failures
    primary_mode_ready = assisted_exit_ready and bool(readiness_checklist.get("criticalScriptsSignedOff"))
    compatibility_retirement_ready = primary_mode_ready and bool(readiness_checklist.get("criticalScriptsSignedOff"))

    blockers = _dedupe_text(
        list(readiness.get("blockers") or [])
        + list(preflight.get("preflightBlockers") or [])
        + list(pilot.get("expansionBlockers") or [])
    )
    action_items = _dedupe_action_items(
        list(preflight.get("recommendedActionItems") or []) + list(pilot.get("recommendedActionItems") or [])
    )

    return {
        "currentMode": readiness.get("mode") or "shadow",
        "compatibilityMode": bool(readiness.get("compatibilityMode")),
        "pilotStage": pilot.get("pilotStage") or "pre_pilot",
        "decisions": {
            "assistedExitReady": assisted_exit_ready,
            "primaryModeReady": primary_mode_ready,
            "compatibilityRetirementReady": compatibility_retirement_ready,
        },
        "summary": {
            "blockingCheckCount": len(blocking_checks),
            "blockingFailureCount": len(blocking_failures),
            "recommendedActionCount": len(action_items),
            "blockerCount": len(blockers),
        },
        "checks": checks,
        "blockers": blockers,
        "recommendedActions": [str(item.get("label") or "") for item in action_items if item.get("label")],
        "recommendedActionItems": action_items,
        "readiness": {
            "goNoGo": bool(readiness.get("goNoGo")),
            "blockers": list(readiness.get("blockers") or []),
        },
        "pilot": {
            "pilotStartGoNoGo": bool(pilot.get("pilotStartGoNoGo")),
            "pilotExpansionGoNoGo": bool(pilot.get("pilotExpansionGoNoGo")),
            "expansionBlockers": list(pilot.get("expansionBlockers") or []),
        },
        "preflight": {
            "effectiveGoNoGo": bool(preflight.get("effectiveGoNoGo")),
            "preflightBlockers": list(preflight.get("preflightBlockers") or []),
        },
    }