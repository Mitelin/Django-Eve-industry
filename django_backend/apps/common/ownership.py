from __future__ import annotations

from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.common.models import CutoverRoleAssignment, CutoverRoleEvent


def get_required_cutover_roles() -> dict[str, str]:
    return {
        "cutoverLead": settings.CUTOVER_LEAD,
        "incidentCommander": settings.CUTOVER_INCIDENT_COMMANDER,
        "backendOwner": settings.CUTOVER_BACKEND_OWNER,
        "dataOwner": settings.CUTOVER_DATA_OWNER,
        "directorRepresentative": settings.CUTOVER_DIRECTOR_REPRESENTATIVE,
        "rollbackApprover": settings.CUTOVER_ROLLBACK_APPROVER,
    }


def ensure_cutover_role_assignments() -> list[CutoverRoleAssignment]:
    assignments: list[CutoverRoleAssignment] = []
    for role_name, env_value in get_required_cutover_roles().items():
        defaults: dict[str, Any] = {}
        if env_value:
            defaults = {
                "assigned_to": env_value,
                "assigned_at": timezone.now(),
            }
        assignment, _created = CutoverRoleAssignment.objects.get_or_create(role_name=role_name, defaults=defaults)
        assignments.append(assignment)
    return assignments


def sync_missing_cutover_role_assignments(*, changed_by: str = "", notes: str = "") -> list[CutoverRoleAssignment]:
    synced: list[CutoverRoleAssignment] = []
    for role_name, env_value in get_required_cutover_roles().items():
        if not env_value:
            continue
        assignment, _created = CutoverRoleAssignment.objects.get_or_create(role_name=role_name)
        if assignment.assigned_to:
            continue
        synced.append(
            update_cutover_role_assignment(
                role_name=role_name,
                assigned_to=env_value,
                changed_by=changed_by,
                notes=notes or "Synced from cutover role defaults.",
            )
        )
    return synced


def update_cutover_role_assignment(
    *,
    role_name: str,
    assigned_to: str,
    changed_by: str = "",
    notes: str = "",
) -> CutoverRoleAssignment:
    assignment, _created = CutoverRoleAssignment.objects.get_or_create(role_name=role_name)
    previous_assigned_to = assignment.assigned_to
    assignment.assigned_to = assigned_to
    assignment.notes = notes
    assignment.assigned_at = timezone.now() if assigned_to else None
    assignment.save(update_fields=["assigned_to", "notes", "assigned_at", "updated_at"])
    CutoverRoleEvent.objects.create(
        assignment=assignment,
        previous_assigned_to=previous_assigned_to,
        new_assigned_to=assigned_to,
        changed_by=changed_by,
        notes=notes,
        effective_at=timezone.now(),
    )
    return assignment


def get_cutover_role_summary() -> dict[str, Any]:
    required_roles = get_required_cutover_roles()
    assignments_by_name = {item.role_name: item for item in CutoverRoleAssignment.objects.all()}

    items: list[dict[str, Any]] = []
    for role_name, env_value in required_roles.items():
        assignment = assignments_by_name.get(role_name)
        assigned_to = assignment.assigned_to if assignment and assignment.assigned_to else env_value
        assigned_at = assignment.assigned_at.isoformat() if assignment and assignment.assigned_at else None
        notes = assignment.notes if assignment else ""
        source = "db" if assignment and assignment.assigned_to else "env" if env_value else "missing"
        items.append(
            {
                "roleName": role_name,
                "assignedTo": assigned_to,
                "assignedAt": assigned_at,
                "notes": notes,
                "source": source,
                "assigned": bool(assigned_to),
            }
        )

    recent_events = CutoverRoleEvent.objects.select_related("assignment")[:10]
    assigned_count = sum(1 for item in items if item["assigned"])
    return {
        "requiredCount": len(items),
        "assignedCount": assigned_count,
        "unassignedCount": len(items) - assigned_count,
        "allRequiredAssigned": assigned_count == len(items),
        "items": items,
        "recentEvents": [
            {
                "roleName": event.assignment.role_name,
                "previousAssignedTo": event.previous_assigned_to or None,
                "newAssignedTo": event.new_assigned_to or None,
                "changedBy": event.changed_by,
                "notes": event.notes,
                "effectiveAt": event.effective_at.isoformat(),
            }
            for event in recent_events
        ],
    }