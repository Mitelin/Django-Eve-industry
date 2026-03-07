from __future__ import annotations

from io import StringIO
from unittest import TestCase
from unittest.mock import MagicMock, patch

from django.core.management import CommandError, call_command
from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase
from django.test.utils import override_settings
from django.utils import timezone

from apps.common.db import DatabaseConfigurationError, is_postgres, require_postgres
from apps.common.locks import (
    AdvisoryLockError,
    AdvisoryLockKey,
    advisory_lock,
    advisory_unlock,
    build_advisory_lock_key,
    build_sync_lock_key,
    build_verify_lock_key,
    try_advisory_lock,
)
from apps.accounts.models import Character
from apps.common.models import CutoverRoleAssignment, CutoverRoleEvent, ReportSnapshot, ScriptSignoff, ScriptSignoffEvent
from apps.corp_sync.models import SyncRun
from apps.industry_planner.models import PlanJob, Project
from apps.workforce.models import WorkEvent, WorkItem


class _FakeCursor:
    def __init__(self, responses: list[tuple[bool]]):
        self.responses = responses
        self.executed: list[tuple[str, list[int]]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params: list[int]) -> None:
        self.executed.append((sql, params))

    def fetchone(self):
        return self.responses.pop(0)


class _FakeConnection:
    def __init__(self, vendor: str, responses: list[tuple[bool]]):
        self.vendor = vendor
        self._cursor = _FakeCursor(responses)

    def cursor(self):
        return self._cursor


class AdvisoryLockTests(TestCase):
    def test_build_advisory_lock_key_is_stable(self) -> None:
        first = build_advisory_lock_key("sync", "jobs", 123)
        second = build_advisory_lock_key("sync", "jobs", 123)
        self.assertEqual(first, second)

    def test_build_advisory_lock_key_changes_with_scope(self) -> None:
        sync_key = build_sync_lock_key("jobs", 123)
        verify_key = build_verify_lock_key("jobs", 123)
        self.assertNotEqual(sync_key, verify_key)

    def test_try_advisory_lock_uses_pg_function(self) -> None:
        connection = _FakeConnection("postgresql", [(True,)])
        key = AdvisoryLockKey(group_id=11, resource_id=22)

        acquired = try_advisory_lock(connection, key)

        self.assertTrue(acquired)
        self.assertEqual(
            connection._cursor.executed,
            [("SELECT pg_try_advisory_lock(%s, %s)", [11, 22])],
        )

    def test_advisory_unlock_uses_pg_function(self) -> None:
        connection = _FakeConnection("postgresql", [(True,)])
        key = AdvisoryLockKey(group_id=33, resource_id=44)

        released = advisory_unlock(connection, key)

        self.assertTrue(released)
        self.assertEqual(
            connection._cursor.executed,
            [("SELECT pg_advisory_unlock(%s, %s)", [33, 44])],
        )

    def test_advisory_lock_context_unlocks_after_use(self) -> None:
        connection = _FakeConnection("postgresql", [(True,), (True,)])
        key = AdvisoryLockKey(group_id=55, resource_id=66)

        with advisory_lock(connection, key) as held_key:
            self.assertEqual(held_key, key)

        self.assertEqual(
            connection._cursor.executed,
            [
                ("SELECT pg_try_advisory_lock(%s, %s)", [55, 66]),
                ("SELECT pg_advisory_unlock(%s, %s)", [55, 66]),
            ],
        )

    def test_try_advisory_lock_rejects_non_postgres(self) -> None:
        connection = _FakeConnection("sqlite", [])

        with self.assertRaises(AdvisoryLockError):
            try_advisory_lock(connection, AdvisoryLockKey(group_id=1, resource_id=2))


class DatabaseHelperTests(TestCase):
    @patch("apps.common.db.get_connection")
    def test_is_postgres_true_for_postgres_vendor(self, get_connection_mock: MagicMock) -> None:
        get_connection_mock.return_value.vendor = "postgresql"

        self.assertTrue(is_postgres())

    @patch("apps.common.db.get_connection")
    def test_require_postgres_raises_for_non_postgres(self, get_connection_mock: MagicMock) -> None:
        get_connection_mock.return_value.vendor = "sqlite"

        with self.assertRaises(DatabaseConfigurationError):
            require_postgres()


class CheckPostgresLocksCommandTests(TestCase):
    @patch("apps.common.management.commands.check_postgres_locks.advisory_lock")
    @patch("apps.common.management.commands.check_postgres_locks.require_postgres")
    def test_command_reports_success(self, require_postgres_mock: MagicMock, advisory_lock_mock: MagicMock) -> None:
        require_postgres_mock.return_value = object()
        advisory_lock_mock.return_value.__enter__.return_value = None
        advisory_lock_mock.return_value.__exit__.return_value = False
        stdout = StringIO()

        call_command("check_postgres_locks", stdout=stdout)

        self.assertIn("Advisory lock OK", stdout.getvalue())

    @patch("apps.common.management.commands.check_postgres_locks.require_postgres")
    def test_command_raises_on_configuration_error(self, require_postgres_mock: MagicMock) -> None:
        require_postgres_mock.side_effect = DatabaseConfigurationError("PostgreSQL is required")

        with self.assertRaisesRegex(CommandError, "PostgreSQL is required"):
            call_command("check_postgres_locks")


class ShadowSummaryTests(DjangoTestCase):
    def test_shadow_summary_report_route_returns_cross_slice_summary(self) -> None:
        user = get_user_model().objects.create_user(username="shadow", password="x")
        Character.objects.create(
            user=user,
            eve_character_id=90000010,
            name="Shadow Main",
            corporation_id=321,
            is_main=True,
        )
        SyncRun.objects.create(kind="jobs", corporation_id=321, status="ok", rows_written=4, finished_at=timezone.now())
        SyncRun.objects.create(kind="assets", corporation_id=321, status="failed", rows_written=0, error_text="esi down")
        project = Project.objects.create(name="Shadow Project", created_by=user)
        plan_job = PlanJob.objects.create(
            project=project,
            activity_id=1,
            blueprint_type_id=100,
            product_type_id=200,
            runs=2,
            expected_duration_s=30,
            level=1,
            is_advanced=False,
            params_hash="shadow-hash",
        )
        WorkItem.objects.create(project=project, plan_job=plan_job, kind="start_job", status="ready", priority_score=10)
        work_item = WorkItem.objects.get(project=project, plan_job=plan_job)
        WorkEvent.objects.create(work_item=work_item, actor=user, event_type="CLAIMED", details={"source": "manual_action"})
        WorkEvent.objects.create(work_item=work_item, actor=user, event_type="DIRECTOR_REQUEUED", details={"source": "recommended_action"})

        response = self.client.get("/api/reports/shadow/summary")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("planner", payload)
        self.assertIn("sync", payload)
        self.assertIn("workforce", payload)
        self.assertEqual(payload["sync"]["corporationCount"], 1)
        self.assertEqual(payload["workforce"]["queue"]["ready"], 1)
        self.assertEqual(payload["workforce"]["recentEventSources"]["recommended"], 1)
        self.assertEqual(payload["workforce"]["recentEventSources"]["manual"], 1)
        self.assertEqual(payload["planner"]["scenarioCount"], 4)
        self.assertGreaterEqual(payload["incidentCount"], 1)
        self.assertTrue(any(item["code"] == "sync_failed" for item in payload["incidents"]))

    def test_shadow_summary_report_command_outputs_json(self) -> None:
        stdout = StringIO()

        call_command("shadow_summary_report", stdout=stdout)

        self.assertIn('"planner"', stdout.getvalue())
        self.assertIn('"sync"', stdout.getvalue())
        self.assertIn('"workforce"', stdout.getvalue())


class CutoverReadinessTests(DjangoTestCase):
    def _seed_cutover_green_baseline(self) -> None:
        CutoverRoleAssignment.objects.bulk_create(
            [
                CutoverRoleAssignment(role_name="cutoverLead", assigned_to="lead"),
                CutoverRoleAssignment(role_name="incidentCommander", assigned_to="ic"),
                CutoverRoleAssignment(role_name="backendOwner", assigned_to="backend"),
                CutoverRoleAssignment(role_name="dataOwner", assigned_to="data"),
                CutoverRoleAssignment(role_name="directorRepresentative", assigned_to="director"),
                CutoverRoleAssignment(role_name="rollbackApprover", assigned_to="rollback"),
            ]
        )
        ScriptSignoff.objects.bulk_create(
            [
                ScriptSignoff(
                    script_name="Blueprints.gs",
                    status=ScriptSignoff.Status.VALIDATED,
                    signed_off_by="director",
                    signed_off_at=timezone.now(),
                ),
                ScriptSignoff(
                    script_name="Corporation.gs",
                    status=ScriptSignoff.Status.VALIDATED,
                    signed_off_by="director",
                    signed_off_at=timezone.now(),
                ),
            ]
        )

    def _create_pilot_cycle(self, *, pilot_user) -> None:
        Character.objects.create(
            user=pilot_user,
            eve_character_id=90000123,
            name="Pilot Main",
            corporation_id=321,
            is_main=True,
        )
        for kind in ["assets", "jobs", "wallet_journal", "wallet_transactions"]:
            SyncRun.objects.create(
                kind=kind,
                corporation_id=321,
                status="ok",
                rows_written=1,
                finished_at=timezone.now(),
            )

        project = Project.objects.create(name="Pilot Project", created_by=pilot_user)
        plan_job = PlanJob.objects.create(
            project=project,
            activity_id=1,
            blueprint_type_id=100,
            product_type_id=200,
            runs=1,
            expected_duration_s=30,
            level=1,
            is_advanced=False,
            params_hash="pilot-cycle-hash",
        )
        work_item = WorkItem.objects.create(
            project=project,
            plan_job=plan_job,
            kind="start_job",
            status="verified",
            assigned_to=pilot_user,
            verified_at=timezone.now(),
            priority_score=10,
        )
        WorkEvent.objects.create(work_item=work_item, actor=pilot_user, event_type="CLAIMED", details={})
        WorkEvent.objects.create(work_item=work_item, actor=pilot_user, event_type="TEMP_DONE", details={})
        WorkEvent.objects.create(work_item=work_item, actor=None, event_type="VERIFIED_OK", details={"source": "system"})

    @override_settings(
        CUTOVER_MODE="assisted",
        CUTOVER_READ_ONLY_ASSIGNMENT=True,
        CUTOVER_COMPATIBILITY_MODE=True,
        CUTOVER_PILOT_USER_IDS=[10, 11],
        CUTOVER_REQUIRED_SCRIPT_SIGNOFFS=["Blueprints.gs", "Corporation.gs"],
        CUTOVER_LEAD="lead",
        CUTOVER_INCIDENT_COMMANDER="ic",
        CUTOVER_BACKEND_OWNER="backend",
        CUTOVER_DATA_OWNER="data",
        CUTOVER_DIRECTOR_REPRESENTATIVE="director",
        CUTOVER_ROLLBACK_APPROVER="rollback",
    )
    def test_cutover_readiness_route_returns_guardrails_and_go_no_go(self) -> None:
        response = self.client.get("/api/reports/cutover/readiness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["mode"], "assisted")
        self.assertTrue(payload["readOnlyAssignment"])
        self.assertEqual(payload["pilotUserIds"], [10, 11])
        self.assertTrue(payload["checklist"]["rolesAssigned"])
        self.assertEqual(payload["roleAssignments"]["assignedCount"], 6)
        self.assertFalse(payload["checklist"]["criticalScriptsSignedOff"])
        self.assertEqual(payload["scriptSignoffs"]["requiredCount"], 2)
        self.assertEqual(payload["scriptSignoffs"]["validatedCount"], 0)
        self.assertFalse(payload["goNoGo"])

    @override_settings(
        CUTOVER_MODE="primary",
        CUTOVER_READ_ONLY_ASSIGNMENT=False,
        CUTOVER_COMPATIBILITY_MODE=False,
        CUTOVER_PILOT_USER_IDS=[10],
        CUTOVER_REQUIRED_SCRIPT_SIGNOFFS=["Blueprints.gs", "Corporation.gs"],
        CUTOVER_LEAD="lead",
        CUTOVER_INCIDENT_COMMANDER="ic",
        CUTOVER_BACKEND_OWNER="backend",
        CUTOVER_DATA_OWNER="data",
        CUTOVER_DIRECTOR_REPRESENTATIVE="director",
        CUTOVER_ROLLBACK_APPROVER="rollback",
    )
    def test_cutover_readiness_allows_primary_when_required_scripts_are_signed_off(self) -> None:
        CutoverRoleAssignment.objects.bulk_create(
            [
                CutoverRoleAssignment(role_name="cutoverLead", assigned_to="lead"),
                CutoverRoleAssignment(role_name="incidentCommander", assigned_to="ic"),
                CutoverRoleAssignment(role_name="backendOwner", assigned_to="backend"),
                CutoverRoleAssignment(role_name="dataOwner", assigned_to="data"),
                CutoverRoleAssignment(role_name="directorRepresentative", assigned_to="director"),
                CutoverRoleAssignment(role_name="rollbackApprover", assigned_to="rollback"),
            ]
        )
        ScriptSignoff.objects.create(
            script_name="Blueprints.gs",
            status=ScriptSignoff.Status.VALIDATED,
            signed_off_by="director",
            signed_off_at=timezone.now(),
        )
        ScriptSignoff.objects.create(
            script_name="Corporation.gs",
            status=ScriptSignoff.Status.VALIDATED,
            signed_off_by="director",
            signed_off_at=timezone.now(),
        )

        response = self.client.get("/api/reports/cutover/readiness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["checklist"]["criticalScriptsSignedOff"])
        self.assertTrue(payload["checklist"]["rolesAssigned"])
        self.assertEqual(payload["scriptSignoffs"]["validatedCount"], 2)
        self.assertTrue(payload["goNoGo"])

    @override_settings(CUTOVER_REQUIRED_SCRIPT_SIGNOFFS=["Blueprints.gs"])
    def test_cutover_script_signoffs_route_returns_blocked_items(self) -> None:
        ScriptSignoff.objects.create(
            script_name="Blueprints.gs",
            status=ScriptSignoff.Status.BLOCKED,
            notes="Legacy menu flow still diverges.",
        )
        ScriptSignoffEvent.objects.create(
            signoff=ScriptSignoff.objects.get(script_name="Blueprints.gs"),
            previous_status=ScriptSignoff.Status.PENDING,
            new_status=ScriptSignoff.Status.BLOCKED,
            changed_by="director",
            notes="Legacy menu flow still diverges.",
        )

        response = self.client.get("/api/reports/cutover/script-signoffs")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["blockedCount"], 1)
        self.assertEqual(payload["items"][0]["scriptName"], "Blueprints.gs")
        self.assertEqual(payload["items"][0]["status"], "blocked")
        self.assertEqual(payload["recentEvents"][0]["newStatus"], "blocked")

    @override_settings(CUTOVER_MODE="shadow")
    def test_cutover_readiness_command_outputs_json(self) -> None:
        stdout = StringIO()

        call_command("cutover_readiness_report", stdout=stdout)

        self.assertIn('"mode"', stdout.getvalue())
        self.assertIn('"checklist"', stdout.getvalue())

    def test_cutover_pilot_readiness_route_allows_pilot_start_before_first_cycle(self) -> None:
        pilot_user = get_user_model().objects.create_user(username="pilot-pre", password="x")

        with self.settings(
            CUTOVER_MODE="assisted",
            CUTOVER_READ_ONLY_ASSIGNMENT=False,
            CUTOVER_COMPATIBILITY_MODE=True,
            CUTOVER_PILOT_USER_IDS=[pilot_user.id],
            CUTOVER_REQUIRED_SCRIPT_SIGNOFFS=["Blueprints.gs", "Corporation.gs"],
            CUTOVER_LEAD="lead",
            CUTOVER_INCIDENT_COMMANDER="ic",
            CUTOVER_BACKEND_OWNER="backend",
            CUTOVER_DATA_OWNER="data",
            CUTOVER_DIRECTOR_REPRESENTATIVE="director",
            CUTOVER_ROLLBACK_APPROVER="rollback",
        ):
            self._seed_cutover_green_baseline()

            response = self.client.get("/api/reports/cutover/pilot-readiness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["pilotStartGoNoGo"])
        self.assertFalse(payload["pilotExpansionGoNoGo"])
        self.assertEqual(payload["pilotStage"], "pre_pilot")
        self.assertEqual(payload["activitySummary"]["claimCount"], 0)
        self.assertIn("Pilot cycle has not yet produced a verified completion.", payload["expansionBlockers"])

    def test_cutover_pilot_readiness_route_turns_green_after_verified_pilot_cycle(self) -> None:
        pilot_user = get_user_model().objects.create_user(username="pilot-green", password="x")

        with self.settings(
            CUTOVER_MODE="assisted",
            CUTOVER_READ_ONLY_ASSIGNMENT=False,
            CUTOVER_COMPATIBILITY_MODE=True,
            CUTOVER_PILOT_USER_IDS=[pilot_user.id],
            CUTOVER_REQUIRED_SCRIPT_SIGNOFFS=["Blueprints.gs", "Corporation.gs"],
            CUTOVER_LEAD="lead",
            CUTOVER_INCIDENT_COMMANDER="ic",
            CUTOVER_BACKEND_OWNER="backend",
            CUTOVER_DATA_OWNER="data",
            CUTOVER_DIRECTOR_REPRESENTATIVE="director",
            CUTOVER_ROLLBACK_APPROVER="rollback",
        ):
            self._seed_cutover_green_baseline()
            self._create_pilot_cycle(pilot_user=pilot_user)

            response = self.client.get("/api/reports/cutover/pilot-readiness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["pilotStartGoNoGo"])
        self.assertTrue(payload["pilotExpansionGoNoGo"])
        self.assertEqual(payload["pilotStage"], "cycle_verified")
        self.assertEqual(payload["activitySummary"]["claimCount"], 1)
        self.assertEqual(payload["activitySummary"]["tempDoneCount"], 1)
        self.assertEqual(payload["activitySummary"]["verifiedOkCount"], 1)
        self.assertEqual(payload["activitySummary"]["verifyMissCount"], 0)

    def test_cutover_pilot_readiness_command_outputs_json(self) -> None:
        stdout = StringIO()

        call_command("cutover_pilot_readiness", stdout=stdout)

        self.assertIn('"pilotStartGoNoGo"', stdout.getvalue())
        self.assertIn('"activitySummary"', stdout.getvalue())

    def test_cutover_preflight_command_outputs_actions_and_deltas(self) -> None:
        ReportSnapshot.objects.create(
            snapshot_date=timezone.localdate(),
            report_name="cutover_readiness",
            incident_count=0,
            go_no_go=False,
            payload={
                "mode": "shadow",
                "blockers": ["Cutover and rollback ownership is incomplete."],
                "roleAssignments": {"assignedCount": 0, "requiredCount": 6},
                "scriptSignoffs": {"validatedCount": 0, "requiredCount": 4},
                "shadow": {"incidentCount": 0},
            },
        )
        stdout = StringIO()

        call_command("cutover_preflight", stdout=stdout)

        self.assertIn('"recommendedActions"', stdout.getvalue())
        self.assertIn('"deltasVsLatestSnapshot"', stdout.getvalue())
        self.assertIn('"Assign missing cutover owners', stdout.getvalue())

    def test_cutover_preflight_route_returns_current_and_actions(self) -> None:
        response = self.client.get("/api/reports/cutover/preflight?trendLimit=3")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("current", payload)
        self.assertIn("recommendedActions", payload)
        self.assertIn("recommendedActionItems", payload)
        self.assertIn("readiness", payload)
        self.assertIn("effectiveGoNoGo", payload)
        self.assertIn("preflightBlockers", payload)
        self.assertIn("changesVsStoredPreflight", payload)
        self.assertIn("workforceProvenance", payload["current"])
        self.assertTrue(any(item["actionType"] == "bootstrapGovernance" for item in payload["recommendedActionItems"]))
        self.assertTrue(any(item["actionType"] == "persistEvidence" for item in payload["recommendedActionItems"]))

    @override_settings(CUTOVER_MODE="assisted", CUTOVER_PILOT_USER_IDS=[])
    def test_cutover_preflight_manual_actions_include_guidance(self) -> None:
        response = self.client.get("/api/reports/cutover/preflight?trendLimit=3")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        manual_item = next(item for item in payload["recommendedActionItems"] if item["code"] == "configure_pilot_users")
        self.assertEqual(manual_item["actionType"], "manual")
        self.assertTrue(manual_item["guidanceTitle"])
        self.assertGreaterEqual(len(manual_item["guidanceSteps"]), 1)
        self.assertEqual(manual_item["targetSetting"], "CUTOVER_PILOT_USER_IDS")

    @patch("apps.common.preflight.generate_cutover_readiness_report")
    def test_cutover_preflight_operational_actions_include_focus_types(
        self,
        readiness_report_mock: MagicMock,
    ) -> None:
        readiness_report_mock.return_value = {
            "mode": "shadow",
            "goNoGo": False,
            "blockers": [
                "Sync posture has stale or failed feeds.",
                "Workforce posture has failed work items or stale project freshness.",
            ],
            "checklist": {
                "plannerParityGreen": True,
                "syncHealthy": False,
                "workforceHealthy": False,
                "criticalScriptsSignedOff": True,
                "compatibilityModeRetained": True,
                "assignmentWritesEnabled": True,
                "rollbackReadOnlyAvailable": True,
                "pilotUsersConfigured": True,
                "pilotUserGuardEnabled": True,
                "rolesAssigned": True,
            },
            "roleAssignments": {
                "assignedCount": 6,
                "requiredCount": 6,
                "unassignedCount": 0,
                "items": [],
            },
            "scriptSignoffs": {
                "validatedCount": 2,
                "requiredCount": 2,
                "pendingCount": 0,
                "blockedCount": 0,
                "items": [],
            },
            "shadow": {
                "incidentCount": 2,
            },
        }

        response = self.client.get("/api/reports/cutover/preflight?trendLimit=3")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        sync_action = next(item for item in payload["recommendedActionItems"] if item["code"] == "resolve_sync_posture")
        workforce_action = next(item for item in payload["recommendedActionItems"] if item["code"] == "clear_workforce_blockers")
        self.assertEqual(sync_action["actionType"], "focusShadowSync")
        self.assertEqual(workforce_action["actionType"], "focusWorkforceBlockers")
        self.assertTrue(sync_action["guidanceSteps"])
        self.assertTrue(workforce_action["guidanceSteps"])

    @patch("apps.common.preflight.generate_cutover_readiness_report")
    def test_cutover_preflight_adds_manual_review_action_for_provenance_warning(
        self,
        readiness_report_mock: MagicMock,
    ) -> None:
        ReportSnapshot.objects.create(
            snapshot_date=timezone.localdate(),
            report_name="cutover_preflight",
            incident_count=0,
            go_no_go=True,
            payload={
                "readiness": {"mode": "assisted", "goNoGo": True, "blockers": []},
                "recommendedActions": [],
                "current": {
                    "assignedRoles": 6,
                    "requiredRoles": 6,
                    "validatedSignoffs": 4,
                    "requiredSignoffs": 4,
                    "blockerCount": 0,
                    "incidentCount": 0,
                    "workforceProvenance": {"total": 3, "recommended": 2, "manual": 1, "system": 0},
                },
            },
        )
        readiness_report_mock.return_value = {
            "mode": "assisted",
            "goNoGo": False,
            "blockers": ["Workforce posture has failed work items or stale project freshness."],
            "checklist": {
                "plannerParityGreen": True,
                "syncHealthy": True,
                "workforceHealthy": False,
                "criticalScriptsSignedOff": True,
                "compatibilityModeRetained": True,
                "assignmentWritesEnabled": True,
                "rollbackReadOnlyAvailable": True,
                "pilotUsersConfigured": True,
                "pilotUserGuardEnabled": True,
                "rolesAssigned": True,
            },
            "roleAssignments": {"assignedCount": 6, "requiredCount": 6, "unassignedCount": 0, "items": []},
            "scriptSignoffs": {
                "validatedCount": 4,
                "requiredCount": 4,
                "pendingCount": 0,
                "blockedCount": 0,
                "items": [],
            },
            "shadow": {
                "incidentCount": 1,
                "planner": {"scenarioCount": 4, "matchedGolden": 4, "matchedLegacy": 4},
                "sync": {"staleCount": 0, "corporationCount": 1},
                "workforce": {
                    "failed": 1,
                    "tempDone": 0,
                    "staleProjectCount": 0,
                    "recentEventSources": {"total": 5, "recommended": 1, "manual": 3, "system": 1},
                },
            },
        }

        response = self.client.get("/api/reports/cutover/preflight?trendLimit=3")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        manual_growth_action = next(
            item for item in payload["recommendedActionItems"] if item["code"] == "review_manual_intervention_growth"
        )
        self.assertEqual(manual_growth_action["actionType"], "manual")
        self.assertTrue(manual_growth_action["guidanceTitle"])
        self.assertGreaterEqual(len(manual_growth_action["guidanceSteps"]), 3)

    def test_cutover_preflight_route_reports_diff_against_latest_stored_preflight(self) -> None:
        ReportSnapshot.objects.create(
            snapshot_date=timezone.localdate(),
            report_name="cutover_preflight",
            incident_count=0,
            go_no_go=True,
            payload={
                "readiness": {
                    "mode": "shadow",
                    "goNoGo": True,
                    "blockers": [],
                },
                "recommendedActions": [],
                "current": {
                    "assignedRoles": 6,
                    "requiredRoles": 6,
                    "validatedSignoffs": 4,
                    "requiredSignoffs": 4,
                    "blockerCount": 0,
                    "incidentCount": 0,
                    "workforceProvenance": {"total": 2, "recommended": 2, "manual": 0, "system": 0},
                },
            },
        )

        response = self.client.get("/api/reports/cutover/preflight?trendLimit=3")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        changes = payload["changesVsStoredPreflight"]
        self.assertTrue(changes["hasStoredBaseline"])
        self.assertTrue(changes["goNoGoChanged"])
        self.assertEqual(changes["previousGoNoGo"], True)
        self.assertEqual(changes["currentGoNoGo"], False)
        self.assertGreaterEqual(len(changes["blockersAdded"]), 1)
        self.assertGreaterEqual(len(changes["actionsAdded"]), 1)
        self.assertTrue(any(item["label"] == "Blocker added" for item in changes["detailRows"]))
        ownership_blocker = next(
            item for item in changes["detailRows"] if item["value"] == "Cutover and rollback ownership is incomplete."
        )
        self.assertEqual(ownership_blocker["actionItem"]["code"], "assign_missing_roles")
        self.assertIn("currentWorkforceProvenance", changes)
        self.assertIn("workforceProvenanceDelta", changes)
        self.assertNotEqual(payload["recommendedActionItems"][0]["actionType"], "persistEvidence")
        self.assertNotIn("payload", payload["latestStoredPreflightSnapshot"])

    @patch("apps.common.preflight.generate_cutover_readiness_report")
    def test_cutover_preflight_route_flags_manual_intervention_growth_against_baseline(
        self,
        readiness_report_mock: MagicMock,
    ) -> None:
        ReportSnapshot.objects.create(
            snapshot_date=timezone.localdate(),
            report_name="cutover_preflight",
            incident_count=0,
            go_no_go=True,
            payload={
                "readiness": {"mode": "assisted", "goNoGo": True, "blockers": []},
                "recommendedActions": [],
                "current": {
                    "assignedRoles": 6,
                    "requiredRoles": 6,
                    "validatedSignoffs": 4,
                    "requiredSignoffs": 4,
                    "blockerCount": 0,
                    "incidentCount": 0,
                    "workforceProvenance": {"total": 3, "recommended": 2, "manual": 1, "system": 0},
                },
            },
        )
        readiness_report_mock.return_value = {
            "mode": "assisted",
            "goNoGo": False,
            "blockers": ["Workforce posture has failed work items or stale project freshness."],
            "checklist": {
                "plannerParityGreen": True,
                "syncHealthy": True,
                "workforceHealthy": False,
                "criticalScriptsSignedOff": True,
                "compatibilityModeRetained": True,
                "assignmentWritesEnabled": True,
                "rollbackReadOnlyAvailable": True,
                "pilotUsersConfigured": True,
                "pilotUserGuardEnabled": True,
                "rolesAssigned": True,
            },
            "roleAssignments": {"assignedCount": 6, "requiredCount": 6, "unassignedCount": 0, "items": []},
            "scriptSignoffs": {
                "validatedCount": 4,
                "requiredCount": 4,
                "pendingCount": 0,
                "blockedCount": 0,
                "items": [],
            },
            "shadow": {
                "incidentCount": 1,
                "planner": {"scenarioCount": 4, "matchedGolden": 4, "matchedLegacy": 4},
                "sync": {"staleCount": 0, "corporationCount": 1},
                "workforce": {
                    "failed": 1,
                    "tempDone": 0,
                    "staleProjectCount": 0,
                    "recentEventSources": {"total": 5, "recommended": 1, "manual": 3, "system": 1},
                },
            },
        }

        response = self.client.get("/api/reports/cutover/preflight?trendLimit=3")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        changes = payload["changesVsStoredPreflight"]
        self.assertEqual(changes["workforceProvenanceDelta"]["manual"], 2)
        self.assertEqual(changes["workforceProvenanceDelta"]["recommended"], -1)
        self.assertTrue(changes["workforceProvenanceWarning"])
        warning_row = next(item for item in changes["detailRows"] if item["label"] == "Workforce provenance warning")
        self.assertEqual(warning_row["actionItem"]["code"], "review_manual_intervention_growth")
        self.assertTrue(any(item["label"] == "Manual interventions increased" for item in changes["detailRows"]))
        self.assertTrue(any(item["label"] == "Workforce provenance warning" for item in changes["detailRows"]))

    def test_cutover_preflight_route_persist_stores_preflight_snapshot(self) -> None:
        response = self.client.get("/api/reports/cutover/preflight?trendLimit=3&persist=1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(ReportSnapshot.objects.filter(report_name="cutover_preflight").count(), 1)
        self.assertEqual(payload["latestStoredPreflightSnapshot"]["reportName"], "cutover_preflight")
        self.assertIn(
            {"reportName": "cutover_preflight", "snapshotDate": timezone.localdate().isoformat()},
            payload["storedSnapshots"],
        )
        self.assertNotIn("payload", payload["latestStoredPreflightSnapshot"])
        stored_payload = ReportSnapshot.objects.get(report_name="cutover_preflight").payload
        self.assertIn("workforceProvenance", stored_payload["current"])

    @patch("apps.common.preflight.generate_cutover_readiness_report")
    def test_cutover_preflight_effective_go_no_go_blocks_manual_dominance_in_assisted_mode(
        self,
        readiness_report_mock: MagicMock,
    ) -> None:
        ReportSnapshot.objects.create(
            snapshot_date=timezone.localdate(),
            report_name="cutover_preflight",
            incident_count=0,
            go_no_go=True,
            payload={
                "readiness": {"mode": "assisted", "goNoGo": True, "blockers": []},
                "recommendedActions": [],
                "current": {
                    "assignedRoles": 6,
                    "requiredRoles": 6,
                    "validatedSignoffs": 4,
                    "requiredSignoffs": 4,
                    "blockerCount": 0,
                    "incidentCount": 0,
                    "workforceProvenance": {"total": 3, "recommended": 2, "manual": 1, "system": 0},
                },
            },
        )
        readiness_report_mock.return_value = {
            "mode": "assisted",
            "goNoGo": True,
            "blockers": [],
            "checklist": {
                "plannerParityGreen": True,
                "syncHealthy": True,
                "workforceHealthy": True,
                "criticalScriptsSignedOff": True,
                "compatibilityModeRetained": True,
                "assignmentWritesEnabled": True,
                "rollbackReadOnlyAvailable": True,
                "pilotUsersConfigured": True,
                "pilotUserGuardEnabled": True,
                "rolesAssigned": True,
            },
            "roleAssignments": {"assignedCount": 6, "requiredCount": 6, "unassignedCount": 0, "items": []},
            "scriptSignoffs": {
                "validatedCount": 4,
                "requiredCount": 4,
                "pendingCount": 0,
                "blockedCount": 0,
                "items": [],
            },
            "shadow": {
                "incidentCount": 0,
                "planner": {"scenarioCount": 4, "matchedGolden": 4, "matchedLegacy": 4},
                "sync": {"staleCount": 0, "corporationCount": 1},
                "workforce": {
                    "failed": 0,
                    "tempDone": 0,
                    "staleProjectCount": 0,
                    "recentEventSources": {"total": 5, "recommended": 1, "manual": 3, "system": 1},
                },
            },
        }

        response = self.client.get("/api/reports/cutover/preflight?trendLimit=3")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["readiness"]["goNoGo"])
        self.assertFalse(payload["effectiveGoNoGo"])
        self.assertIn(
            "Manual workforce interventions are rising above recommendation-driven handling.",
            payload["preflightBlockers"],
        )
        self.assertEqual(payload["current"]["preflightBlockerCount"], 1)

    @override_settings(CUTOVER_REQUIRED_SCRIPT_SIGNOFFS=["Blueprints.gs", "Corporation.gs"])
    def test_update_cutover_script_signoff_route_updates_summary_and_readiness(self) -> None:
        response = self.client.post(
            "/api/reports/cutover/script-signoffs/update",
            data='{"scriptName": "Blueprints.gs", "status": "validated", "signedOffBy": "director", "notes": "Pilot route confirmed."}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["scriptSignoffs"]["validatedCount"], 1)
        self.assertEqual(payload["scriptSignoffs"]["items"][0]["scriptName"], "Blueprints.gs")
        self.assertEqual(payload["scriptSignoffs"]["items"][0]["status"], "validated")
        self.assertEqual(payload["readiness"]["scriptSignoffs"]["validatedCount"], 1)

    @override_settings(CUTOVER_REQUIRED_SCRIPT_SIGNOFFS=["Blueprints.gs", "Corporation.gs"])
    def test_sync_missing_cutover_script_signoffs_route_creates_required_rows(self) -> None:
        response = self.client.post(
            "/api/reports/cutover/script-signoffs/sync-missing",
            data='{"changedBy": "director", "notes": "Bootstrap missing signoffs."}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload["syncedScripts"]), {"Blueprints.gs", "Corporation.gs"})
        self.assertEqual(ScriptSignoff.objects.count(), 2)
        self.assertEqual(ScriptSignoffEvent.objects.count(), 2)
        self.assertEqual(payload["scriptSignoffs"]["requiredCount"], 2)

    @override_settings(CUTOVER_REQUIRED_SCRIPT_SIGNOFFS=["Blueprints.gs"])
    def test_cutover_preflight_route_prefers_bulk_script_sync_action_when_rows_are_missing(self) -> None:
        response = self.client.get("/api/reports/cutover/preflight?trendLimit=3")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        script_action = next(item for item in payload["recommendedActionItems"] if item["code"] == "validate_critical_scripts")
        self.assertEqual(script_action["actionType"], "syncMissingScriptSignoffs")

    def test_update_cutover_role_owner_route_updates_summary_and_readiness(self) -> None:
        response = self.client.post(
            "/api/reports/cutover/roles/update",
            data='{"roleName": "cutoverLead", "assignedTo": "Lead One", "changedBy": "director", "notes": "Primary owner for pilot."}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["roleAssignments"]["assignedCount"], 1)
        self.assertEqual(payload["roleAssignments"]["items"][0]["roleName"], "cutoverLead")
        self.assertEqual(payload["roleAssignments"]["items"][0]["assignedTo"], "Lead One")
        self.assertEqual(payload["readiness"]["roleAssignments"]["assignedCount"], 1)

    @override_settings(CUTOVER_LEAD="lead", CUTOVER_BACKEND_OWNER="backend")
    def test_sync_missing_cutover_roles_route_applies_env_defaults(self) -> None:
        CutoverRoleAssignment.objects.create(role_name="cutoverLead", assigned_to="")
        CutoverRoleAssignment.objects.create(role_name="backendOwner", assigned_to="")

        response = self.client.post(
            "/api/reports/cutover/roles/sync-missing",
            data='{"changedBy": "director", "notes": "Bootstrap missing owners."}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload["syncedRoles"]), {"cutoverLead", "backendOwner"})
        self.assertEqual(CutoverRoleAssignment.objects.get(role_name="cutoverLead").assigned_to, "lead")
        self.assertEqual(CutoverRoleAssignment.objects.get(role_name="backendOwner").assigned_to, "backend")
        self.assertEqual(payload["roleAssignments"]["assignedCount"], 2)

    @override_settings(CUTOVER_LEAD="lead")
    def test_cutover_preflight_route_prefers_bulk_role_sync_action_when_env_defaults_exist(self) -> None:
        response = self.client.get("/api/reports/cutover/preflight?trendLimit=3")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        role_action = next(item for item in payload["recommendedActionItems"] if item["code"] == "assign_missing_roles")
        self.assertEqual(role_action["actionType"], "syncMissingRoleOwners")

    def test_update_cutover_role_owner_route_rejects_unknown_role(self) -> None:
        response = self.client.post(
            "/api/reports/cutover/roles/update",
            data='{"roleName": "unknownRole", "assignedTo": "Lead One"}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("roleName must be one of", response.json()["error"])


class ScriptSignoffCommandTests(DjangoTestCase):
    @override_settings(CUTOVER_REQUIRED_SCRIPT_SIGNOFFS=["Blueprints.gs", "Corporation.gs"])
    def test_sync_script_signoffs_command_creates_required_rows(self) -> None:
        stdout = StringIO()

        call_command("sync_script_signoffs", stdout=stdout)

        self.assertEqual(ScriptSignoff.objects.count(), 2)
        self.assertIn("Blueprints.gs", stdout.getvalue())
        self.assertIn("Corporation.gs", stdout.getvalue())

    def test_set_script_signoff_command_marks_validated(self) -> None:
        stdout = StringIO()

        call_command(
            "set_script_signoff",
            "Blueprints.gs",
            "validated",
            "--by",
            "director",
            "--notes",
            "Pilot route parity confirmed.",
            stdout=stdout,
        )

        signoff = ScriptSignoff.objects.get(script_name="Blueprints.gs")
        event = ScriptSignoffEvent.objects.get(signoff=signoff)
        self.assertEqual(signoff.status, ScriptSignoff.Status.VALIDATED)
        self.assertEqual(signoff.signed_off_by, "director")
        self.assertEqual(signoff.notes, "Pilot route parity confirmed.")
        self.assertIsNotNone(signoff.signed_off_at)
        self.assertEqual(event.previous_status, ScriptSignoff.Status.PENDING)
        self.assertEqual(event.new_status, ScriptSignoff.Status.VALIDATED)
        self.assertIn("status=validated", stdout.getvalue())

    def test_set_script_signoff_command_clears_timestamp_for_blocked_status(self) -> None:
        signoff = ScriptSignoff.objects.create(
            script_name="Blueprints.gs",
            status=ScriptSignoff.Status.VALIDATED,
            signed_off_by="director",
            signed_off_at=timezone.now(),
        )
        self.assertIsNotNone(signoff.signed_off_at)

        call_command("set_script_signoff", "Blueprints.gs", "blocked", "--notes", "Menu mismatch")

        signoff.refresh_from_db()
        event = ScriptSignoffEvent.objects.filter(signoff=signoff).order_by("-effective_at", "-id").first()
        self.assertEqual(signoff.status, ScriptSignoff.Status.BLOCKED)
        self.assertEqual(signoff.notes, "Menu mismatch")
        self.assertIsNone(signoff.signed_off_at)
        self.assertEqual(event.previous_status, ScriptSignoff.Status.VALIDATED)
        self.assertEqual(event.new_status, ScriptSignoff.Status.BLOCKED)


class CutoverRoleCommandTests(DjangoTestCase):
    @override_settings(
        CUTOVER_LEAD="lead",
        CUTOVER_INCIDENT_COMMANDER="ic",
        CUTOVER_BACKEND_OWNER="backend",
        CUTOVER_DATA_OWNER="data",
        CUTOVER_DIRECTOR_REPRESENTATIVE="director",
        CUTOVER_ROLLBACK_APPROVER="rollback",
    )
    def test_sync_cutover_roles_command_creates_required_rows(self) -> None:
        stdout = StringIO()

        call_command("sync_cutover_roles", stdout=stdout)

        self.assertEqual(CutoverRoleAssignment.objects.count(), 6)
        self.assertIn("cutoverLead", stdout.getvalue())
        self.assertEqual(CutoverRoleAssignment.objects.get(role_name="cutoverLead").assigned_to, "lead")

    def test_set_cutover_role_owner_command_records_event(self) -> None:
        stdout = StringIO()

        call_command(
            "set_cutover_role_owner",
            "cutoverLead",
            "alice",
            "--by",
            "director",
            "--notes",
            "Pilot owner assigned.",
            stdout=stdout,
        )

        assignment = CutoverRoleAssignment.objects.get(role_name="cutoverLead")
        event = CutoverRoleEvent.objects.get(assignment=assignment)
        self.assertEqual(assignment.assigned_to, "alice")
        self.assertEqual(event.previous_assigned_to, "")
        self.assertEqual(event.new_assigned_to, "alice")
        self.assertEqual(event.changed_by, "director")
        self.assertIn("owner=alice", stdout.getvalue())


class ReportSnapshotHistoryTests(DjangoTestCase):
    def test_persist_report_snapshots_command_stores_daily_rows(self) -> None:
        stdout = StringIO()

        call_command("persist_report_snapshots", stdout=stdout)

        self.assertEqual(ReportSnapshot.objects.count(), 4)
        self.assertIn("shadow_summary", stdout.getvalue())
        self.assertIn("cutover_readiness", stdout.getvalue())
        self.assertIn("cutover_pilot_readiness", stdout.getvalue())
        self.assertIn("cutover_preflight", stdout.getvalue())

    def test_report_history_route_returns_recent_snapshots(self) -> None:
        call_command("persist_report_snapshots")

        response = self.client.get("/api/reports/history?limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["snapshots"]), 4)
        self.assertEqual(
            {item["reportName"] for item in payload["snapshots"]},
            {"shadow_summary", "cutover_readiness", "cutover_pilot_readiness", "cutover_preflight"},
        )

    def test_persist_report_history_route_stores_all_snapshot_types(self) -> None:
        response = self.client.post("/api/reports/history/persist")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(ReportSnapshot.objects.count(), 4)
        self.assertEqual(
            {item["reportName"] for item in payload["storedSnapshots"]},
            {"shadow_summary", "cutover_readiness", "cutover_pilot_readiness", "cutover_preflight"},
        )

    def test_report_history_route_can_filter_by_report_name(self) -> None:
        call_command("persist_report_snapshots")

        response = self.client.get("/api/reports/history?reportName=cutover_readiness&limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["snapshots"]), 1)
        self.assertEqual(payload["snapshots"][0]["reportName"], "cutover_readiness")

    def test_report_history_route_can_filter_preflight_snapshot(self) -> None:
        call_command("persist_report_snapshots")

        response = self.client.get("/api/reports/history?reportName=cutover_preflight&limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["snapshots"]), 1)
        self.assertEqual(payload["snapshots"][0]["reportName"], "cutover_preflight")

    def test_report_history_route_can_filter_pilot_snapshot(self) -> None:
        call_command("persist_report_snapshots")

        response = self.client.get("/api/reports/history?reportName=cutover_pilot_readiness&limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["snapshots"]), 1)
        self.assertEqual(payload["snapshots"][0]["reportName"], "cutover_pilot_readiness")

    def test_cutover_trend_route_returns_derived_counts(self) -> None:
        ReportSnapshot.objects.create(
            snapshot_date=timezone.localdate(),
            report_name="cutover_readiness",
            incident_count=2,
            go_no_go=False,
            payload={
                "mode": "assisted",
                "blockers": ["ownership incomplete", "signoffs pending"],
                "roleAssignments": {"assignedCount": 3, "requiredCount": 6},
                "scriptSignoffs": {"validatedCount": 1, "requiredCount": 4},
            },
        )

        response = self.client.get("/api/reports/cutover/trend?limit=5")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["trend"]), 1)
        self.assertEqual(payload["trend"][0]["assignedRoles"], 3)
        self.assertEqual(payload["trend"][0]["validatedSignoffs"], 1)
        self.assertEqual(payload["trend"][0]["blockerCount"], 2)

    def test_cutover_pilot_trend_route_returns_stage_and_cycle_counts(self) -> None:
        ReportSnapshot.objects.create(
            snapshot_date=timezone.localdate(),
            report_name="cutover_pilot_readiness",
            incident_count=2,
            go_no_go=False,
            payload={
                "pilotStage": "awaiting_verification",
                "pilotStartGoNoGo": True,
                "pilotExpansionGoNoGo": False,
                "pilotUserIds": [10, 11],
                "activitySummary": {
                    "claimCount": 1,
                    "tempDoneCount": 1,
                    "verifiedOkCount": 0,
                    "verifyMissCount": 1,
                },
                "expansionBlockers": ["Pilot cycle recorded verification misses; resolve them before expanding rollout."],
            },
        )

        response = self.client.get("/api/reports/cutover/pilot-trend?limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["trend"]), 1)
        self.assertEqual(payload["trend"][0]["pilotStage"], "awaiting_verification")
        self.assertTrue(payload["trend"][0]["pilotStartGoNoGo"])
        self.assertFalse(payload["trend"][0]["pilotExpansionGoNoGo"])
        self.assertEqual(payload["trend"][0]["pilotUserCount"], 2)
        self.assertEqual(payload["trend"][0]["claimCount"], 1)
        self.assertEqual(payload["trend"][0]["verifyMissCount"], 1)
