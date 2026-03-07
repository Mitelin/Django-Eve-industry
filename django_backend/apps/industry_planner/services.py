from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Protocol

from django.db import connections
from django.db import transaction

from apps.industry_planner.models import PlanJob, PlanMaterial, Project


class PlannerRepository(Protocol):
    def get_blueprint_products(self, type_id: int) -> list[dict[str, Any]]: ...

    def get_blueprint_source(self, type_id: int) -> list[dict[str, Any]]: ...

    def get_blueprint_material(self, type_id: int) -> list[dict[str, Any]]: ...

    def get_ore_minerals(self, type_name: str) -> list[dict[str, Any]]: ...


class DjangoSdeRepository:
    def __init__(self, *, connection_alias: str = "default") -> None:
        self.connection_alias = connection_alias

    def _fetch_all(self, query: str, params: list[Any]) -> list[dict[str, Any]]:
        connection = connections[self.connection_alias]
        with connection.cursor() as cursor:
            cursor.execute(query, params)
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]

    def get_blueprint_products(self, type_id: int) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT
                dur.activityId,
                bpo.typeId AS blueprintTypeId,
                bpo.typeName AS blueprint,
                dur.time,
                prd.quantity,
                prd.productTypeID,
                prdt.typeName AS product,
                prdg.groupID AS productGroupId,
                prdg.categoryID AS productCategoryId,
                limits.maxProductionLimit,
                prob.probability,
                COALESCE(mt.metaGroupID, 1) AS metaGroupID
            FROM invTypes bpo
            JOIN industryActivity dur ON dur.typeId = bpo.typeId
            JOIN industryActivityProducts prd ON prd.typeId = bpo.typeId AND prd.activityId = dur.activityId
            LEFT JOIN industryActivityProbabilities prob
                ON prob.typeID = bpo.typeID
               AND prob.activityID = dur.activityID
               AND prob.productTypeID = prd.productTypeID
            JOIN industryBlueprints limits ON limits.typeID = bpo.typeID
            JOIN invTypes prdt ON prdt.typeId = prd.productTypeID
            JOIN invGroups prdg ON prdg.groupID = prdt.groupID
            LEFT JOIN invMetaTypes mt ON mt.typeID = prd.productTypeID
            WHERE bpo.typeId = %s
            """,
            [type_id],
        )

    def get_blueprint_source(self, type_id: int) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT
                dur.activityId,
                bpo.typeId AS blueprintTypeId,
                bpo.typeName AS blueprint,
                dur.time,
                prd.quantity,
                prd.productTypeID,
                prdt.typeName AS product,
                prdg.groupID AS productGroupId,
                prdg.categoryID AS productCategoryId,
                limits.maxProductionLimit,
                prob.probability,
                COALESCE(mt.metaGroupID, 1) AS metaGroupID
            FROM invTypes bpo
            JOIN industryActivity dur ON dur.typeId = bpo.typeId
            JOIN industryActivityProducts prd ON prd.typeId = bpo.typeId AND prd.activityId = dur.activityId
            LEFT JOIN industryActivityProbabilities prob
                ON prob.typeID = bpo.typeID
               AND prob.activityID = dur.activityID
               AND prob.productTypeID = prd.productTypeID
            JOIN industryBlueprints limits ON limits.typeID = bpo.typeID
            JOIN invTypes prdt ON prdt.typeId = prd.productTypeID
            JOIN invGroups prdg ON prdg.groupID = prdt.groupID
            LEFT JOIN invMetaTypes mt ON mt.typeID = prd.productTypeID
            WHERE prd.productTypeID = %s
            """,
            [type_id],
        )

    def get_blueprint_material(self, type_id: int) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT
                mat.activityId,
                mat.materialTypeID,
                matt.typeName AS material,
                matt.groupID AS materialGroupId,
                matg.categoryID AS materialCategoryId,
                mat.quantity,
                prd.typeId AS blueprintTypeId,
                prdt.typeName AS blueprint,
                prd.quantity AS blueprintQuantity
            FROM industryActivityMaterials mat
            JOIN invTypes matt ON matt.typeId = mat.materialTypeID
            JOIN invGroups matg ON matg.groupID = matt.groupID
            LEFT JOIN industryActivityProducts prd ON prd.productTypeID = mat.materialTypeID
            LEFT JOIN invTypes prdt ON prdt.typeId = prd.typeId
            WHERE mat.typeId = %s
              AND (prdt.typeName IS NULL OR prdt.typeName NOT LIKE 'Test%%')
            """,
            [type_id],
        )

    def get_ore_minerals(self, type_name: str) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT o.portionSize, m.quantity, mat.typeName
            FROM invTypes o
            JOIN invTypeMaterials m ON m.typeID = o.typeID
            JOIN invTypes mat ON mat.typeID = m.materialTypeID
            WHERE o.typeName = %s
            """,
            [type_name],
        )


def resolve_material_multipliers(
    industry_structure_type: str | None,
    industry_rig: str | None,
    reaction_rig: str | None,
    manufacturing_role_bonus: float | None = None,
    manufacturing_rig_bonus: float | None = None,
    reaction_rig_bonus: float | None = None,
) -> tuple[float, float, float]:
    if manufacturing_role_bonus is None:
        structure_type = (industry_structure_type or "").strip().lower()
        manufacturing_role_bonus = 1.0 if structure_type in {"station", ""} else 0.99

    if manufacturing_rig_bonus is None:
        manufacturing_rig = (industry_rig or "").strip().upper()
        if manufacturing_rig == "T1":
            manufacturing_rig_bonus = 0.976
        elif manufacturing_rig == "T2":
            manufacturing_rig_bonus = 0.958
        else:
            manufacturing_rig_bonus = 1.0

    if reaction_rig_bonus is None:
        reaction_rig_type = (reaction_rig or "").strip().upper()
        if reaction_rig_type == "T1":
            reaction_rig_bonus = 0.986
        elif reaction_rig_type == "T2":
            reaction_rig_bonus = 0.974
        else:
            reaction_rig_bonus = 1.0

    return float(manufacturing_role_bonus), float(manufacturing_rig_bonus), float(reaction_rig_bonus)


def add_job(
    result: dict[str, Any],
    amount: int,
    level: int,
    job_type: str,
    blueprint_product: dict[str, Any],
    bp_te: int,
    materials: list[dict[str, Any]] | None,
    is_advanced: bool,
) -> int:
    existing = next(
        (
            job
            for job in result["jobs"]
            if job["blueprintTypeId"] == blueprint_product["blueprintTypeId"] and job["type"] == job_type
        ),
        None,
    )

    runs = int(math.ceil(amount / blueprint_product["quantity"]))

    base_time = blueprint_product["time"]
    if job_type == "Manufacturing":
        time = int(math.ceil(base_time * ((100 - bp_te) / 100) * (1 - 0.15) * (1 - 0.2 * 2.1)))
    elif job_type == "Invention":
        time = int(math.ceil(base_time * (1 - 0.15) * (1 - 0.2 * 2.1)))
    elif job_type == "Reaction":
        time = int(math.ceil(base_time * (1 - 0.25) * (1 - 0.2 * 2.1)))
    else:
        time = int(math.ceil(base_time * 0.8))

    if existing:
        existing["runs"] += runs
        if existing["level"] < level:
            existing["level"] = level
        if materials:
            for index, material in enumerate(materials):
                existing["materials"][index]["quantity"] += material["quantity"]
        return runs

    result["jobs"].append(
        {
            "level": level,
            "type": job_type,
            "blueprintTypeId": blueprint_product["blueprintTypeId"],
            "blueprint": blueprint_product["blueprint"],
            "runs": runs,
            "time": time,
            "quantity": blueprint_product["quantity"],
            "productTypeID": blueprint_product["blueprintTypeId"]
            if job_type == "Copying"
            else blueprint_product["productTypeID"],
            "product": blueprint_product["blueprint"] if job_type == "Copying" else blueprint_product["product"],
            "materials": materials,
            "probability": blueprint_product.get("probability"),
            "isAdvanced": is_advanced,
            "maxProductionLimit": blueprint_product.get("maxProductionLimit"),
        }
    )
    return runs


def add_material(
    result: dict[str, Any],
    amount: int,
    level: int,
    product: dict[str, Any],
    material: dict[str, Any],
    bp_me: int,
    is_advanced: bool,
    manufacturing_role_bonus: float = 0.99,
    manufacturing_rig_bonus: float = 0.958,
    reaction_rig_bonus: float = 0.974,
) -> int:
    existing = next(
        (entry for entry in result["materials"] if entry["materialTypeID"] == material["materialTypeID"]),
        None,
    )

    if material["quantity"] == 1:
        quantity = int(math.ceil((amount * material["quantity"]) / product["quantity"]))
    elif material["activityId"] == 1:
        quantity = int(
            math.ceil(
                (
                    amount
                    * material["quantity"]
                    * ((100.0 - float(bp_me)) / 100.0)
                    * float(manufacturing_role_bonus)
                    * float(manufacturing_rig_bonus)
                )
                / product["quantity"]
            )
        )
    else:
        quantity = int(math.ceil((amount * material["quantity"] * float(reaction_rig_bonus)) / product["quantity"]))

    quantity_basic_manufacture = quantity if material["activityId"] == 1 and not is_advanced else 0
    quantity_advanced_manufacture = quantity if material["activityId"] == 1 and is_advanced else 0
    quantity_basic_reaction = quantity if material["activityId"] == 11 and not is_advanced else 0
    quantity_advanced_reaction = quantity if material["activityId"] == 11 and is_advanced else 0

    if existing:
        existing["quantity"] += quantity
        existing["quantityBasicManufacture"] += quantity_basic_manufacture
        existing["quantityAdvancedManufacture"] += quantity_advanced_manufacture
        existing["quantityBasicReaction"] += quantity_basic_reaction
        existing["quantityAdvancedReaction"] += quantity_advanced_reaction
        if existing["level"] < level:
            existing["level"] = level
        return quantity

    result["materials"].append(
        {
            "materialTypeID": material["materialTypeID"],
            "material": material["material"],
            "quantity": quantity,
            "quantityBasicManufacture": quantity_basic_manufacture,
            "quantityAdvancedManufacture": quantity_advanced_manufacture,
            "quantityBasicReaction": quantity_basic_reaction,
            "quantityAdvancedReaction": quantity_advanced_reaction,
            "level": level,
            "activityId": material["activityId"],
            "isInput": False if material.get("blueprintTypeId") else True,
        }
    )
    return quantity


def add_module(
    result: dict[str, Any],
    amount: int,
    level: int,
    type_id: int,
    activity_id: int,
    *,
    merge_modules: bool = False,
) -> None:
    if merge_modules and level > 0:
        level_is_invention_gate = int(level) == 2
        existing = next(
            (
                module
                for module in result.get("modules", [])
                if module.get("typeId") == type_id
                and module.get("activityId") == activity_id
                and (int(module.get("level") or 0) == 2) == level_is_invention_gate
            ),
            None,
        )
        if existing:
            existing["amount"] = int(existing.get("amount") or 0) + int(amount)
            if int(existing.get("level") or 0) != 2:
                existing["level"] = max(int(existing.get("level") or 0), int(level))

            stats = result.setdefault("_merge_stats", {"merged": 0, "added": 0})
            stats["merged"] = int(stats.get("merged") or 0) + 1
            return

    if merge_modules:
        stats = result.setdefault("_merge_stats", {"merged": 0, "added": 0})
        stats["added"] = int(stats.get("added") or 0) + 1

    result["modules"].append({"level": level, "typeId": type_id, "activityId": activity_id, "amount": amount})


def process_blueprint(
    result: dict[str, Any],
    amount: int,
    level: int,
    product: dict[str, Any],
    materials: list[dict[str, Any]],
    activity_id: int,
    typeme: int,
    typete: int,
    copy_bpo: bool,
    produce_fuel_blocks: bool,
    manufacturing_role_bonus: float,
    manufacturing_rig_bonus: float,
    reaction_rig_bonus: float,
    repository: PlannerRepository,
    *,
    merge_modules: bool = False,
) -> dict[str, Any]:
    materials_job: list[dict[str, Any]] = []
    materials_copy: list[dict[str, Any]] = []

    add_copy_job = (
        ((activity_id == 1 and product["metaGroupID"] == 1) or (activity_id == 8 and product["metaGroupID"] == 2))
        and copy_bpo
    )

    is_advanced = False
    for element in materials:
        if element["activityId"] == activity_id:
            if element.get("materialGroupId") == 1136 and not produce_fuel_blocks:
                element.pop("blueprintTypeId", None)
            if element.get("blueprintTypeId"):
                is_advanced = True

    for element in list(materials):
        if (
            element["materialTypeID"] == product["blueprintTypeId"]
            and product["metaGroupID"] == 2
            and product["activityId"] == 1
        ):
            blueprint_source = repository.get_blueprint_source(element["materialTypeID"])
            if blueprint_source:
                quantity = int(math.ceil(amount / blueprint_source[0]["quantity"]))
                add_module(result, quantity, 1, int(blueprint_source[0]["blueprintTypeId"]), 8, merge_modules=merge_modules)

        if (element["activityId"] == activity_id) or (
            copy_bpo and element["activityId"] == 5 and product["metaGroupID"] == 1
        ):
            quantity = add_material(
                result,
                amount,
                level,
                product,
                element,
                typeme,
                is_advanced,
                manufacturing_role_bonus=manufacturing_role_bonus,
                manufacturing_rig_bonus=manufacturing_rig_bonus,
                reaction_rig_bonus=reaction_rig_bonus,
            )

            if element["activityId"] == activity_id:
                if element.get("blueprintTypeId"):
                    add_module(
                        result,
                        quantity,
                        level + 1,
                        int(element["blueprintTypeId"]),
                        int(element["activityId"]),
                        merge_modules=merge_modules,
                    )
                materials_job.append({"type": element["material"], "quantity": quantity, "base_quantity": element["quantity"]})
            else:
                if element.get("blueprintTypeId"):
                    add_module(result, quantity, 11, int(element["blueprintTypeId"]), 1, merge_modules=merge_modules)
                materials_copy.append({"type": element["material"], "quantity": quantity, "base_quantity": element["quantity"]})

    if add_copy_job:
        copy_element = {
            "activityId": 5,
            "materialTypeID": product["blueprintTypeId"],
            "material": product["blueprint"],
            "blueprintTypeId": product["blueprintTypeId"],
            "quantity": 1,
        }
        copy_quantity = int(math.ceil(amount / product["maxProductionLimit"]))
        add_material(
            result,
            copy_quantity,
            10,
            product,
            copy_element,
            0,
            False,
            manufacturing_role_bonus=manufacturing_role_bonus,
            manufacturing_rig_bonus=manufacturing_rig_bonus,
            reaction_rig_bonus=reaction_rig_bonus,
        )
        materials_job.append({"type": copy_element["material"], "quantity": copy_quantity})

    if product["activityId"] == 1:
        runs = add_job(result, amount, level, "Manufacturing", product, typete, materials_job, is_advanced)
        if add_copy_job:
            add_job(result, runs, 12 if level > 10 else 10, "Copying", product, typete, materials_copy, False)
    elif product["activityId"] == 8:
        runs = add_job(result, amount, 9, "Invention", product, typete, materials_job, False)
        if add_copy_job:
            add_job(result, runs, 10, "Copying", product, typete, materials_copy, False)
    elif product["activityId"] == 11:
        add_job(result, amount, level, "Reaction", product, 0, materials_job, is_advanced)

    return result


class IndustryPlannerService:
    def __init__(self, *, repository: PlannerRepository | None = None) -> None:
        self.repository = repository or DjangoSdeRepository()

    def get_blueprints_details(
        self,
        *,
        types: list[dict[str, Any]],
        efficiency: dict[str, Any],
        build_t1: bool,
        copy_bpo: bool,
        produce_fuel_blocks: bool,
        merge_modules: bool = False,
        manufacturing_role_bonus: float = 0.99,
        manufacturing_rig_bonus: float = 0.958,
        reaction_rig_bonus: float = 0.974,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"jobs": [], "materials": [], "modules": []}

        for item in types:
            add_module(result, int(item["amount"]), 1, int(item["typeId"]), 1, merge_modules=merge_modules)

        while result["modules"]:
            module = result["modules"].pop(0)
            blueprint_products = self.repository.get_blueprint_products(int(module["typeId"]))

            for element in list(blueprint_products):
                if not (
                    element["activityId"] == module["activityId"]
                    or (module["activityId"] == 1 and element["activityId"] == 11)
                    or (module["activityId"] == 11 and element["activityId"] == 1)
                    or (module["level"] == 2 and element["activityId"] == 8)
                ):
                    continue

                blueprint_product = element
                amount = int(math.ceil(int(module["amount"]) / blueprint_product["quantity"]) * blueprint_product["quantity"])
                level = int(module["level"])

                blueprint_material = self.repository.get_blueprint_material(int(module["typeId"]))

                if blueprint_product["productCategoryId"] == 6:
                    if blueprint_product["metaGroupID"] == 1:
                        me = int(efficiency.get("shipT1ME") or 0)
                        te = int(efficiency.get("shipT1TE") or 0)
                    else:
                        me = int(efficiency.get("shipT2ME") or 0)
                        te = int(efficiency.get("shipT2TE") or 0)
                else:
                    if blueprint_product["metaGroupID"] == 1:
                        me = int(efficiency.get("moduleT1ME") or 0)
                        te = int(efficiency.get("moduleT1TE") or 0)
                    else:
                        me = int(efficiency.get("moduleT2ME") or 0)
                        te = int(efficiency.get("moduleT2TE") or 0)

                if blueprint_product["productCategoryId"] == 6:
                    if (not build_t1) and (not any(entry.get("typeId") == blueprint_product["blueprintTypeId"] for entry in types)):
                        continue

                if blueprint_product["activityId"] == 8:
                    if not any(entry.get("typeId") == blueprint_product["productTypeID"] for entry in types):
                        continue

                if blueprint_product["metaGroupID"] == 2:
                    blueprint_material.append(
                        {
                            "materialTypeID": blueprint_product["blueprintTypeId"],
                            "activityId": 1,
                            "material": blueprint_product["blueprint"],
                            "quantity": 1,
                        }
                    )

                process_blueprint(
                    result,
                    amount,
                    level,
                    blueprint_product,
                    blueprint_material,
                    blueprint_product["activityId"],
                    me,
                    te,
                    copy_bpo,
                    produce_fuel_blocks,
                    manufacturing_role_bonus,
                    manufacturing_rig_bonus,
                    reaction_rig_bonus,
                    self.repository,
                    merge_modules=merge_modules,
                )

        if merge_modules:
            stats = result.get("_merge_stats") or {}
            result["meta"] = {
                "mergeModules": True,
                "modulesAdded": int(stats.get("added") or 0),
                "modulesMerged": int(stats.get("merged") or 0),
            }
            result.pop("_merge_stats", None)

        result.pop("modules", None)
        return result

    def get_blueprint_details(
        self,
        *,
        type_id: int,
        amount: int,
        efficiency: dict[str, Any],
        build_t1: bool,
        copy_bpo: bool,
        produce_fuel_blocks: bool,
        merge_modules: bool = False,
        manufacturing_role_bonus: float = 0.99,
        manufacturing_rig_bonus: float = 0.958,
        reaction_rig_bonus: float = 0.974,
    ) -> dict[str, Any]:
        return self.get_blueprints_details(
            types=[{"typeId": int(type_id), "amount": int(amount)}],
            efficiency=efficiency,
            build_t1=build_t1,
            copy_bpo=copy_bpo,
            produce_fuel_blocks=produce_fuel_blocks,
            merge_modules=merge_modules,
            manufacturing_role_bonus=manufacturing_role_bonus,
            manufacturing_rig_bonus=manufacturing_rig_bonus,
            reaction_rig_bonus=reaction_rig_bonus,
        )

    def get_ore_details(self, type_name: str) -> list[dict[str, Any]]:
        return self.repository.get_ore_minerals(type_name)

    def rebuild_project_plan(
        self,
        *,
        project: Project,
        types: list[dict[str, Any]],
        efficiency: dict[str, Any],
        build_t1: bool,
        copy_bpo: bool,
        produce_fuel_blocks: bool,
        merge_modules: bool = False,
        manufacturing_role_bonus: float = 0.99,
        manufacturing_rig_bonus: float = 0.958,
        reaction_rig_bonus: float = 0.974,
    ) -> dict[str, Any]:
        result = self.get_blueprints_details(
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
        params_hash = self._build_params_hash(
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

        material_lookup = {entry["material"]: entry for entry in result.get("materials", [])}

        with transaction.atomic():
            project.plan_materials.all().delete()
            project.plan_jobs.all().delete()

            for material in result.get("materials", []):
                PlanMaterial.objects.create(
                    project=project,
                    plan_job=None,
                    material_type_id=int(material["materialTypeID"]),
                    quantity_total=int(material["quantity"]),
                    activity_id=int(material.get("activityId") or 0),
                    level=int(material.get("level") or 0),
                    is_input=bool(material.get("isInput", True)),
                    is_intermediate=not bool(material.get("isInput", True)),
                )

            for job in result.get("jobs", []):
                plan_job = PlanJob.objects.create(
                    project=project,
                    activity_id=self._job_type_to_activity_id(job["type"]),
                    blueprint_type_id=int(job["blueprintTypeId"]),
                    product_type_id=int(job["productTypeID"]),
                    runs=int(job["runs"]),
                    expected_duration_s=int(job.get("time") or 0),
                    level=int(job.get("level") or 0),
                    probability=job.get("probability"),
                    is_advanced=bool(job.get("isAdvanced", False)),
                    params_hash=params_hash,
                )

                for material in job.get("materials") or []:
                    aggregate_material = material_lookup.get(material["type"])
                    if aggregate_material is None:
                        continue
                    PlanMaterial.objects.create(
                        project=project,
                        plan_job=plan_job,
                        material_type_id=int(aggregate_material["materialTypeID"]),
                        quantity_total=int(material["quantity"]),
                        activity_id=int(aggregate_material.get("activityId") or 0),
                        level=int(job.get("level") or 0),
                        is_input=bool(aggregate_material.get("isInput", True)),
                        is_intermediate=not bool(aggregate_material.get("isInput", True)),
                    )

        return result

    @staticmethod
    def _build_params_hash(**params: Any) -> str:
        serialized = json.dumps(params, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _job_type_to_activity_id(job_type: str) -> int:
        mapping = {
            "Manufacturing": 1,
            "Copying": 5,
            "Invention": 8,
            "Reaction": 11,
        }
        return mapping.get(job_type, 0)