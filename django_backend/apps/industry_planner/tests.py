from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import sys
from typing import Any
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.industry_planner.models import PlanJob, PlanMaterial, Project, ProjectTarget
from apps.industry_planner.shadow import generate_shadow_planner_report
from apps.industry_planner.services import IndustryPlannerService, add_material, resolve_material_multipliers


_LEGACY_ROOT = Path(__file__).resolve().parents[3] / "ZAMEK"
if str(_LEGACY_ROOT) not in sys.path:
    sys.path.append(str(_LEGACY_ROOT))

from py_backend.services import blueprints as legacy_blueprints


_GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


def assert_json_golden(name: str, data: Any) -> None:
    path = _GOLDEN_DIR / f"{name}.json"
    expected = path.read_text(encoding="utf-8")
    serialized = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    assert expected == serialized


class _FakePlannerRepository:
    def __init__(self) -> None:
        self.blueprint_products: dict[int, list[dict[str, Any]]] = {}
        self.blueprint_materials: dict[int, list[dict[str, Any]]] = {}
        self.blueprint_sources: dict[int, list[dict[str, Any]]] = {}
        self.ore_minerals: dict[str, list[dict[str, Any]]] = {}

    def get_blueprint_products(self, type_id: int) -> list[dict[str, Any]]:
        return [dict(item) for item in self.blueprint_products.get(type_id, [])]

    def get_blueprint_source(self, type_id: int) -> list[dict[str, Any]]:
        return [dict(item) for item in self.blueprint_sources.get(type_id, [])]

    def get_blueprint_material(self, type_id: int) -> list[dict[str, Any]]:
        return [dict(item) for item in self.blueprint_materials.get(type_id, [])]

    def get_ore_minerals(self, type_name: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self.ore_minerals.get(type_name, [])]


def build_recursive_repository() -> _FakePlannerRepository:
    repository = _FakePlannerRepository()
    repository.blueprint_products = {
        100: [
            {
                "activityId": 1,
                "blueprintTypeId": 100,
                "blueprint": "Final Blueprint",
                "time": 120,
                "quantity": 1,
                "productTypeID": 200,
                "product": "Final Product",
                "productGroupId": 10,
                "productCategoryId": 7,
                "maxProductionLimit": 300,
                "probability": None,
                "metaGroupID": 1,
            }
        ],
        300: [
            {
                "activityId": 1,
                "blueprintTypeId": 300,
                "blueprint": "Intermediate Blueprint",
                "time": 60,
                "quantity": 1,
                "productTypeID": 201,
                "product": "Intermediate Product",
                "productGroupId": 11,
                "productCategoryId": 7,
                "maxProductionLimit": 300,
                "probability": None,
                "metaGroupID": 1,
            }
        ],
    }
    repository.blueprint_materials = {
        100: [
            {
                "activityId": 1,
                "materialTypeID": 500,
                "material": "Tritanium",
                "materialGroupId": 18,
                "materialCategoryId": 4,
                "quantity": 10,
                "blueprintTypeId": None,
                "blueprint": None,
                "blueprintQuantity": None,
            },
            {
                "activityId": 1,
                "materialTypeID": 201,
                "material": "Intermediate Product",
                "materialGroupId": 20,
                "materialCategoryId": 7,
                "quantity": 2,
                "blueprintTypeId": 300,
                "blueprint": "Intermediate Blueprint",
                "blueprintQuantity": 1,
            },
        ],
        300: [
            {
                "activityId": 1,
                "materialTypeID": 501,
                "material": "Pyerite",
                "materialGroupId": 18,
                "materialCategoryId": 4,
                "quantity": 5,
                "blueprintTypeId": None,
                "blueprint": None,
                "blueprintQuantity": None,
            }
        ],
    }
    repository.ore_minerals = {
        "Veldspar": [{"portionSize": 100, "quantity": 415, "typeName": "Tritanium"}],
    }
    return repository


def build_copy_bpo_repository() -> _FakePlannerRepository:
    repository = _FakePlannerRepository()
    repository.blueprint_products = {
        400: [
            {
                "activityId": 1,
                "blueprintTypeId": 400,
                "blueprint": "Copy Blueprint",
                "time": 100,
                "quantity": 2,
                "productTypeID": 401,
                "product": "Copy Product",
                "productGroupId": 12,
                "productCategoryId": 7,
                "maxProductionLimit": 3,
                "probability": None,
                "metaGroupID": 1,
            }
        ]
    }
    repository.blueprint_materials = {
        400: [
            {
                "activityId": 1,
                "materialTypeID": 402,
                "material": "Tritanium",
                "materialGroupId": 18,
                "materialCategoryId": 4,
                "quantity": 4,
                "blueprintTypeId": None,
                "blueprint": None,
                "blueprintQuantity": None,
            },
            {
                "activityId": 5,
                "materialTypeID": 403,
                "material": "Copy Material",
                "materialGroupId": 100,
                "materialCategoryId": 9,
                "quantity": 2,
                "blueprintTypeId": None,
                "blueprint": None,
                "blueprintQuantity": None,
            },
        ]
    }
    return repository


def build_reaction_repository() -> _FakePlannerRepository:
    repository = _FakePlannerRepository()
    repository.blueprint_products = {
        500: [
            {
                "activityId": 11,
                "blueprintTypeId": 500,
                "blueprint": "Reaction Formula",
                "time": 100,
                "quantity": 2,
                "productTypeID": 501,
                "product": "Reaction Product",
                "productGroupId": 13,
                "productCategoryId": 7,
                "maxProductionLimit": 300,
                "probability": None,
                "metaGroupID": 1,
            }
        ]
    }
    repository.blueprint_materials = {
        500: [
            {
                "activityId": 11,
                "materialTypeID": 502,
                "material": "Reaction Input",
                "materialGroupId": 1136,
                "materialCategoryId": 9,
                "quantity": 10,
                "blueprintTypeId": None,
                "blueprint": None,
                "blueprintQuantity": None,
            }
        ]
    }
    return repository


def build_ship_gate_repository() -> _FakePlannerRepository:
    repository = _FakePlannerRepository()
    repository.blueprint_products = {
        900: [
            {
                "activityId": 1,
                "blueprintTypeId": 900,
                "blueprint": "Advanced Hull Blueprint",
                "time": 180,
                "quantity": 1,
                "productTypeID": 901,
                "product": "Advanced Hull",
                "productGroupId": 25,
                "productCategoryId": 6,
                "maxProductionLimit": 100,
                "probability": None,
                "metaGroupID": 2,
            }
        ],
        800: [
            {
                "activityId": 1,
                "blueprintTypeId": 800,
                "blueprint": "Base Hull Blueprint",
                "time": 120,
                "quantity": 1,
                "productTypeID": 801,
                "product": "Base Hull",
                "productGroupId": 24,
                "productCategoryId": 6,
                "maxProductionLimit": 100,
                "probability": None,
                "metaGroupID": 1,
            }
        ],
    }
    repository.blueprint_materials = {
        900: [
            {
                "activityId": 1,
                "materialTypeID": 801,
                "material": "Base Hull",
                "materialGroupId": 24,
                "materialCategoryId": 6,
                "quantity": 1,
                "blueprintTypeId": 800,
                "blueprint": "Base Hull Blueprint",
                "blueprintQuantity": 1,
            },
            {
                "activityId": 1,
                "materialTypeID": 804,
                "material": "Capital Plate",
                "materialGroupId": 18,
                "materialCategoryId": 4,
                "quantity": 8,
                "blueprintTypeId": None,
                "blueprint": None,
                "blueprintQuantity": None,
            },
        ],
        800: [
            {
                "activityId": 1,
                "materialTypeID": 805,
                "material": "Base Mineral",
                "materialGroupId": 18,
                "materialCategoryId": 4,
                "quantity": 20,
                "blueprintTypeId": None,
                "blueprint": None,
                "blueprintQuantity": None,
            }
        ],
    }
    return repository


def run_legacy_blueprints_details(
    repository: _FakePlannerRepository,
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
                merge_modules=merge_modules,
                manufacturing_role_bonus=manufacturing_role_bonus,
                manufacturing_rig_bonus=manufacturing_rig_bonus,
                reaction_rig_bonus=reaction_rig_bonus,
            )

    return asyncio.run(_run())


def run_legacy_ore_details(repository: _FakePlannerRepository, type_name: str) -> list[dict[str, Any]]:
    async def _get_ore_minerals(name: str) -> list[dict[str, Any]]:
        return repository.get_ore_minerals(name)

    async def _run() -> list[dict[str, Any]]:
        with patch.object(legacy_blueprints, "get_ore_minerals", _get_ore_minerals):
            return await legacy_blueprints.get_ore_details(type_name)

    return asyncio.run(_run())


class PlannerServiceTests(SimpleTestCase):
    def test_resolve_material_multipliers_preserves_legacy_defaults(self) -> None:
        manufacturing_role_bonus, manufacturing_rig_bonus, reaction_rig_bonus = resolve_material_multipliers(
            industry_structure_type="athanor",
            industry_rig="T2",
            reaction_rig="T1",
        )

        self.assertEqual(manufacturing_role_bonus, 0.99)
        self.assertEqual(manufacturing_rig_bonus, 0.958)
        self.assertEqual(reaction_rig_bonus, 0.986)

    def test_add_material_rounding_manufacturing_me(self) -> None:
        result = {"materials": []}
        product = {"quantity": 10}
        material = {"materialTypeID": 1, "material": "Tritanium", "quantity": 37, "activityId": 1}

        quantity = add_material(result, amount=10, level=1, product=product, material=material, bp_me=10, is_advanced=False)

        expected = math.ceil((10 * 37 * ((100.0 - 10) / 100.0) * 0.99 * 0.958) / 10)
        self.assertEqual(quantity, expected)

    def test_get_blueprint_details_recurses_into_intermediate_blueprints(self) -> None:
        repository = build_recursive_repository()
        service = IndustryPlannerService(repository=repository)

        result = service.get_blueprint_details(
            type_id=100,
            amount=3,
            efficiency={"moduleT1ME": 0, "moduleT1TE": 0},
            build_t1=True,
            copy_bpo=False,
            produce_fuel_blocks=False,
        )

        self.assertEqual(len(result["jobs"]), 2)
        self.assertEqual({job["blueprintTypeId"] for job in result["jobs"]}, {100, 300})
        self.assertEqual(next(job for job in result["jobs"] if job["blueprintTypeId"] == 100)["runs"], 3)
        self.assertEqual(next(job for job in result["jobs"] if job["blueprintTypeId"] == 300)["runs"], 6)

        materials = {material["materialTypeID"]: material for material in result["materials"]}
        self.assertEqual(materials[500]["quantity"], 29)
        self.assertEqual(materials[500]["isInput"], True)
        self.assertEqual(materials[201]["quantity"], 6)
        self.assertEqual(materials[201]["isInput"], False)
        self.assertEqual(materials[201]["quantityAdvancedManufacture"], 6)
        self.assertEqual(materials[501]["quantity"], 29)

    def test_service_matches_legacy_python_for_recursive_scenario(self) -> None:
        repository = build_recursive_repository()
        service = IndustryPlannerService(repository=repository)

        current = service.get_blueprints_details(
            types=[{"typeId": 100, "amount": 3}],
            efficiency={"moduleT1ME": 0, "moduleT1TE": 0},
            build_t1=True,
            copy_bpo=False,
            produce_fuel_blocks=False,
        )
        legacy = run_legacy_blueprints_details(
            repository,
            types=[{"typeId": 100, "amount": 3}],
            efficiency={"moduleT1ME": 0, "moduleT1TE": 0},
            build_t1=True,
            copy_bpo=False,
            produce_fuel_blocks=False,
        )

        self.assertEqual(current, legacy)

    def test_service_matches_legacy_python_for_merge_modules_scenario(self) -> None:
        repository = _FakePlannerRepository()
        repository.blueprint_products = {
            100: [
                {
                    "activityId": 1,
                    "blueprintTypeId": 100,
                    "blueprint": "Batch Blueprint",
                    "time": 100,
                    "quantity": 2,
                    "productTypeID": 200,
                    "product": "Batch Product",
                    "productGroupId": 10,
                    "productCategoryId": 7,
                    "maxProductionLimit": 300,
                    "probability": None,
                    "metaGroupID": 1,
                }
            ]
        }
        repository.blueprint_materials = {100: []}
        service = IndustryPlannerService(repository=repository)

        current = service.get_blueprints_details(
            types=[{"typeId": 100, "amount": 1}, {"typeId": 100, "amount": 1}],
            efficiency={"moduleT1ME": 0, "moduleT1TE": 0},
            build_t1=True,
            copy_bpo=False,
            produce_fuel_blocks=False,
            merge_modules=True,
        )
        legacy = run_legacy_blueprints_details(
            repository,
            types=[{"typeId": 100, "amount": 1}, {"typeId": 100, "amount": 1}],
            efficiency={"moduleT1ME": 0, "moduleT1TE": 0},
            build_t1=True,
            copy_bpo=False,
            produce_fuel_blocks=False,
            merge_modules=True,
        )

        self.assertEqual(current, legacy)

    def test_service_matches_legacy_python_for_ore_details(self) -> None:
        repository = build_recursive_repository()
        service = IndustryPlannerService(repository=repository)

        current = service.get_ore_details("Veldspar")
        legacy = run_legacy_ore_details(repository, "Veldspar")

        self.assertEqual(current, legacy)

    def test_service_matches_legacy_python_for_copy_bpo_scenario(self) -> None:
        repository = build_copy_bpo_repository()
        service = IndustryPlannerService(repository=repository)

        current = service.get_blueprints_details(
            types=[{"typeId": 400, "amount": 5}],
            efficiency={"moduleT1ME": 0, "moduleT1TE": 0},
            build_t1=True,
            copy_bpo=True,
            produce_fuel_blocks=False,
        )
        legacy = run_legacy_blueprints_details(
            repository,
            types=[{"typeId": 400, "amount": 5}],
            efficiency={"moduleT1ME": 0, "moduleT1TE": 0},
            build_t1=True,
            copy_bpo=True,
            produce_fuel_blocks=False,
        )

        self.assertEqual(current, legacy)
        self.assertEqual({job["type"] for job in current["jobs"]}, {"Manufacturing", "Copying"})

    def test_service_matches_legacy_python_for_reaction_scenario(self) -> None:
        repository = build_reaction_repository()
        service = IndustryPlannerService(repository=repository)

        current = service.get_blueprints_details(
            types=[{"typeId": 500, "amount": 3}],
            efficiency={"moduleT1ME": 0, "moduleT1TE": 0},
            build_t1=True,
            copy_bpo=False,
            produce_fuel_blocks=True,
            reaction_rig_bonus=0.974,
        )
        legacy = run_legacy_blueprints_details(
            repository,
            types=[{"typeId": 500, "amount": 3}],
            efficiency={"moduleT1ME": 0, "moduleT1TE": 0},
            build_t1=True,
            copy_bpo=False,
            produce_fuel_blocks=True,
            reaction_rig_bonus=0.974,
        )

        self.assertEqual(current, legacy)
        self.assertEqual(current["jobs"][0]["type"], "Reaction")

    def test_service_matches_legacy_python_for_build_t1_false_ship_gate(self) -> None:
        repository = build_ship_gate_repository()
        service = IndustryPlannerService(repository=repository)

        current = service.get_blueprints_details(
            types=[{"typeId": 900, "amount": 1}],
            efficiency={"shipT1ME": 0, "shipT1TE": 0, "shipT2ME": 0, "shipT2TE": 0},
            build_t1=False,
            copy_bpo=False,
            produce_fuel_blocks=False,
        )
        legacy = run_legacy_blueprints_details(
            repository,
            types=[{"typeId": 900, "amount": 1}],
            efficiency={"shipT1ME": 0, "shipT1TE": 0, "shipT2ME": 0, "shipT2TE": 0},
            build_t1=False,
            copy_bpo=False,
            produce_fuel_blocks=False,
        )

        self.assertEqual(current, legacy)
        self.assertEqual({job["blueprintTypeId"] for job in current["jobs"]}, {900})
        materials = {material["materialTypeID"]: material for material in current["materials"]}
        self.assertIn(801, materials)
        self.assertNotIn(805, materials)

    def test_merge_modules_changes_run_rounding_and_emits_meta(self) -> None:
        repository = _FakePlannerRepository()
        repository.blueprint_products = {
            100: [
                {
                    "activityId": 1,
                    "blueprintTypeId": 100,
                    "blueprint": "Batch Blueprint",
                    "time": 100,
                    "quantity": 2,
                    "productTypeID": 200,
                    "product": "Batch Product",
                    "productGroupId": 10,
                    "productCategoryId": 7,
                    "maxProductionLimit": 300,
                    "probability": None,
                    "metaGroupID": 1,
                }
            ]
        }
        repository.blueprint_materials = {100: []}
        service = IndustryPlannerService(repository=repository)

        without_merge = service.get_blueprints_details(
            types=[{"typeId": 100, "amount": 1}, {"typeId": 100, "amount": 1}],
            efficiency={"moduleT1ME": 0, "moduleT1TE": 0},
            build_t1=True,
            copy_bpo=False,
            produce_fuel_blocks=False,
            merge_modules=False,
        )
        with_merge = service.get_blueprints_details(
            types=[{"typeId": 100, "amount": 1}, {"typeId": 100, "amount": 1}],
            efficiency={"moduleT1ME": 0, "moduleT1TE": 0},
            build_t1=True,
            copy_bpo=False,
            produce_fuel_blocks=False,
            merge_modules=True,
        )

        self.assertEqual(without_merge["jobs"][0]["runs"], 2)
        self.assertEqual(with_merge["jobs"][0]["runs"], 1)
        self.assertEqual(with_merge["meta"], {"mergeModules": True, "modulesAdded": 1, "modulesMerged": 1})


class PlannerRouteTests(SimpleTestCase):
    def test_shadow_planner_report_route_returns_summary(self) -> None:
        response = self.client.get("/api/reports/shadow/planner")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["planner"]["scenarioCount"], 4)
        self.assertTrue(response.json()["planner"]["allGoldenMatched"])
        self.assertTrue(response.json()["planner"]["allLegacyMatched"])

    def test_calculate_route_real_service_matches_golden_recursive_dataset(self) -> None:
        service = IndustryPlannerService(repository=build_recursive_repository())

        with patch("apps.industry_planner.views.planner_service", service):
            response = self.client.post(
                "/api/blueprints/calculate",
                data={
                    "types": [{"typeId": 100, "amount": 3}],
                    "moduleT1ME": 0,
                    "moduleT1TE": 0,
                    "buildT1": True,
                    "copyBPO": False,
                    "produceFuelBlocks": False,
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        assert_json_golden("planner_calculate_recursive_basic", response.json())

    def test_calculate_route_real_service_matches_golden_copy_bpo_dataset(self) -> None:
        service = IndustryPlannerService(repository=build_copy_bpo_repository())

        with patch("apps.industry_planner.views.planner_service", service):
            response = self.client.post(
                "/api/blueprints/calculate",
                data={
                    "types": [{"typeId": 400, "amount": 5}],
                    "moduleT1ME": 0,
                    "moduleT1TE": 0,
                    "buildT1": True,
                    "copyBPO": True,
                    "produceFuelBlocks": False,
                    "manufacturingRoleBonus": 0.99,
                    "manufacturingRigBonus": 0.958,
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        assert_json_golden("planner_calculate_copy_bpo_basic", response.json())

    def test_calculate_route_real_service_matches_golden_reaction_dataset(self) -> None:
        service = IndustryPlannerService(repository=build_reaction_repository())

        with patch("apps.industry_planner.views.planner_service", service):
            response = self.client.post(
                "/api/blueprints/calculate",
                data={
                    "types": [{"typeId": 500, "amount": 3}],
                    "buildT1": True,
                    "copyBPO": False,
                    "produceFuelBlocks": True,
                    "reactionRigBonus": 0.974,
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        assert_json_golden("planner_calculate_reaction_basic", response.json())

    def test_calculate_route_real_service_matches_golden_build_t1_false_ship_gate_dataset(self) -> None:
        service = IndustryPlannerService(repository=build_ship_gate_repository())

        with patch("apps.industry_planner.views.planner_service", service):
            response = self.client.post(
                "/api/blueprints/calculate",
                data={
                    "types": [{"typeId": 900, "amount": 1}],
                    "shipT1ME": 0,
                    "shipT1TE": 0,
                    "shipT2ME": 0,
                    "shipT2TE": 0,
                    "buildT1": False,
                    "copyBPO": False,
                    "produceFuelBlocks": False,
                    "manufacturingRoleBonus": 0.99,
                    "manufacturingRigBonus": 0.958,
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        assert_json_golden("planner_calculate_build_t1_false_ship_gate", response.json())

    def test_calculate_route_maps_legacy_payload_to_service(self) -> None:
        with patch("apps.industry_planner.views.planner_service.get_blueprints_details") as get_blueprints_details_mock:
            get_blueprints_details_mock.return_value = {
                "jobs": [{"blueprintTypeId": 100, "runs": 3}],
                "materials": [{"materialTypeID": 34, "quantity": 120}],
            }

            response = self.client.post(
                "/api/blueprints/calculate",
                data={
                    "types": [{"typeId": 200, "amount": 3}],
                    "shipT1ME": 10,
                    "shipT1TE": 20,
                    "moduleT1ME": 4,
                    "moduleT1TE": 8,
                    "buildT1": True,
                    "copyBPO": False,
                    "produceFuelBlocks": True,
                    "mergeModules": True,
                    "manufacturingRoleBonus": 0.99,
                    "manufacturingRigBonus": 0.958,
                    "reactionRigBonus": 0.974,
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {"jobs": [{"blueprintTypeId": 100, "runs": 3}], "materials": [{"materialTypeID": 34, "quantity": 120}]},
        )
        get_blueprints_details_mock.assert_called_once_with(
            types=[{"typeId": 200, "amount": 3}],
            efficiency={
                "shipT1ME": 10,
                "shipT1TE": 20,
                "shipT2ME": 0,
                "shipT2TE": 0,
                "moduleT1ME": 4,
                "moduleT1TE": 8,
                "moduleT2ME": 0,
                "moduleT2TE": 0,
            },
            build_t1=True,
            copy_bpo=False,
            produce_fuel_blocks=True,
            merge_modules=True,
            manufacturing_role_bonus=0.99,
            manufacturing_rig_bonus=0.958,
            reaction_rig_bonus=0.974,
        )

    def test_calculate_by_id_returns_legacy_missing_amount_error(self) -> None:
        response = self.client.post(
            "/api/blueprints/123/calculate",
            data={},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode("utf-8"), "Chyba: amount missing")

    def test_calculate_route_uses_legacy_defaults_when_flags_missing(self) -> None:
        with patch("apps.industry_planner.views.planner_service.get_blueprints_details") as get_blueprints_details_mock:
            get_blueprints_details_mock.return_value = {"jobs": [], "materials": []}

            response = self.client.post(
                "/api/blueprints/calculate",
                data={"types": [{"typeId": 200, "amount": 1}]},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        get_blueprints_details_mock.assert_called_once_with(
            types=[{"typeId": 200, "amount": 1}],
            efficiency={
                "shipT1ME": 0,
                "shipT1TE": 0,
                "shipT2ME": 0,
                "shipT2TE": 0,
                "moduleT1ME": 0,
                "moduleT1TE": 0,
                "moduleT2ME": 0,
                "moduleT2TE": 0,
            },
            build_t1=True,
            copy_bpo=True,
            produce_fuel_blocks=True,
            merge_modules=False,
            manufacturing_role_bonus=1.0,
            manufacturing_rig_bonus=1.0,
            reaction_rig_bonus=1.0,
        )

    def test_calculate_route_returns_legacy_plaintext_error(self) -> None:
        with patch("apps.industry_planner.views.planner_service.get_blueprints_details") as get_blueprints_details_mock:
            get_blueprints_details_mock.side_effect = RuntimeError("boom")

            response = self.client.post(
                "/api/blueprints/calculate",
                data={"types": [{"typeId": 200, "amount": 1}]},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode("utf-8"), "Chyba: boom")


class PlannerPersistenceTests(TestCase):
    def test_rebuild_project_plan_persists_jobs_and_materials(self) -> None:
        user = get_user_model().objects.create_user(username="planner", password="x")
        project = Project.objects.create(name="Recursive Plan", created_by=user)
        service = IndustryPlannerService(repository=build_recursive_repository())

        result = service.rebuild_project_plan(
            project=project,
            types=[{"typeId": 100, "amount": 3}],
            efficiency={"moduleT1ME": 0, "moduleT1TE": 0},
            build_t1=True,
            copy_bpo=False,
            produce_fuel_blocks=False,
        )

        self.assertEqual(len(result["jobs"]), 2)
        self.assertEqual(PlanJob.objects.filter(project=project).count(), 2)
        self.assertEqual(PlanMaterial.objects.filter(project=project, plan_job__isnull=True).count(), 3)
        self.assertEqual(PlanMaterial.objects.filter(project=project, plan_job__isnull=False).count(), 3)

        final_job = PlanJob.objects.get(project=project, blueprint_type_id=100)
        intermediate_job = PlanJob.objects.get(project=project, blueprint_type_id=300)
        self.assertEqual(final_job.activity_id, 1)
        self.assertEqual(final_job.runs, 3)
        self.assertEqual(intermediate_job.runs, 6)
        self.assertEqual(len({final_job.params_hash, intermediate_job.params_hash}), 1)

        aggregate_intermediate = PlanMaterial.objects.get(project=project, plan_job__isnull=True, material_type_id=201)
        self.assertFalse(aggregate_intermediate.is_input)
        self.assertTrue(aggregate_intermediate.is_intermediate)

        linked_final_material = PlanMaterial.objects.get(project=project, plan_job=final_job, material_type_id=500)
        self.assertEqual(linked_final_material.quantity_total, 29)

    def test_rebuild_project_plan_replaces_previous_rows(self) -> None:
        user = get_user_model().objects.create_user(username="planner2", password="x")
        project = Project.objects.create(name="Replace Plan", created_by=user)
        service = IndustryPlannerService(repository=build_recursive_repository())

        service.rebuild_project_plan(
            project=project,
            types=[{"typeId": 100, "amount": 3}],
            efficiency={"moduleT1ME": 0, "moduleT1TE": 0},
            build_t1=True,
            copy_bpo=False,
            produce_fuel_blocks=False,
        )
        first_job_ids = list(PlanJob.objects.filter(project=project).values_list("id", flat=True))

        service.rebuild_project_plan(
            project=project,
            types=[{"typeId": 100, "amount": 1}],
            efficiency={"moduleT1ME": 0, "moduleT1TE": 0},
            build_t1=True,
            copy_bpo=False,
            produce_fuel_blocks=False,
        )

        second_job_ids = list(PlanJob.objects.filter(project=project).values_list("id", flat=True))
        self.assertEqual(len(second_job_ids), 2)
        self.assertTrue(set(first_job_ids).isdisjoint(second_job_ids))


class PlannerProjectRouteTests(TestCase):
    def test_create_project_persists_targets(self) -> None:
        user = get_user_model().objects.create_user(username="route-planner", password="x")

        response = self.client.post(
            "/api/planner/projects/create",
            data={
                "name": "Route Project",
                "priority": 5,
                "status": "draft",
                "createdByUserId": user.id,
                "notes": "pilot",
                "targets": [
                    {"typeId": 100, "quantity": 3, "isFinalOutput": True},
                    {"typeId": 300, "quantity": 1, "isFinalOutput": False},
                ],
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["name"], "Route Project")
        self.assertEqual(payload["priority"], 5)
        self.assertEqual(len(payload["targets"]), 2)
        self.assertEqual(Project.objects.count(), 1)
        self.assertEqual(ProjectTarget.objects.count(), 2)

    def test_rebuild_project_uses_targets_when_types_missing(self) -> None:
        user = get_user_model().objects.create_user(username="route-planner-2", password="x")
        project = Project.objects.create(name="Project API", created_by=user)
        ProjectTarget.objects.create(project=project, type_id=100, quantity=3, is_final_output=True)

        service = IndustryPlannerService(repository=build_recursive_repository())
        with patch("apps.industry_planner.views.planner_service", service):
            response = self.client.post(
                f"/api/planner/projects/{project.id}/rebuild",
                data={
                    "moduleT1ME": 0,
                    "moduleT1TE": 0,
                    "buildT1": True,
                    "copyBPO": False,
                    "produceFuelBlocks": False,
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["project"]["id"], project.id)
        self.assertEqual(payload["project"]["planSummary"], {"jobCount": 2, "materialCount": 6})
        self.assertEqual(len(payload["project"]["jobs"]), 2)
        self.assertEqual(PlanJob.objects.filter(project=project).count(), 2)
        self.assertEqual(PlanMaterial.objects.filter(project=project).count(), 6)

    def test_update_project_changes_fields_and_targets(self) -> None:
        user = get_user_model().objects.create_user(username="route-planner-4", password="x")
        project = Project.objects.create(name="Before Update", priority=2, status="draft", created_by=user, notes="old")
        ProjectTarget.objects.create(project=project, type_id=100, quantity=3, is_final_output=True)

        response = self.client.post(
            f"/api/planner/projects/{project.id}/update",
            data={
                "name": "After Update",
                "priority": 5,
                "status": "active",
                "notes": "new notes",
                "targets": [{"typeId": 200, "quantity": 7, "isFinalOutput": True}],
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        project.refresh_from_db()
        self.assertEqual(project.name, "After Update")
        self.assertEqual(project.priority, 5)
        self.assertEqual(project.status, "active")
        self.assertEqual(project.notes, "new notes")
        self.assertEqual(list(project.targets.values_list("type_id", flat=True)), [200])

    def test_update_project_rejects_blank_name(self) -> None:
        user = get_user_model().objects.create_user(username="route-planner-5", password="x")
        project = Project.objects.create(name="Keep Me", created_by=user)

        response = self.client.post(
            f"/api/planner/projects/{project.id}/update",
            data={"name": "   "},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "name cannot be blank"})

    def test_get_project_returns_persisted_plan(self) -> None:
        user = get_user_model().objects.create_user(username="route-planner-3", password="x")
        project = Project.objects.create(name="Project Detail", created_by=user)
        service = IndustryPlannerService(repository=build_recursive_repository())
        service.rebuild_project_plan(
            project=project,
            types=[{"typeId": 100, "amount": 3}],
            efficiency={"moduleT1ME": 0, "moduleT1TE": 0},
            build_t1=True,
            copy_bpo=False,
            produce_fuel_blocks=False,
        )

        response = self.client.get(f"/api/planner/projects/{project.id}")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["id"], project.id)
        self.assertEqual(payload["planSummary"], {"jobCount": 2, "materialCount": 6})
        self.assertEqual(len(payload["jobs"]), 2)
        self.assertEqual(len(payload["materials"]), 3)


class PlannerShadowReportTests(SimpleTestCase):
    def test_generate_shadow_planner_report_matches_golden_and_legacy(self) -> None:
        report = generate_shadow_planner_report()

        self.assertEqual(report["planner"]["scenarioCount"], 4)
        self.assertTrue(report["planner"]["allGoldenMatched"])
        self.assertTrue(report["planner"]["allLegacyMatched"])