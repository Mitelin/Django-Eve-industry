from __future__ import annotations

import json
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpRequest, JsonResponse
from django.shortcuts import render
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from apps.industry_planner.models import Project
from apps.workforce.models import WorkEvent, WorkItem
from apps.workforce.services import (
    InvalidWorkItemTransition,
    NoAvailableWorkItem,
    StaleVerificationData,
    VerificationWindowOpen,
    WorkforceService,
)


workforce_service = WorkforceService()


def ui_home(request: HttpRequest):
    return render(request, "workforce/ui_home.html")


@ensure_csrf_cookie
def director_screen(request: HttpRequest):
    return render(request, "workforce/director_screen.html")


def worker_screen(request: HttpRequest):
    return render(request, "workforce/worker_screen.html")


def _parse_json_body(request: HttpRequest) -> dict[str, Any]:
    if "application/json" not in (request.content_type or ""):
        return {}
    if not request.body:
        return {}
    payload = request.body.decode("utf-8").strip()
    if not payload:
        return {}
    return json.loads(payload)


def _get_user_from_body(body: dict[str, Any]):
    user_id = body.get("userId")
    if user_id is None:
        raise ValueError("userId is required")
    return get_object_or_404(get_user_model(), pk=int(user_id))


def _enforce_assignment_write_enabled() -> JsonResponse | None:
    if settings.CUTOVER_READ_ONLY_ASSIGNMENT:
        return JsonResponse(
            {
                "error": "Task assignment is currently in read-only mode",
                "cutoverMode": settings.CUTOVER_MODE,
            },
            status=409,
        )
    return None


def _enforce_pilot_user_allowed(user) -> JsonResponse | None:
    if settings.CUTOVER_MODE != "assisted":
        return None
    if not settings.CUTOVER_PILOT_USER_IDS:
        return None
    if int(user.id) in settings.CUTOVER_PILOT_USER_IDS:
        return None
    return JsonResponse(
        {
            "error": "User is not enabled for the assisted-mode pilot",
            "cutoverMode": settings.CUTOVER_MODE,
            "pilotUserIds": settings.CUTOVER_PILOT_USER_IDS,
        },
        status=403,
    )


def _serialize_work_item(work_item: WorkItem) -> dict[str, Any]:
    return {
        "id": work_item.id,
        "projectId": work_item.project_id,
        "planJobId": work_item.plan_job_id,
        "kind": work_item.kind,
        "status": work_item.status,
        "assignedToUserId": work_item.assigned_to_id,
        "lockedUntil": work_item.locked_until.isoformat().replace("+00:00", "Z") if work_item.locked_until else None,
        "attempt": work_item.attempt,
        "priorityScore": work_item.priority_score,
        "payload": work_item.payload,
        "verifiedAt": work_item.verified_at.isoformat().replace("+00:00", "Z") if work_item.verified_at else None,
        "updatedAt": work_item.updated_at.isoformat().replace("+00:00", "Z"),
        "version": work_item.version,
    }


def _serialize_project_summary(project: Project) -> dict[str, Any]:
    progress = workforce_service.get_project_progress(project=project)
    freshness = workforce_service.get_project_jobs_freshness(project=project)
    return {
        "id": project.id,
        "name": project.name,
        "priority": project.priority,
        "status": project.status,
        "dueAt": project.due_at.isoformat().replace("+00:00", "Z") if project.due_at else None,
        "planSummary": {
            "jobCount": project.plan_jobs.count(),
            "materialCount": project.plan_materials.count(),
        },
        "progress": {
            "total": progress.total,
            "ready": progress.ready,
            "assigned": progress.assigned,
            "tempDone": progress.temp_done,
            "verified": progress.verified,
            "failed": progress.failed,
            "cancelled": progress.cancelled,
        },
        "jobsFreshness": {
            "corporationId": freshness.corporation_id,
            "lastSuccessAt": freshness.last_success_at,
            "ageSeconds": freshness.age_seconds,
            "isStale": freshness.is_stale,
        },
        "staleWarning": freshness.is_stale,
    }


def _serialize_project_target(target) -> dict[str, Any]:
    return {
        "id": target.id,
        "typeId": target.type_id,
        "quantity": target.quantity,
        "isFinalOutput": target.is_final_output,
    }


def _serialize_plan_job_context(work_item: WorkItem) -> dict[str, Any] | None:
    plan_job = work_item.plan_job
    if plan_job is None:
        return None
    return {
        "id": plan_job.id,
        "activityId": plan_job.activity_id,
        "blueprintTypeId": plan_job.blueprint_type_id,
        "productTypeId": plan_job.product_type_id,
        "runs": plan_job.runs,
        "expectedDurationS": plan_job.expected_duration_s,
        "level": plan_job.level,
        "isAdvanced": plan_job.is_advanced,
    }


def _serialize_plan_job_materials(work_item: WorkItem) -> list[dict[str, Any]]:
    plan_job = work_item.plan_job
    if plan_job is None:
        return []
    return [
        {
            "id": material.id,
            "materialTypeId": material.material_type_id,
            "quantityTotal": material.quantity_total,
            "activityId": material.activity_id,
            "level": material.level,
            "isInput": material.is_input,
            "isIntermediate": material.is_intermediate,
        }
        for material in plan_job.materials.order_by("id")
    ]


def _serialize_work_item_events(work_item: WorkItem, *, limit: int = 10) -> list[dict[str, Any]]:
    return [
        _serialize_work_event(event)
        for event in work_item.events.order_by("-created_at", "-id")[:limit]
    ]


def _summarize_work_event(event_type: str, details: dict[str, Any]) -> str:
    if event_type in {"DIRECTOR_REQUEUED", "REQUEUED", "DIRECTOR_RELEASED", "RELEASED"}:
        reason = details.get("reason")
        source = details.get("source")
        previous_status = details.get("previousStatus")
        parts = [
            part
            for part in [
                f"reason={reason}" if reason else None,
                f"source={source}" if source else None,
                f"from={previous_status}" if previous_status else None,
            ]
            if part
        ]
        return ", ".join(parts) or "State returned to ready"
    if event_type == "VERIFIED_OK":
        matched_job_id = details.get("matchedJobId")
        source = details.get("source")
        parts = [part for part in [f"matchedJobId={matched_job_id}" if matched_job_id is not None else None, f"source={source}" if source else None] if part]
        return ", ".join(parts) or "Verification matched"
    if event_type == "VERIFY_MISS":
        reason = details.get("reason")
        failed_at = details.get("failedAt")
        source = details.get("source")
        parts = [part for part in [f"reason={reason}" if reason else None, f"failedAt={failed_at}" if failed_at else None, f"source={source}" if source else None] if part]
        return ", ".join(parts) or "Verification missed"
    if event_type == "TEMP_DONE":
        temp_done_key = details.get("idempotencyKey")
        return f"idempotencyKey={temp_done_key}" if temp_done_key else "Marked temp-done"
    if event_type == "CLAIMED":
        lock_until = details.get("lockUntil")
        return f"lockUntil={lock_until}" if lock_until else "Claimed"
    if event_type == "ESCALATED":
        reason = details.get("reason")
        attempt = details.get("attempt")
        source = details.get("source")
        parts = [part for part in [f"reason={reason}" if reason else None, f"attempt={attempt}" if attempt is not None else None, f"source={source}" if source else None] if part]
        return ", ".join(parts) or "Escalated"
    return ""


def _classify_work_event(event_type: str) -> dict[str, str]:
    if event_type == "VERIFIED_OK":
        return {"outcomeLabel": "matched", "outcomeTone": "good"}
    if event_type in {"DIRECTOR_RELEASED", "RELEASED"}:
        return {"outcomeLabel": "released", "outcomeTone": "good"}
    if event_type in {"DIRECTOR_REQUEUED", "REQUEUED"}:
        return {"outcomeLabel": "requeued", "outcomeTone": "warn"}
    if event_type == "VERIFY_MISS":
        return {"outcomeLabel": "verify miss", "outcomeTone": "bad"}
    if event_type == "ESCALATED":
        return {"outcomeLabel": "escalated", "outcomeTone": "bad"}
    if event_type == "TEMP_DONE":
        return {"outcomeLabel": "temp-done", "outcomeTone": "warn"}
    if event_type == "CLAIMED":
        return {"outcomeLabel": "claimed", "outcomeTone": "warn"}
    return {"outcomeLabel": event_type.lower().replace("_", " "), "outcomeTone": ""}


def _serialize_work_event(event: WorkEvent | None) -> dict[str, Any] | None:
    if event is None:
        return None
    details = event.details or {}
    return {
        "id": event.id,
        "eventType": event.event_type,
        "actorUserId": event.actor_id,
        "details": details,
        "summary": _summarize_work_event(event.event_type, details),
        "source": details.get("source"),
        **_classify_work_event(event.event_type),
        "createdAt": event.created_at.isoformat().replace("+00:00", "Z"),
    }


def _classify_work_item_risk(
    work_item: WorkItem,
    *,
    latest_event: WorkEvent | None,
    latest_cleanup_event: WorkEvent | None,
) -> dict[str, Any]:
    latest_summary = _summarize_work_event(latest_event.event_type, latest_event.details or {}) if latest_event is not None else ""
    latest_cleanup_summary = (
        _summarize_work_event(latest_cleanup_event.event_type, latest_cleanup_event.details or {})
        if latest_cleanup_event is not None
        else ""
    )
    latest_type = latest_event.event_type if latest_event is not None else None
    latest_cleanup_type = latest_cleanup_event.event_type if latest_cleanup_event is not None else None

    if latest_type == "ESCALATED" or latest_cleanup_type == "ESCALATED":
        return {
            "code": "escalated",
            "label": "escalated",
            "tone": "bad",
            "rank": 0,
            "reason": latest_summary if latest_type == "ESCALATED" else latest_cleanup_summary,
            "nextAction": "manual_review",
        }
    if latest_type == "VERIFY_MISS" or latest_cleanup_type == "VERIFY_MISS":
        return {
            "code": "verification_miss",
            "label": "verify miss",
            "tone": "bad",
            "rank": 1,
            "reason": latest_summary if latest_type == "VERIFY_MISS" else latest_cleanup_summary,
            "nextAction": "retry_verify",
        }
    if work_item.status == "failed":
        return {
            "code": "manual_cleanup",
            "label": "failed",
            "tone": "bad",
            "rank": 2,
            "reason": latest_cleanup_summary or latest_summary or "Awaiting manual cleanup",
            "nextAction": "requeue",
        }
    if latest_cleanup_type in ["DIRECTOR_REQUEUED", "REQUEUED"]:
        return {
            "code": "requeued",
            "label": "requeued",
            "tone": "warn",
            "rank": 3,
            "reason": latest_cleanup_summary or "Returned to ready",
            "nextAction": "monitor",
        }
    if work_item.status == "temp_done":
        return {
            "code": "awaiting_verification",
            "label": "temp-done",
            "tone": "warn",
            "rank": 4,
            "reason": latest_summary or "Awaiting verification",
            "nextAction": "wait_for_verify",
        }
    if work_item.status == "assigned":
        return {
            "code": "assigned_aging",
            "label": "assigned aging",
            "tone": "warn",
            "rank": 5,
            "reason": latest_summary or "Assignment still active",
            "nextAction": "release_or_monitor",
        }
    return {
        "code": work_item.status,
        "label": work_item.status.replace("_", " "),
        "tone": "",
        "rank": 6,
        "reason": latest_summary or "",
        "nextAction": "inspect",
    }


def _build_manual_attention_summary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = timezone.now()
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        risk = item["risk"]
        updated_at = timezone.datetime.fromisoformat(item["updatedAt"].replace("Z", "+00:00"))
        age_seconds = max(int((now - updated_at).total_seconds()), 0)
        entry = grouped.setdefault(
            risk["code"],
            {
                "code": risk["code"],
                "label": risk["label"],
                "count": 0,
                "nextAction": risk["nextAction"],
                "tone": risk["tone"],
                "firstWorkItemId": None,
                "firstActionableWorkItemId": None,
                "oldestAgeSeconds": 0,
            },
        )
        entry["count"] += 1
        entry["oldestAgeSeconds"] = max(entry["oldestAgeSeconds"], age_seconds)
        if entry["firstWorkItemId"] is None:
            entry["firstWorkItemId"] = item["id"]
        if entry["firstActionableWorkItemId"] is None and risk["nextAction"] in {"retry_verify", "requeue", "release_or_monitor"}:
            entry["firstActionableWorkItemId"] = item["id"]
    return sorted(grouped.values(), key=lambda entry: (-entry["count"], entry["label"]))


def _serialize_project_events(project: Project, *, limit: int = 10) -> list[dict[str, Any]]:
    return [
        {
            **(_serialize_work_event(event) or {}),
            "workItemId": event.work_item_id,
        }
        for event in WorkEvent.objects.filter(work_item__project=project).order_by("-created_at", "-id")[:limit]
    ]


def _build_recent_event_source_summary(events: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "total": len(events),
        "recommended": 0,
        "manual": 0,
        "system": 0,
    }
    for event in events:
        source = event.get("source")
        if source == "recommended_action":
            summary["recommended"] += 1
        elif source == "manual_action":
            summary["manual"] += 1
        else:
            summary["system"] += 1
    return summary


def _serialize_work_item_for_director(work_item: WorkItem) -> dict[str, Any]:
    latest_event = work_item.events.order_by("-created_at", "-id").first()
    latest_cleanup_event = (
        work_item.events.filter(
            event_type__in=["DIRECTOR_REQUEUED", "DIRECTOR_RELEASED", "VERIFIED_OK", "REQUEUED", "VERIFY_MISS"]
        )
        .order_by("-created_at", "-id")
        .first()
    )
    return {
        **_serialize_work_item(work_item),
        "assignee": (
            {
                "userId": work_item.assigned_to_id,
                "username": work_item.assigned_to.get_username(),
            }
            if work_item.assigned_to_id
            else None
        ),
        "planJob": _serialize_plan_job_context(work_item),
        "latestEvent": _serialize_work_event(latest_event),
        "latestCleanupEvent": _serialize_work_event(latest_cleanup_event),
        "risk": _classify_work_item_risk(
            work_item,
            latest_event=latest_event,
            latest_cleanup_event=latest_cleanup_event,
        ),
    }


def _build_project_status_breakdown(project: Project) -> dict[str, Any]:
    progress = workforce_service.get_project_progress(project=project)
    return {
        "total": progress.total,
        "ready": progress.ready,
        "assigned": progress.assigned,
        "tempDone": progress.temp_done,
        "verified": progress.verified,
        "failed": progress.failed,
        "cancelled": progress.cancelled,
    }


def _build_project_activity_breakdown(project: Project) -> list[dict[str, Any]]:
    items = list(project.work_items.select_related("plan_job").order_by("plan_job__activity_id", "plan_job__level", "id"))
    grouped: dict[tuple[int | None, int | None], dict[str, Any]] = {}
    for work_item in items:
        plan_job = work_item.plan_job
        key = (
            int(plan_job.activity_id) if plan_job else None,
            int(plan_job.level) if plan_job else None,
        )
        entry = grouped.setdefault(
            key,
            {
                "activityId": key[0],
                "level": key[1],
                "total": 0,
                "ready": 0,
                "assigned": 0,
                "tempDone": 0,
                "verified": 0,
                "failed": 0,
                "cancelled": 0,
            },
        )
        entry["total"] += 1
        status_key = "tempDone" if work_item.status == "temp_done" else work_item.status
        entry[status_key] += 1
    return list(grouped.values())


def _build_active_worker_summary(project: Project) -> list[dict[str, Any]]:
    items = list(
        project.work_items.select_related("assigned_to", "plan_job")
        .filter(status__in=["assigned", "temp_done"], assigned_to__isnull=False)
        .order_by("assigned_to_id", "-updated_at", "-id")
    )
    grouped: dict[int, dict[str, Any]] = {}
    for work_item in items:
        user = work_item.assigned_to
        if user is None:
            continue
        entry = grouped.setdefault(
            user.id,
            {
                "userId": user.id,
                "username": user.get_username(),
                "assignedCount": 0,
                "tempDoneCount": 0,
                "workItems": [],
            },
        )
        if work_item.status == "assigned":
            entry["assignedCount"] += 1
        elif work_item.status == "temp_done":
            entry["tempDoneCount"] += 1
        entry["workItems"].append(_serialize_work_item_for_director(work_item))
    return list(grouped.values())


def _build_bottleneck_summary(project: Project) -> dict[str, Any]:
    failed_items = list(
        project.work_items.select_related("assigned_to", "plan_job").filter(status="failed").order_by("-updated_at", "-id")[:25]
    )
    temp_done_items = list(
        project.work_items.select_related("assigned_to", "plan_job").filter(status="temp_done").order_by("updated_at", "id")[:25]
    )
    assigned_items = list(
        project.work_items.select_related("assigned_to", "plan_job").filter(status="assigned").order_by("updated_at", "id")[:25]
    )

    def _sort_items(items: list[WorkItem]) -> list[dict[str, Any]]:
        ranked_items: list[tuple[dict[str, Any], int, int]] = []
        for index, work_item in enumerate(items):
            latest_event = work_item.events.order_by("-created_at", "-id").first()
            latest_cleanup_event = (
                work_item.events.filter(
                    event_type__in=["DIRECTOR_REQUEUED", "DIRECTOR_RELEASED", "VERIFIED_OK", "REQUEUED", "VERIFY_MISS"]
                )
                .order_by("-created_at", "-id")
                .first()
            )
            ranked_items.append(
                (
                    _serialize_work_item_for_director(work_item),
                    _classify_work_item_risk(
                        work_item,
                        latest_event=latest_event,
                        latest_cleanup_event=latest_cleanup_event,
                    )["rank"],
                    index,
                )
            )

        ranked_items.sort(key=lambda item: (item[1], item[2]))
        return [item for item, _rank, _index in ranked_items[:10]]

    failed_payload = _sort_items(failed_items)
    temp_done_payload = _sort_items(temp_done_items)
    assigned_payload = _sort_items(assigned_items)
    manual_attention = sorted(
        [
            *[item for item in failed_payload if item["risk"]["tone"] == "bad"],
            *[item for item in temp_done_payload if item["risk"]["tone"] == "bad"],
            *[item for item in assigned_payload if item["risk"]["tone"] == "bad"],
        ],
        key=lambda item: (item["risk"]["rank"], item["id"]),
    )
    manual_attention_summary = _build_manual_attention_summary(manual_attention)

    return {
        "failed": failed_payload,
        "tempDone": temp_done_payload,
        "assigned": assigned_payload,
        "manualAttention": manual_attention[:10],
        "manualAttentionSummary": manual_attention_summary,
    }


def _build_work_item_instructions(work_item: WorkItem) -> dict[str, Any]:
    plan_job = work_item.plan_job
    if plan_job is None:
        return {
            "title": "Work item",
            "summary": "Open this work item and follow the assigned payload.",
            "steps": [],
        }

    return {
        "title": f"Start industry job for blueprint {plan_job.blueprint_type_id}",
        "summary": (
            f"Start {plan_job.runs} run(s) for blueprint {plan_job.blueprint_type_id} "
            f"to produce product {plan_job.product_type_id}."
        ),
        "steps": [
            f"Confirm fresh jobs sync before starting if stale warning is shown.",
            f"Start activity {plan_job.activity_id} with blueprint type {plan_job.blueprint_type_id}.",
            f"Use {plan_job.runs} run(s) and verify output product type {plan_job.product_type_id}.",
            f"Mark temp-done after the job is submitted so verifier can match ESI evidence.",
        ],
    }


@require_POST
def claim_work_item(request: HttpRequest) -> JsonResponse:
    blocked = _enforce_assignment_write_enabled()
    if blocked is not None:
        return blocked
    body = _parse_json_body(request)
    try:
        user = _get_user_from_body(body)
        blocked = _enforce_pilot_user_allowed(user)
        if blocked is not None:
            return blocked
        work_item = workforce_service.claim_next(user=user)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except NoAvailableWorkItem as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    return JsonResponse({"workItem": _serialize_work_item(work_item)})


@require_POST
def temp_done_work_item(request: HttpRequest, work_item_id: int) -> JsonResponse:
    blocked = _enforce_assignment_write_enabled()
    if blocked is not None:
        return blocked
    body = _parse_json_body(request)
    try:
        user = _get_user_from_body(body)
        blocked = _enforce_pilot_user_allowed(user)
        if blocked is not None:
            return blocked
        idempotency_key = body.get("idempotencyKey")
        if not idempotency_key:
            raise ValueError("idempotencyKey is required")
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    work_item = get_object_or_404(WorkItem, pk=work_item_id)
    updated = workforce_service.mark_temp_done(work_item=work_item, actor=user, idempotency_key=idempotency_key)
    return JsonResponse({"workItem": _serialize_work_item(updated)})


@require_POST
def release_work_item(request: HttpRequest, work_item_id: int) -> JsonResponse:
    blocked = _enforce_assignment_write_enabled()
    if blocked is not None:
        return blocked
    body = _parse_json_body(request)
    try:
        user = _get_user_from_body(body)
        blocked = _enforce_pilot_user_allowed(user)
        if blocked is not None:
            return blocked
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    work_item = get_object_or_404(WorkItem, pk=work_item_id)
    updated = workforce_service.release(work_item=work_item, actor=user, reason=body.get("reason") or "manual_release")
    return JsonResponse({"workItem": _serialize_work_item(updated)})


@require_POST
def director_requeue_work_item(request: HttpRequest, work_item_id: int) -> JsonResponse:
    blocked = _enforce_assignment_write_enabled()
    if blocked is not None:
        return blocked
    body = _parse_json_body(request)
    work_item = get_object_or_404(WorkItem, pk=work_item_id)
    try:
        updated = workforce_service.director_requeue(
            work_item=work_item,
            reason=body.get("reason") or "director_requeue",
            source=body.get("source"),
        )
    except InvalidWorkItemTransition as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    return JsonResponse({"workItem": _serialize_work_item(updated)})


@require_POST
def director_release_work_item(request: HttpRequest, work_item_id: int) -> JsonResponse:
    blocked = _enforce_assignment_write_enabled()
    if blocked is not None:
        return blocked
    body = _parse_json_body(request)
    work_item = get_object_or_404(WorkItem, pk=work_item_id)
    try:
        updated = workforce_service.director_release(
            work_item=work_item,
            reason=body.get("reason") or "director_release",
            source=body.get("source"),
        )
    except InvalidWorkItemTransition as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    return JsonResponse({"workItem": _serialize_work_item(updated)})


@require_POST
def director_verify_work_item(request: HttpRequest, work_item_id: int) -> JsonResponse:
    body = _parse_json_body(request)
    work_item = get_object_or_404(WorkItem, pk=work_item_id)
    try:
        verified = workforce_service.verify_start_job(work_item=work_item, source=body.get("source"))
    except (InvalidWorkItemTransition, VerificationWindowOpen, StaleVerificationData) as exc:
        return JsonResponse({"error": str(exc)}, status=409)

    updated = WorkItem.objects.get(pk=work_item.pk)
    return JsonResponse(
        {
            "workItem": _serialize_work_item(updated),
            "verified": bool(verified),
        }
    )


@require_GET
def my_active(request: HttpRequest) -> JsonResponse:
    user_id = request.GET.get("userId")
    if user_id is None:
        return JsonResponse({"error": "userId is required"}, status=400)
    user = get_object_or_404(get_user_model(), pk=int(user_id))
    blocked = _enforce_pilot_user_allowed(user)
    if blocked is not None:
        return blocked
    work_item = workforce_service.get_my_active(user=user)
    return JsonResponse({"workItem": _serialize_work_item(work_item) if work_item else None})


@require_GET
def my_active_detail(request: HttpRequest) -> JsonResponse:
    user_id = request.GET.get("userId")
    if user_id is None:
        return JsonResponse({"error": "userId is required"}, status=400)
    user = get_object_or_404(get_user_model(), pk=int(user_id))
    blocked = _enforce_pilot_user_allowed(user)
    if blocked is not None:
        return blocked
    work_item = workforce_service.get_my_active(user=user)
    if work_item is None:
        return JsonResponse({"workItem": None})

    project = work_item.project
    freshness = workforce_service.get_project_jobs_freshness(project=project)
    return JsonResponse(
        {
            "workItem": _serialize_work_item(work_item),
            "project": {
                "id": project.id,
                "name": project.name,
                "priority": project.priority,
                "status": project.status,
            },
            "planJob": _serialize_plan_job_context(work_item),
            "planMaterials": _serialize_plan_job_materials(work_item),
            "instructions": _build_work_item_instructions(work_item),
            "recentEvents": _serialize_work_item_events(work_item),
            "jobsFreshness": {
                "corporationId": freshness.corporation_id,
                "lastSuccessAt": freshness.last_success_at,
                "ageSeconds": freshness.age_seconds,
                "isStale": freshness.is_stale,
            },
            "staleWarning": freshness.is_stale,
        }
    )


@require_GET
def queue(request: HttpRequest) -> JsonResponse:
    limit = int(request.GET.get("limit") or 20)
    work_items = workforce_service.get_queue(limit=limit)
    return JsonResponse({"workItems": [_serialize_work_item(work_item) for work_item in work_items]})


@require_POST
def dispatch_project(request: HttpRequest, project_id: int) -> JsonResponse:
    blocked = _enforce_assignment_write_enabled()
    if blocked is not None:
        return blocked
    project = get_object_or_404(Project, pk=project_id)
    dispatched = workforce_service.dispatch_project(project)
    return JsonResponse({"workItems": [_serialize_work_item(work_item) for work_item in dispatched]})


@require_GET
def project_progress(_request: HttpRequest, project_id: int) -> JsonResponse:
    project = get_object_or_404(Project, pk=project_id)
    progress = workforce_service.get_project_progress(project=project)
    freshness = workforce_service.get_project_jobs_freshness(project=project)
    return JsonResponse(
        {
            "projectId": progress.project_id,
            "total": progress.total,
            "ready": progress.ready,
            "assigned": progress.assigned,
            "tempDone": progress.temp_done,
            "verified": progress.verified,
            "failed": progress.failed,
            "cancelled": progress.cancelled,
            "jobsFreshness": {
                "corporationId": freshness.corporation_id,
                "lastSuccessAt": freshness.last_success_at,
                "ageSeconds": freshness.age_seconds,
                "isStale": freshness.is_stale,
            },
            "staleWarning": freshness.is_stale,
        }
    )


@require_POST
def verify_batch(_request: HttpRequest) -> JsonResponse:
    result = workforce_service.verify_batch()
    return JsonResponse(result)


@require_GET
def director_dashboard(_request: HttpRequest) -> JsonResponse:
    projects = list(Project.objects.order_by("-priority", "name"))
    ready_queue = workforce_service.get_queue(limit=10)
    temp_done_items = list(WorkItem.objects.filter(status="temp_done").order_by("created_at", "id")[:10])
    failed_items = list(WorkItem.objects.filter(status="failed").order_by("-updated_at", "-id")[:10])
    assigned_count = WorkItem.objects.filter(status="assigned").count()
    temp_done_count = WorkItem.objects.filter(status="temp_done").count()
    failed_count = WorkItem.objects.filter(status="failed").count()
    stale_project_count = 0
    serialized_projects: list[dict[str, Any]] = []
    for project in projects:
        summary = _serialize_project_summary(project)
        if summary["staleWarning"]:
            stale_project_count += 1
        serialized_projects.append(summary)

    now = timezone.now()
    oldest_temp_done = temp_done_items[0] if temp_done_items else None
    return JsonResponse(
        {
            "projects": serialized_projects,
            "queueSummary": {
                "readyCount": WorkItem.objects.filter(status="ready").count(),
                "assignedCount": assigned_count,
                "tempDoneCount": temp_done_count,
                "failedCount": failed_count,
                "topReady": [_serialize_work_item(work_item) for work_item in ready_queue],
            },
            "tempDoneSummary": {
                "count": temp_done_count,
                "oldestAgeSeconds": (
                    max(int((now - oldest_temp_done.updated_at).total_seconds()), 0) if oldest_temp_done else None
                ),
                "items": [_serialize_work_item(work_item) for work_item in temp_done_items],
            },
            "failedSummary": {
                "count": failed_count,
                "items": [_serialize_work_item(work_item) for work_item in failed_items],
            },
            "staleProjectCount": stale_project_count,
        }
    )


@require_GET
def director_project_detail(_request: HttpRequest, project_id: int) -> JsonResponse:
    project = get_object_or_404(Project, pk=project_id)
    freshness = workforce_service.get_project_jobs_freshness(project=project)
    recent_events = _serialize_project_events(project)
    return JsonResponse(
        {
            "project": {
                **_serialize_project_summary(project),
                "notes": project.notes,
            },
            "targets": [_serialize_project_target(target) for target in project.targets.order_by("id")],
            "queueBreakdown": {
                "byStatus": _build_project_status_breakdown(project),
                "byActivity": _build_project_activity_breakdown(project),
            },
            "activeWorkers": _build_active_worker_summary(project),
            "bottlenecks": _build_bottleneck_summary(project),
            "recentEvents": recent_events,
            "recentEventSources": _build_recent_event_source_summary(recent_events),
            "jobsFreshness": {
                "corporationId": freshness.corporation_id,
                "lastSuccessAt": freshness.last_success_at,
                "ageSeconds": freshness.age_seconds,
                "isStale": freshness.is_stale,
            },
            "staleWarning": freshness.is_stale,
        }
    )