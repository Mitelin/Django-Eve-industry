from __future__ import annotations

from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.common.models import ScriptSignoff, ScriptSignoffEvent


def ensure_required_script_signoffs() -> list[ScriptSignoff]:
    signoffs: list[ScriptSignoff] = []
    for script_name in dict.fromkeys(settings.CUTOVER_REQUIRED_SCRIPT_SIGNOFFS):
        signoff, _created = ScriptSignoff.objects.get_or_create(script_name=script_name)
        signoffs.append(signoff)
    return signoffs


def sync_missing_required_script_signoffs(*, changed_by: str = "", notes: str = "") -> list[ScriptSignoff]:
    synced: list[ScriptSignoff] = []
    for script_name in dict.fromkeys(settings.CUTOVER_REQUIRED_SCRIPT_SIGNOFFS):
        signoff, created = ScriptSignoff.objects.get_or_create(script_name=script_name)
        if not created:
            continue
        ScriptSignoffEvent.objects.create(
            signoff=signoff,
            previous_status="",
            new_status=ScriptSignoff.Status.PENDING,
            changed_by=changed_by,
            notes=notes or "Synced from required script inventory.",
            effective_at=timezone.now(),
        )
        synced.append(signoff)
    return synced


def update_script_signoff(
    *,
    script_name: str,
    status: str,
    signed_off_by: str = "",
    notes: str = "",
) -> ScriptSignoff:
    signoff, _created = ScriptSignoff.objects.get_or_create(script_name=script_name)
    previous_status = signoff.status if signoff.pk else ""
    signoff.status = status
    signoff.signed_off_by = signed_off_by
    signoff.notes = notes
    signoff.signed_off_at = timezone.now() if status == ScriptSignoff.Status.VALIDATED else None
    signoff.save(update_fields=["status", "signed_off_by", "notes", "signed_off_at", "updated_at"])
    ScriptSignoffEvent.objects.create(
        signoff=signoff,
        previous_status=previous_status,
        new_status=status,
        changed_by=signed_off_by,
        notes=notes,
        effective_at=timezone.now(),
    )
    return signoff


def get_script_signoff_summary() -> dict[str, Any]:
    signoffs_by_name = {item.script_name: item for item in ScriptSignoff.objects.all()}
    required_names = list(dict.fromkeys(settings.CUTOVER_REQUIRED_SCRIPT_SIGNOFFS))
    extra_names = sorted(name for name in signoffs_by_name if name not in required_names)

    items: list[dict[str, Any]] = []
    for script_name in [*required_names, *extra_names]:
        signoff = signoffs_by_name.get(script_name)
        items.append(
            {
                "scriptName": script_name,
                "required": script_name in required_names,
                "status": signoff.status if signoff else ScriptSignoff.Status.PENDING,
                "source": "db" if signoff else "missing",
                "signedOffBy": signoff.signed_off_by if signoff else "",
                "signedOffAt": signoff.signed_off_at.isoformat() if signoff and signoff.signed_off_at else None,
                "notes": signoff.notes if signoff else "",
            }
        )

    required_items = [item for item in items if item["required"]]
    validated_count = sum(1 for item in required_items if item["status"] == ScriptSignoff.Status.VALIDATED)
    blocked_count = sum(1 for item in required_items if item["status"] == ScriptSignoff.Status.BLOCKED)
    pending_count = sum(1 for item in required_items if item["status"] == ScriptSignoff.Status.PENDING)
    recent_events = ScriptSignoffEvent.objects.select_related("signoff")[:10]

    return {
        "requiredCount": len(required_items),
        "validatedCount": validated_count,
        "blockedCount": blocked_count,
        "pendingCount": pending_count,
        "allRequiredValidated": bool(required_items) and validated_count == len(required_items),
        "items": items,
        "recentEvents": [
            {
                "scriptName": event.signoff.script_name,
                "previousStatus": event.previous_status or None,
                "newStatus": event.new_status,
                "changedBy": event.changed_by,
                "notes": event.notes,
                "effectiveAt": event.effective_at.isoformat(),
            }
            for event in recent_events
        ],
    }