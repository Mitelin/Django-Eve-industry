from __future__ import annotations

from datetime import date
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.common.models import RollbackEvidence, RollbackEvidenceEvent


def _parse_cutover_date(value: str) -> date | None:
    raw_value = (value or "").strip()
    if not raw_value:
        return None
    try:
        return date.fromisoformat(raw_value)
    except ValueError:
        return None


def _get_env_defaults() -> dict[str, dict[str, Any]]:
    return {
        RollbackEvidence.EvidenceType.RUNBOOK_REVIEW: {
            "label": "Runbook review",
            "envDate": _parse_cutover_date(settings.CUTOVER_RUNBOOK_REVIEWED_AT),
        },
        RollbackEvidence.EvidenceType.ROLLBACK_DRILL: {
            "label": "Rollback drill",
            "envDate": _parse_cutover_date(settings.CUTOVER_ROLLBACK_TESTED_AT),
        },
    }


def update_rollback_evidence(
    *,
    evidence_type: str,
    evidence_date: date | None,
    changed_by: str = "",
    notes: str = "",
) -> RollbackEvidence:
    evidence, _created = RollbackEvidence.objects.get_or_create(evidence_type=evidence_type)
    previous_evidence_date = evidence.evidence_date
    evidence.evidence_date = evidence_date
    evidence.recorded_by = changed_by
    evidence.notes = notes
    evidence.save(update_fields=["evidence_date", "recorded_by", "notes", "updated_at"])
    RollbackEvidenceEvent.objects.create(
        evidence=evidence,
        previous_evidence_date=previous_evidence_date,
        new_evidence_date=evidence_date,
        changed_by=changed_by,
        notes=notes,
        effective_at=timezone.now(),
    )
    return evidence


def get_rollback_evidence_summary() -> dict[str, Any]:
    today = timezone.localdate()
    max_age_days = int(settings.CUTOVER_ROLLBACK_EVIDENCE_MAX_AGE_DAYS or 14)
    env_defaults = _get_env_defaults()
    evidence_by_type = {item.evidence_type: item for item in RollbackEvidence.objects.all()}
    items: list[dict[str, Any]] = []

    for evidence_type, defaults in env_defaults.items():
        evidence = evidence_by_type.get(evidence_type)
        resolved_date = evidence.evidence_date if evidence and evidence.evidence_date else defaults["envDate"]
        age_days = (today - resolved_date).days if resolved_date else None
        current = age_days is not None and age_days <= max_age_days
        source = "db" if evidence and evidence.evidence_date else "env" if defaults["envDate"] else "missing"
        items.append(
            {
                "evidenceType": evidence_type,
                "label": defaults["label"],
                "evidenceDate": resolved_date.isoformat() if resolved_date else None,
                "ageDays": age_days,
                "current": current,
                "source": source,
                "recordedBy": evidence.recorded_by if evidence else "",
                "notes": evidence.notes if evidence else "",
            }
        )

    items_by_type = {item["evidenceType"]: item for item in items}
    runbook_item = items_by_type[RollbackEvidence.EvidenceType.RUNBOOK_REVIEW]
    rollback_item = items_by_type[RollbackEvidence.EvidenceType.ROLLBACK_DRILL]
    within_policy = bool(runbook_item["current"] and rollback_item["current"])
    enforcement_enabled = bool(settings.CUTOVER_ENFORCE_ROLLBACK_EVIDENCE)
    recent_events = RollbackEvidenceEvent.objects.select_related("evidence")[:10]

    return {
        "enforcementEnabled": enforcement_enabled,
        "maxAgeDays": max_age_days,
        "runbookReviewedAt": runbook_item["evidenceDate"],
        "rollbackTestedAt": rollback_item["evidenceDate"],
        "runbookAgeDays": runbook_item["ageDays"],
        "rollbackTestAgeDays": rollback_item["ageDays"],
        "status": {
            "runbookReviewed": bool(runbook_item["evidenceDate"]),
            "rollbackTestRecorded": bool(rollback_item["evidenceDate"]),
            "runbookCurrent": bool(runbook_item["current"]),
            "rollbackTestCurrent": bool(rollback_item["current"]),
            "withinPolicy": within_policy,
            "gateSatisfied": within_policy or not enforcement_enabled,
        },
        "items": items,
        "recentEvents": [
            {
                "evidenceType": event.evidence.evidence_type,
                "previousEvidenceDate": event.previous_evidence_date.isoformat() if event.previous_evidence_date else None,
                "newEvidenceDate": event.new_evidence_date.isoformat() if event.new_evidence_date else None,
                "changedBy": event.changed_by,
                "notes": event.notes,
                "effectiveAt": event.effective_at.isoformat(),
            }
            for event in recent_events
        ],
    }