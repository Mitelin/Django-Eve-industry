from __future__ import annotations

import json
from typing import Any

from django.contrib.auth import get_user_model
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_POST

from apps.industry_planner.models import Project, ProjectTarget
from apps.industry_planner.shadow import generate_shadow_planner_report
from apps.industry_planner.services import IndustryPlannerService, resolve_material_multipliers


planner_service = IndustryPlannerService()


def _parse_json_body(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    return json.loads(request.body.decode("utf-8"))


def _build_efficiency(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "shipT1ME": body.get("shipT1ME") or body.get("typeme") or 0,
        "shipT1TE": body.get("shipT1TE") or body.get("typete") or 0,
        "shipT2ME": body.get("shipT2ME") or body.get("typeme") or 0,
        "shipT2TE": body.get("shipT2TE") or body.get("typete") or 0,
        "moduleT1ME": body.get("moduleT1ME") or body.get("moduleme") or 0,
        "moduleT1TE": body.get("moduleT1TE") or body.get("modulete") or 0,
        "moduleT2ME": body.get("moduleT2ME") or body.get("moduleme") or 0,
        "moduleT2TE": body.get("moduleT2TE") or body.get("modulete") or 0,
    }


def _build_flags(body: dict[str, Any]) -> tuple[bool, bool, bool, bool]:
    build_t1 = body.get("buildT1")
    if build_t1 is None:
        build_t1 = True

    copy_bpo = body.get("copyBPO")
    if copy_bpo is None:
        copy_bpo = True

    produce_fuel_blocks = body.get("produceFuelBlocks")
    if produce_fuel_blocks is None:
        produce_fuel_blocks = True

    merge_modules = body.get("mergeModules")
    if merge_modules is None:
        merge_modules = False

    return bool(build_t1), bool(copy_bpo), bool(produce_fuel_blocks), bool(merge_modules)


def _build_bonus_tuple(body: dict[str, Any]) -> tuple[float, float, float]:
    return resolve_material_multipliers(
        body.get("industryStructureType"),
        body.get("industryRig"),
        body.get("reactionRig"),
        manufacturing_role_bonus=body.get("manufacturingRoleBonus"),
        manufacturing_rig_bonus=body.get("manufacturingRigBonus"),
        reaction_rig_bonus=body.get("reactionRigBonus"),
    )


def _serialize_project(project: Project, *, include_plan_rows: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": project.id,
        "name": project.name,
        "priority": project.priority,
        "status": project.status,
        "createdByUserId": project.created_by_id,
        "dueAt": project.due_at.isoformat().replace("+00:00", "Z") if project.due_at else None,
        "notes": project.notes,
        "targets": [
            {
                "id": target.id,
                "typeId": target.type_id,
                "quantity": target.quantity,
                "isFinalOutput": target.is_final_output,
            }
            for target in project.targets.order_by("id")
        ],
        "planSummary": {
            "jobCount": project.plan_jobs.count(),
            "materialCount": project.plan_materials.count(),
        },
    }
    if not include_plan_rows:
        return data

    data["jobs"] = [
        {
            "id": plan_job.id,
            "activityId": plan_job.activity_id,
            "blueprintTypeId": plan_job.blueprint_type_id,
            "productTypeId": plan_job.product_type_id,
            "runs": plan_job.runs,
            "expectedDurationS": plan_job.expected_duration_s,
            "level": plan_job.level,
            "probability": plan_job.probability,
            "isAdvanced": plan_job.is_advanced,
            "paramsHash": plan_job.params_hash,
            "materials": [
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
            ],
        }
        for plan_job in project.plan_jobs.order_by("id")
    ]
    data["materials"] = [
        {
            "id": material.id,
            "materialTypeId": material.material_type_id,
            "quantityTotal": material.quantity_total,
            "activityId": material.activity_id,
            "level": material.level,
            "isInput": material.is_input,
            "isIntermediate": material.is_intermediate,
            "planJobId": material.plan_job_id,
        }
        for material in project.plan_materials.filter(plan_job__isnull=True).order_by("id")
    ]
    return data


def _replace_project_targets(project: Project, targets: list[dict[str, Any]]) -> None:
    project.targets.all().delete()
    ProjectTarget.objects.bulk_create(
        [
            ProjectTarget(
                project=project,
                type_id=int(target["typeId"]),
                quantity=int(target.get("quantity") or 0),
                is_final_output=bool(target.get("isFinalOutput", True)),
            )
            for target in targets
        ]
    )


def _apply_project_updates(project: Project, body: dict[str, Any]) -> Project:
    if body.get("name") is not None:
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValueError("name cannot be blank")
        project.name = name
    if body.get("priority") is not None:
        project.priority = int(body.get("priority") or 0)
    if body.get("status") is not None:
        project.status = str(body.get("status") or project.status)
    if body.get("notes") is not None:
        project.notes = str(body.get("notes") or "")
    if body.get("dueAt") is not None:
        project.due_at = parse_datetime(body.get("dueAt")) if body.get("dueAt") else None

    project.save(update_fields=["name", "priority", "status", "notes", "due_at", "updated_at"])
    if body.get("targets") is not None:
        _replace_project_targets(project, body.get("targets") or [])
    return project


@require_POST
def calculate_blueprints(request: HttpRequest) -> HttpResponse:
    body = _parse_json_body(request)
    efficiency = _build_efficiency(body)
    build_t1, copy_bpo, produce_fuel_blocks, merge_modules = _build_flags(body)
    manufacturing_role_bonus, manufacturing_rig_bonus, reaction_rig_bonus = _build_bonus_tuple(body)

    try:
        details = planner_service.get_blueprints_details(
            types=body.get("types") or [],
            efficiency=efficiency,
            build_t1=build_t1,
            copy_bpo=copy_bpo,
            produce_fuel_blocks=produce_fuel_blocks,
            merge_modules=merge_modules,
            manufacturing_role_bonus=manufacturing_role_bonus,
            manufacturing_rig_bonus=manufacturing_rig_bonus,
            reaction_rig_bonus=reaction_rig_bonus,
        )
        return JsonResponse(details)
    except Exception as exc:
        return HttpResponse(f"Chyba: {exc}", content_type="text/plain")


@require_POST
def calculate_blueprint_by_id(request: HttpRequest, type_id: int) -> HttpResponse:
    body = _parse_json_body(request)
    amount = body.get("amount")
    if amount is None:
        return HttpResponse("Chyba: amount missing", content_type="text/plain")

    efficiency = _build_efficiency(body)
    build_t1, copy_bpo, produce_fuel_blocks, merge_modules = _build_flags(body)
    manufacturing_role_bonus, manufacturing_rig_bonus, reaction_rig_bonus = _build_bonus_tuple(body)

    try:
        details = planner_service.get_blueprint_details(
            type_id=int(type_id),
            amount=int(amount),
            efficiency=efficiency,
            build_t1=build_t1,
            copy_bpo=copy_bpo,
            produce_fuel_blocks=produce_fuel_blocks,
            merge_modules=merge_modules,
            manufacturing_role_bonus=manufacturing_role_bonus,
            manufacturing_rig_bonus=manufacturing_rig_bonus,
            reaction_rig_bonus=reaction_rig_bonus,
        )
        return JsonResponse(details)
    except Exception as exc:
        return HttpResponse(f"Chyba: {exc}", content_type="text/plain")


@require_POST
def ore_material(request: HttpRequest) -> HttpResponse:
    body = _parse_json_body(request)
    try:
        material = planner_service.get_ore_details(body.get("typeName"))
        return JsonResponse(material, safe=False)
    except Exception as exc:
        return HttpResponse(f"Chyba: {exc}", content_type="text/plain")


@require_GET
def list_projects(_request: HttpRequest) -> JsonResponse:
    projects = Project.objects.order_by("-priority", "name")
    return JsonResponse({"projects": [_serialize_project(project, include_plan_rows=False) for project in projects]})


@require_GET
def shadow_planner_report(_request: HttpRequest) -> JsonResponse:
    return JsonResponse(generate_shadow_planner_report())


@require_POST
def create_project(request: HttpRequest) -> JsonResponse:
    body = _parse_json_body(request)
    name = (body.get("name") or "").strip()
    created_by_user_id = body.get("createdByUserId")

    if not name:
        return JsonResponse({"error": "name is required"}, status=400)
    if created_by_user_id is None:
        return JsonResponse({"error": "createdByUserId is required"}, status=400)

    user = get_object_or_404(get_user_model(), pk=int(created_by_user_id))
    due_at_raw = body.get("dueAt")
    due_at = parse_datetime(due_at_raw) if due_at_raw else None

    project = Project.objects.create(
        name=name,
        priority=int(body.get("priority") or 3),
        status=body.get("status") or "draft",
        created_by=user,
        due_at=due_at,
        notes=body.get("notes") or "",
    )
    _replace_project_targets(project, body.get("targets") or [])
    return JsonResponse(_serialize_project(project), status=201)


@require_GET
def get_project(_request: HttpRequest, project_id: int) -> JsonResponse:
    project = get_object_or_404(Project, pk=project_id)
    return JsonResponse(_serialize_project(project))


@require_POST
def update_project(request: HttpRequest, project_id: int) -> JsonResponse:
    project = get_object_or_404(Project, pk=project_id)
    body = _parse_json_body(request)
    try:
        project = _apply_project_updates(project, body)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    project.refresh_from_db()
    return JsonResponse(_serialize_project(project))


@require_POST
def rebuild_project(request: HttpRequest, project_id: int) -> JsonResponse:
    project = get_object_or_404(Project, pk=project_id)
    body = _parse_json_body(request)

    if body.get("targets") is not None:
        _replace_project_targets(project, body.get("targets") or [])

    types = body.get("types")
    if types is None:
        types = [
            {"typeId": target.type_id, "amount": target.quantity}
            for target in project.targets.filter(is_final_output=True).order_by("id")
        ]

    efficiency = _build_efficiency(body)
    build_t1, copy_bpo, produce_fuel_blocks, merge_modules = _build_flags(body)
    manufacturing_role_bonus, manufacturing_rig_bonus, reaction_rig_bonus = _build_bonus_tuple(body)

    details = planner_service.rebuild_project_plan(
        project=project,
        types=types,
        efficiency=efficiency,
        build_t1=build_t1,
        copy_bpo=copy_bpo,
        produce_fuel_blocks=produce_fuel_blocks,
        merge_modules=merge_modules,
        manufacturing_role_bonus=manufacturing_role_bonus,
        manufacturing_rig_bonus=manufacturing_rig_bonus,
        reaction_rig_bonus=reaction_rig_bonus,
    )

    project.refresh_from_db()
    return JsonResponse({"project": _serialize_project(project), "plannerResult": details})