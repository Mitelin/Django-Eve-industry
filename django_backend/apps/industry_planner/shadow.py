from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from apps.industry_planner.services import IndustryPlannerService


_LEGACY_ROOT = Path(__file__).resolve().parents[3] / "ZAMEK"
if str(_LEGACY_ROOT) not in sys.path:
    sys.path.append(str(_LEGACY_ROOT))

from py_backend.services import blueprints as legacy_blueprints


_GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


class _FakePlannerRepository:
    def __init__(self) -> None:
        self.blueprint_products: dict[int, list[dict[str, Any]]] = {}
        self.blueprint_materials: dict[int, list[dict[str, Any]]] = {}
        self.blueprint_sources: dict[int, list[dict[str, Any]]] = {}

    def get_blueprint_products(self, type_id: int) -> list[dict[str, Any]]:
        return [dict(item) for item in self.blueprint_products.get(type_id, [])]

    def get_blueprint_source(self, type_id: int) -> list[dict[str, Any]]:
        return [dict(item) for item in self.blueprint_sources.get(type_id, [])]

    def get_blueprint_material(self, type_id: int) -> list[dict[str, Any]]:
        return [dict(item) for item in self.blueprint_materials.get(type_id, [])]

    def get_ore_minerals(self, _type_name: str) -> list[dict[str, Any]]:
        return []


def _build_recursive_repository() -> _FakePlannerRepository:
    repository = _FakePlannerRepository()
    repository.blueprint_products = {
        100: [{"activityId": 1, "blueprintTypeId": 100, "blueprint": "Final Blueprint", "time": 120, "quantity": 1, "productTypeID": 200, "product": "Final Product", "productGroupId": 10, "productCategoryId": 7, "maxProductionLimit": 300, "probability": None, "metaGroupID": 1}],
        300: [{"activityId": 1, "blueprintTypeId": 300, "blueprint": "Intermediate Blueprint", "time": 60, "quantity": 1, "productTypeID": 201, "product": "Intermediate Product", "productGroupId": 11, "productCategoryId": 7, "maxProductionLimit": 300, "probability": None, "metaGroupID": 1}],
    }
    repository.blueprint_materials = {
        100: [
            {"activityId": 1, "materialTypeID": 500, "material": "Tritanium", "materialGroupId": 18, "materialCategoryId": 4, "quantity": 10, "blueprintTypeId": None, "blueprint": None, "blueprintQuantity": None},
            {"activityId": 1, "materialTypeID": 201, "material": "Intermediate Product", "materialGroupId": 20, "materialCategoryId": 7, "quantity": 2, "blueprintTypeId": 300, "blueprint": "Intermediate Blueprint", "blueprintQuantity": 1},
        ],
        300: [{"activityId": 1, "materialTypeID": 501, "material": "Pyerite", "materialGroupId": 18, "materialCategoryId": 4, "quantity": 5, "blueprintTypeId": None, "blueprint": None, "blueprintQuantity": None}],
    }
    return repository


def _build_copy_bpo_repository() -> _FakePlannerRepository:
    repository = _FakePlannerRepository()
    repository.blueprint_products = {
        400: [{"activityId": 1, "blueprintTypeId": 400, "blueprint": "Copy Blueprint", "time": 100, "quantity": 2, "productTypeID": 401, "product": "Copy Product", "productGroupId": 12, "productCategoryId": 7, "maxProductionLimit": 3, "probability": None, "metaGroupID": 1}],
    }
    repository.blueprint_materials = {
        400: [
            {"activityId": 1, "materialTypeID": 402, "material": "Tritanium", "materialGroupId": 18, "materialCategoryId": 4, "quantity": 4, "blueprintTypeId": None, "blueprint": None, "blueprintQuantity": None},
            {"activityId": 5, "materialTypeID": 403, "material": "Copy Material", "materialGroupId": 100, "materialCategoryId": 9, "quantity": 2, "blueprintTypeId": None, "blueprint": None, "blueprintQuantity": None},
        ],
    }
    return repository


def _build_reaction_repository() -> _FakePlannerRepository:
    repository = _FakePlannerRepository()
    repository.blueprint_products = {
        500: [{"activityId": 11, "blueprintTypeId": 500, "blueprint": "Reaction Formula", "time": 100, "quantity": 2, "productTypeID": 501, "product": "Reaction Product", "productGroupId": 13, "productCategoryId": 7, "maxProductionLimit": 300, "probability": None, "metaGroupID": 1}],
    }
    repository.blueprint_materials = {
        500: [{"activityId": 11, "materialTypeID": 502, "material": "Reaction Input", "materialGroupId": 1136, "materialCategoryId": 9, "quantity": 10, "blueprintTypeId": None, "blueprint": None, "blueprintQuantity": None}],
    }
    return repository


def _build_ship_gate_repository() -> _FakePlannerRepository:
    repository = _FakePlannerRepository()
    repository.blueprint_products = {
        900: [{"activityId": 1, "blueprintTypeId": 900, "blueprint": "Advanced Hull Blueprint", "time": 180, "quantity": 1, "productTypeID": 901, "product": "Advanced Hull", "productGroupId": 25, "productCategoryId": 6, "maxProductionLimit": 100, "probability": None, "metaGroupID": 2}],
        800: [{"activityId": 1, "blueprintTypeId": 800, "blueprint": "Base Hull Blueprint", "time": 120, "quantity": 1, "productTypeID": 801, "product": "Base Hull", "productGroupId": 24, "productCategoryId": 6, "maxProductionLimit": 100, "probability": None, "metaGroupID": 1}],
    }
    repository.blueprint_materials = {
        900: [
            {"activityId": 1, "materialTypeID": 801, "material": "Base Hull", "materialGroupId": 24, "materialCategoryId": 6, "quantity": 1, "blueprintTypeId": 800, "blueprint": "Base Hull Blueprint", "blueprintQuantity": 1},
            {"activityId": 1, "materialTypeID": 804, "material": "Capital Plate", "materialGroupId": 18, "materialCategoryId": 4, "quantity": 8, "blueprintTypeId": None, "blueprint": None, "blueprintQuantity": None},
        ],
        800: [{"activityId": 1, "materialTypeID": 805, "material": "Base Mineral", "materialGroupId": 18, "materialCategoryId": 4, "quantity": 20, "blueprintTypeId": None, "blueprint": None, "blueprintQuantity": None}],
    }
    return repository


def _run_legacy_blueprints_details(
    repository: _FakePlannerRepository,
    *,
    types: list[dict[str, Any]],
    efficiency: dict[str, Any],
    build_t1: bool,
    copy_bpo: bool,
    produce_fuel_blocks: bool,
    manufacturing_role_bonus: float = 0.99,
    manufacturing_rig_bonus: float = 0.958,
    reaction_rig_bonus: float = 0.974,
) -> dict[str, Any]:
    async def _get_blueprint_products(type_id: int) -> list[dict[str, Any]]:
        return repository.get_blueprint_products(type_id)

    async def _get_blueprint_source(type_id: int) -> list[dict[str, Any]]:
        return repository.get_blueprint_source(type_id)

    async def _get_blueprint_material(type_id: int) -> list[dict[str, Any]]:
        return repository.get_blueprint_material(type_id)

    async def _get_ore_minerals(type_name: str) -> list[dict[str, Any]]:
        return repository.get_ore_minerals(type_name)

    async def _run() -> dict[str, Any]:
        with (
            patch.object(legacy_blueprints, "get_blueprint_products", _get_blueprint_products),
            patch.object(legacy_blueprints, "get_blueprint_source", _get_blueprint_source),
            patch.object(legacy_blueprints, "get_blueprint_material", _get_blueprint_material),
            patch.object(legacy_blueprints, "get_ore_minerals", _get_ore_minerals),
        ):
            return await legacy_blueprints.get_blueprints_details(
                types=types,
                efficiency=efficiency,
                build_t1=build_t1,
                copy_bpo=copy_bpo,
                produce_fuel_blocks=produce_fuel_blocks,
                manufacturing_role_bonus=manufacturing_role_bonus,
                manufacturing_rig_bonus=manufacturing_rig_bonus,
                reaction_rig_bonus=reaction_rig_bonus,
            )

    return asyncio.run(_run())


def _load_golden(name: str) -> Any:
    return json.loads((_GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))


def generate_shadow_planner_report() -> dict[str, Any]:
    scenario_definitions = [
        {"name": "planner_calculate_recursive_basic", "label": "Recursive manufacturing", "repository": _build_recursive_repository(), "types": [{"typeId": 100, "amount": 3}], "efficiency": {"moduleT1ME": 0, "moduleT1TE": 0}, "build_t1": True, "copy_bpo": False, "produce_fuel_blocks": False, "manufacturing_role_bonus": 1.0, "manufacturing_rig_bonus": 1.0, "reaction_rig_bonus": 1.0},
        {"name": "planner_calculate_copy_bpo_basic", "label": "Copy BPO flow", "repository": _build_copy_bpo_repository(), "types": [{"typeId": 400, "amount": 5}], "efficiency": {"moduleT1ME": 0, "moduleT1TE": 0}, "build_t1": True, "copy_bpo": True, "produce_fuel_blocks": False, "manufacturing_role_bonus": 0.99, "manufacturing_rig_bonus": 0.958, "reaction_rig_bonus": 0.974},
        {"name": "planner_calculate_reaction_basic", "label": "Reaction flow", "repository": _build_reaction_repository(), "types": [{"typeId": 500, "amount": 3}], "efficiency": {"moduleT1ME": 0, "moduleT1TE": 0}, "build_t1": True, "copy_bpo": False, "produce_fuel_blocks": True, "manufacturing_role_bonus": 1.0, "manufacturing_rig_bonus": 1.0, "reaction_rig_bonus": 0.974},
        {"name": "planner_calculate_build_t1_false_ship_gate", "label": "Build T1 false ship gate", "repository": _build_ship_gate_repository(), "types": [{"typeId": 900, "amount": 1}], "efficiency": {"shipT1ME": 0, "shipT1TE": 0, "shipT2ME": 0, "shipT2TE": 0}, "build_t1": False, "copy_bpo": False, "produce_fuel_blocks": False, "manufacturing_role_bonus": 0.99, "manufacturing_rig_bonus": 0.958, "reaction_rig_bonus": 0.974},
    ]
    scenarios: list[dict[str, Any]] = []
    matched_golden = 0
    matched_legacy = 0
    for definition in scenario_definitions:
        current = IndustryPlannerService(repository=definition["repository"]).get_blueprints_details(
            types=definition["types"],
            efficiency=definition["efficiency"],
            build_t1=definition["build_t1"],
            copy_bpo=definition["copy_bpo"],
            produce_fuel_blocks=definition["produce_fuel_blocks"],
            manufacturing_role_bonus=float(definition.get("manufacturing_role_bonus", 0.99)),
            manufacturing_rig_bonus=float(definition.get("manufacturing_rig_bonus", 0.958)),
            reaction_rig_bonus=float(definition.get("reaction_rig_bonus", 0.974)),
        )
        legacy = _run_legacy_blueprints_details(
            definition["repository"],
            types=definition["types"],
            efficiency=definition["efficiency"],
            build_t1=definition["build_t1"],
            copy_bpo=definition["copy_bpo"],
            produce_fuel_blocks=definition["produce_fuel_blocks"],
            manufacturing_role_bonus=float(definition.get("manufacturing_role_bonus", 0.99)),
            manufacturing_rig_bonus=float(definition.get("manufacturing_rig_bonus", 0.958)),
            reaction_rig_bonus=float(definition.get("reaction_rig_bonus", 0.974)),
        )
        golden_match = current == _load_golden(definition["name"])
        legacy_match = current == legacy
        matched_golden += 1 if golden_match else 0
        matched_legacy += 1 if legacy_match else 0
        scenarios.append({"name": definition["name"], "label": definition["label"], "goldenMatch": golden_match, "legacyMatch": legacy_match, "jobCount": len(current.get("jobs") or []), "materialCount": len(current.get("materials") or [])})
    return {"planner": {"scenarioCount": len(scenarios), "matchedGolden": matched_golden, "matchedLegacy": matched_legacy, "allGoldenMatched": matched_golden == len(scenarios), "allLegacyMatched": matched_legacy == len(scenarios), "scenarios": scenarios}}