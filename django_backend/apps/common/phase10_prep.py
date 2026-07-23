from __future__ import annotations

from typing import Any

from apps.common.cutover import generate_cutover_readiness_report
from apps.common.phase9_exit import generate_phase9_exit_report


def _check(*, code: str, label: str, satisfied: bool, blocking: bool, detail: str = "") -> dict[str, Any]:
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


def generate_phase10_prep_report() -> dict[str, Any]:
    readiness = generate_cutover_readiness_report()
    phase9_exit = generate_phase9_exit_report()
    readiness_checklist = readiness.get("checklist") or {}
    phase9_decisions = phase9_exit.get("decisions") or {}
    current_mode = str(readiness.get("mode") or "shadow")
    compatibility_mode = bool(readiness.get("compatibilityMode"))

    checks = [
        _check(
            code="not_shadow_mode",
            label="System is beyond shadow mode.",
            satisfied=current_mode != "shadow",
            blocking=True,
            detail=f"current={current_mode}",
        ),
        _check(
            code="phase9_primary_ready",
            label="Phase 9 exit artifact says primary mode is ready.",
            satisfied=bool(phase9_decisions.get("primaryModeReady")),
            blocking=True,
            detail=f"assistedExitReady={bool(phase9_decisions.get('assistedExitReady'))}",
        ),
        _check(
            code="critical_scripts_signed_off",
            label="Critical scripts are signed off.",
            satisfied=bool(readiness_checklist.get("criticalScriptsSignedOff")),
            blocking=True,
            detail=f"validated={((readiness.get('scriptSignoffs') or {}).get('validatedCount') or 0)}/{((readiness.get('scriptSignoffs') or {}).get('requiredCount') or 0)}",
        ),
        _check(
            code="rollback_gate_satisfied",
            label="Rollback evidence is current.",
            satisfied=bool(readiness_checklist.get("rollbackEvidenceGateSatisfied")),
            blocking=True,
            detail=f"current={bool(readiness_checklist.get('rollbackEvidenceCurrent'))}",
        ),
        _check(
            code="assignment_writes_enabled",
            label="Assignment writes are enabled.",
            satisfied=bool(readiness_checklist.get("assignmentWritesEnabled")),
            blocking=True,
            detail="primary mode requires live writes",
        ),
        _check(
            code="compatibility_retirement_ready",
            label="Compatibility shims can be disabled safely.",
            satisfied=bool(phase9_decisions.get("compatibilityRetirementReady")),
            blocking=False,
            detail=f"compatibilityMode={'on' if compatibility_mode else 'off'}",
        ),
    ]

    blocking_failures = [item for item in checks if item["blocking"] and not item["satisfied"]]
    can_enter_primary_mode = not blocking_failures
    can_disable_compatibility_mode = can_enter_primary_mode and bool(phase9_decisions.get("compatibilityRetirementReady"))

    blockers = []
    if current_mode == "shadow":
        blockers.append("System is still in shadow mode; assisted cutover must complete before primary mode.")
    blockers.extend(list(phase9_exit.get("blockers") or []))
    blockers = _dedupe_text(blockers)

    return {
        "currentMode": current_mode,
        "compatibilityMode": compatibility_mode,
        "decisions": {
            "canEnterPrimaryMode": can_enter_primary_mode,
            "canDisableCompatibilityMode": can_disable_compatibility_mode,
            "requiresLegacyCompatibility": not can_disable_compatibility_mode,
        },
        "summary": {
            "blockingFailureCount": len(blocking_failures),
            "blockerCount": len(blockers),
            "recommendedActionCount": len(phase9_exit.get("recommendedActionItems") or []),
        },
        "checks": checks,
        "blockers": blockers,
        "recommendedActions": list(phase9_exit.get("recommendedActions") or []),
        "recommendedActionItems": list(phase9_exit.get("recommendedActionItems") or []),
        "phase9Exit": {
            "assistedExitReady": bool(phase9_decisions.get("assistedExitReady")),
            "primaryModeReady": bool(phase9_decisions.get("primaryModeReady")),
            "compatibilityRetirementReady": bool(phase9_decisions.get("compatibilityRetirementReady")),
        },
    }