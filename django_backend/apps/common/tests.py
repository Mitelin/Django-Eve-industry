from __future__ import annotations

from datetime import timedelta
from io import StringIO
import json
import os
import tempfile
from unittest import TestCase
from unittest.mock import MagicMock, patch
import zipfile

from django.core.management import CommandError, call_command
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
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
from apps.common.models import (
    CutoverRoleAssignment,
    CutoverRoleEvent,
    ReportSnapshot,
    RollbackEvidence,
    RollbackEvidenceEvent,
    SdeImportRun,
    SdeImportState,
    ScriptSignoff,
    ScriptSignoffEvent,
)
from apps.common.rollback import update_rollback_evidence
from apps.common.sde_import import import_sde_archive
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


class RuntimeVersionTests(DjangoTestCase):
    @patch("apps.common.views._read_runtime_version")
    def test_runtime_version_matches_legacy_response_shape(self, read_version_mock: MagicMock) -> None:
        read_version_mock.return_value = {
            "source": "git",
            "commit": "abc123def456",
            "shortCommit": "abc123d",
            "branch": "main",
            "dirty": True,
        }

        response = self.client.get("/api/version")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["commit"], "abc123def456")
        self.assertEqual(payload["shortCommit"], "abc123d")
        self.assertEqual(payload["branch"], "main")
        self.assertTrue(payload["dirty"])
        self.assertFalse(payload["schedulerEnabled"])
        self.assertFalse(payload["schedulerRunning"])
        self.assertEqual(
            payload["activityScheduler"],
            {
                "running": False,
                "lastStartedAt": None,
                "lastFinishedAt": None,
                "lastSuccessAt": None,
                "lastResultCount": None,
                "lastError": None,
            },
        )
        self.assertIsNone(payload["nextActivityRunAt"])
        self.assertIn("processStartedAt", payload)


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
        WorkEvent.objects.create(work_item=work_item, actor=pilot_user, event_type="CLAIMED", details={"assignedUserId": pilot_user.id})
        WorkEvent.objects.create(work_item=work_item, actor=pilot_user, event_type="TEMP_DONE", details={"assignedUserId": pilot_user.id})
        WorkEvent.objects.create(work_item=work_item, actor=None, event_type="VERIFIED_OK", details={"source": "system", "assignedUserId": pilot_user.id})

    def _create_pilot_issue_state(self, *, pilot_user) -> None:
        Character.objects.create(
            user=pilot_user,
            eve_character_id=90000124,
            name="Pilot Issue",
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

        project = Project.objects.create(name="Pilot Incident Project", created_by=pilot_user)
        stale_plan_job = PlanJob.objects.create(
            project=project,
            activity_id=1,
            blueprint_type_id=101,
            product_type_id=201,
            runs=1,
            expected_duration_s=30,
            level=1,
            is_advanced=False,
            params_hash="pilot-incident-stale",
        )
        escalated_plan_job = PlanJob.objects.create(
            project=project,
            activity_id=1,
            blueprint_type_id=102,
            product_type_id=202,
            runs=1,
            expected_duration_s=30,
            level=1,
            is_advanced=False,
            params_hash="pilot-incident-escalated",
        )
        stale_item = WorkItem.objects.create(
            project=project,
            plan_job=stale_plan_job,
            kind="start_job",
            status="temp_done",
            assigned_to=pilot_user,
            priority_score=10,
        )
        WorkItem.objects.filter(pk=stale_item.pk).update(updated_at=timezone.now() - timedelta(minutes=45))
        escalated_item = WorkItem.objects.create(
            project=project,
            plan_job=escalated_plan_job,
            kind="start_job",
            status="failed",
            assigned_to=pilot_user,
            priority_score=10,
        )
        WorkEvent.objects.create(
            work_item=escalated_item,
            actor=None,
            event_type="VERIFY_MISS",
            details={"source": "system", "assignedUserId": pilot_user.id},
        )
        WorkEvent.objects.create(
            work_item=escalated_item,
            actor=None,
            event_type="ESCALATED",
            details={"reason": "retry_cap_reached", "source": "system", "assignedUserId": pilot_user.id},
        )
        WorkEvent.objects.create(
            work_item=escalated_item,
            actor=None,
            event_type="DIRECTOR_REQUEUED",
            details={"reason": "manual cleanup", "source": "manual_action", "assignedUserId": pilot_user.id},
        )

    def _create_pilot_policy_breach_state(self, *, pilot_user) -> None:
        Character.objects.create(
            user=pilot_user,
            eve_character_id=90000125,
            name="Pilot Policy",
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

        project = Project.objects.create(name="Pilot Policy Project", created_by=pilot_user)
        plan_job = PlanJob.objects.create(
            project=project,
            activity_id=1,
            blueprint_type_id=103,
            product_type_id=203,
            runs=1,
            expected_duration_s=30,
            level=1,
            is_advanced=False,
            params_hash="pilot-policy-hash",
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
        WorkEvent.objects.create(work_item=work_item, actor=pilot_user, event_type="CLAIMED", details={"assignedUserId": pilot_user.id})
        WorkEvent.objects.create(work_item=work_item, actor=pilot_user, event_type="TEMP_DONE", details={"assignedUserId": pilot_user.id})
        WorkEvent.objects.create(work_item=work_item, actor=None, event_type="VERIFY_MISS", details={"source": "system", "assignedUserId": pilot_user.id})
        WorkEvent.objects.create(work_item=work_item, actor=None, event_type="VERIFIED_OK", details={"source": "system", "assignedUserId": pilot_user.id})
        WorkEvent.objects.create(
            work_item=work_item,
            actor=None,
            event_type="DIRECTOR_REQUEUED",
            details={"reason": "manual cleanup", "source": "manual_action", "assignedUserId": pilot_user.id},
        )

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

    def test_cutover_readiness_blocks_when_rollback_evidence_enforced_and_missing(self) -> None:
        with self.settings(
            CUTOVER_MODE="assisted",
            CUTOVER_READ_ONLY_ASSIGNMENT=False,
            CUTOVER_COMPATIBILITY_MODE=True,
            CUTOVER_PILOT_USER_IDS=[10],
            CUTOVER_REQUIRED_SCRIPT_SIGNOFFS=["Blueprints.gs", "Corporation.gs"],
            CUTOVER_LEAD="lead",
            CUTOVER_INCIDENT_COMMANDER="ic",
            CUTOVER_BACKEND_OWNER="backend",
            CUTOVER_DATA_OWNER="data",
            CUTOVER_DIRECTOR_REPRESENTATIVE="director",
            CUTOVER_ROLLBACK_APPROVER="rollback",
            CUTOVER_ENFORCE_ROLLBACK_EVIDENCE=True,
            CUTOVER_RUNBOOK_REVIEWED_AT="",
            CUTOVER_ROLLBACK_TESTED_AT="",
        ):
            self._seed_cutover_green_baseline()
            response = self.client.get("/api/reports/cutover/readiness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["checklist"]["rollbackEvidenceGateSatisfied"])
        self.assertFalse(payload["rollbackSummary"]["status"]["gateSatisfied"])
        self.assertIn("Rollback runbook review or rollback drill evidence is missing or stale.", payload["blockers"])
        self.assertFalse(payload["goNoGo"])

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

    def test_update_cutover_rollback_evidence_route_updates_summary_and_readiness(self) -> None:
        with self.settings(
            CUTOVER_MODE="assisted",
            CUTOVER_PILOT_USER_IDS=[10],
            CUTOVER_REQUIRED_SCRIPT_SIGNOFFS=["Blueprints.gs", "Corporation.gs"],
            CUTOVER_LEAD="lead",
            CUTOVER_INCIDENT_COMMANDER="ic",
            CUTOVER_BACKEND_OWNER="backend",
            CUTOVER_DATA_OWNER="data",
            CUTOVER_DIRECTOR_REPRESENTATIVE="director",
            CUTOVER_ROLLBACK_APPROVER="rollback",
            CUTOVER_ENFORCE_ROLLBACK_EVIDENCE=True,
        ):
            self._seed_cutover_green_baseline()
            response = self.client.post(
                "/api/reports/cutover/rollback-evidence/update",
                data='{"evidenceType": "runbook_review", "evidenceDate": "2026-03-07", "changedBy": "director", "notes": "Reviewed assisted rollback steps."}',
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["rollbackSummary"]["items"][0]["evidenceType"], "runbook_review")
        self.assertEqual(payload["rollbackSummary"]["items"][0]["evidenceDate"], "2026-03-07")
        self.assertEqual(payload["rollbackSummary"]["items"][0]["recordedBy"], "director")
        self.assertEqual(RollbackEvidence.objects.get(evidence_type="runbook_review").recorded_by, "director")
        self.assertEqual(RollbackEvidenceEvent.objects.count(), 1)
        self.assertFalse(payload["readiness"]["rollbackSummary"]["status"]["gateSatisfied"])

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
        self.assertIn("recommendedActionItems", payload)
        self.assertIn("recommendedActions", payload)

    def test_cutover_pilot_readiness_route_blocks_start_when_rollback_evidence_is_enforced_and_missing(self) -> None:
        pilot_user = get_user_model().objects.create_user(username="pilot-rollback", password="x")

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
            CUTOVER_ENFORCE_ROLLBACK_EVIDENCE=True,
            CUTOVER_RUNBOOK_REVIEWED_AT="",
            CUTOVER_ROLLBACK_TESTED_AT="",
        ):
            self._seed_cutover_green_baseline()
            response = self.client.get("/api/reports/cutover/pilot-readiness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["pilotStartGoNoGo"])
        self.assertIn("Rollback runbook review or rollback drill evidence is missing or stale.", payload["startBlockers"])
        rollback_action = next(item for item in payload["recommendedActionItems"] if item["code"] == "refresh_rollback_evidence")
        self.assertEqual(rollback_action["actionType"], "focusRollbackEvidence")

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
        self.assertEqual(payload["operationalSummary"]["verifyMissRatePercent"], 0.0)
        self.assertEqual(payload["operationalSummary"]["escalatedCount"], 0)
        self.assertEqual(payload["operationalSummary"]["tempDonePastSlaCount"], 0)
        self.assertTrue(payload["policySummary"]["status"]["withinPolicy"])

    def test_cutover_pilot_readiness_route_blocks_expansion_for_sla_breach_and_escalation(self) -> None:
        pilot_user = get_user_model().objects.create_user(username="pilot-incident", password="x")

        with self.settings(
            CUTOVER_MODE="assisted",
            CUTOVER_READ_ONLY_ASSIGNMENT=False,
            CUTOVER_COMPATIBILITY_MODE=True,
            CUTOVER_PILOT_USER_IDS=[pilot_user.id],
            CUTOVER_PILOT_VERIFICATION_SLA_MINUTES=30,
            CUTOVER_REQUIRED_SCRIPT_SIGNOFFS=["Blueprints.gs", "Corporation.gs"],
            CUTOVER_LEAD="lead",
            CUTOVER_INCIDENT_COMMANDER="ic",
            CUTOVER_BACKEND_OWNER="backend",
            CUTOVER_DATA_OWNER="data",
            CUTOVER_DIRECTOR_REPRESENTATIVE="director",
            CUTOVER_ROLLBACK_APPROVER="rollback",
        ):
            self._seed_cutover_green_baseline()
            self._create_pilot_issue_state(pilot_user=pilot_user)

            response = self.client.get("/api/reports/cutover/pilot-readiness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["pilotStartGoNoGo"])
        self.assertFalse(payload["pilotExpansionGoNoGo"])
        self.assertEqual(payload["operationalSummary"]["verifyMissCount"], 1)
        self.assertEqual(payload["operationalSummary"]["escalatedCount"], 1)
        self.assertEqual(payload["operationalSummary"]["tempDonePastSlaCount"], 1)
        self.assertEqual(payload["operationalSummary"]["failedOpenCount"], 1)
        self.assertFalse(payload["checklist"]["pilotVerificationSlaHealthy"])
        self.assertFalse(payload["checklist"]["pilotEscalationFree"])
        self.assertIn("Pilot already has TEMP_DONE work beyond the verification SLA window.", payload["startBlockers"])
        self.assertIn("Pilot recorded escalated work items; clear them before expanding rollout.", payload["expansionBlockers"])

    def test_cutover_pilot_readiness_route_blocks_expansion_when_policy_thresholds_are_exceeded(self) -> None:
        pilot_user = get_user_model().objects.create_user(username="pilot-policy", password="x")

        with self.settings(
            CUTOVER_MODE="assisted",
            CUTOVER_READ_ONLY_ASSIGNMENT=False,
            CUTOVER_COMPATIBILITY_MODE=True,
            CUTOVER_PILOT_USER_IDS=[pilot_user.id],
            CUTOVER_PILOT_MAX_VERIFY_MISS_RATE_PERCENT=0,
            CUTOVER_PILOT_MAX_ESCALATED_COUNT=0,
            CUTOVER_PILOT_MAX_MANUAL_INTERVENTION_COUNT=0,
            CUTOVER_REQUIRED_SCRIPT_SIGNOFFS=["Blueprints.gs", "Corporation.gs"],
            CUTOVER_LEAD="lead",
            CUTOVER_INCIDENT_COMMANDER="ic",
            CUTOVER_BACKEND_OWNER="backend",
            CUTOVER_DATA_OWNER="data",
            CUTOVER_DIRECTOR_REPRESENTATIVE="director",
            CUTOVER_ROLLBACK_APPROVER="rollback",
        ):
            self._seed_cutover_green_baseline()
            self._create_pilot_policy_breach_state(pilot_user=pilot_user)

            response = self.client.get("/api/reports/cutover/pilot-readiness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["pilotStartGoNoGo"])
        self.assertFalse(payload["pilotExpansionGoNoGo"])
        self.assertEqual(payload["pilotStage"], "cycle_verified_with_retries")
        self.assertEqual(payload["operationalSummary"]["verifyMissRatePercent"], 50.0)
        self.assertEqual(payload["operationalSummary"]["manualInterventionCount"], 1)
        self.assertFalse(payload["policySummary"]["status"]["verifyMissRateWithinPolicy"])
        self.assertFalse(payload["policySummary"]["status"]["manualInterventionCountWithinPolicy"])
        self.assertFalse(payload["policySummary"]["status"]["withinPolicy"])
        self.assertIn(
            "Pilot verify-miss rate exceeds policy threshold; hold rollout expansion until retry pressure is back within policy.",
            payload["expansionBlockers"],
        )
        self.assertIn(
            "Pilot manual interventions exceed policy threshold; hold rollout expansion until operator intervention pressure is back within policy.",
            payload["expansionBlockers"],
        )
        retry_action = next(item for item in payload["recommendedActionItems"] if item["code"] == "reduce_pilot_retry_pressure")
        manual_action = next(
            item for item in payload["recommendedActionItems"] if item["code"] == "stabilize_pilot_manual_interventions"
        )
        self.assertEqual(retry_action["actionType"], "focusWorkforceBlockers")
        self.assertEqual(manual_action["actionType"], "focusWorkforceBlockers")
        self.assertIn(retry_action["label"], payload["recommendedActions"])
        self.assertIn(manual_action["label"], payload["recommendedActions"])

    def test_cutover_pilot_readiness_command_outputs_json(self) -> None:
        stdout = StringIO()

        call_command("cutover_pilot_readiness", stdout=stdout)

        self.assertIn('"pilotStartGoNoGo"', stdout.getvalue())
        self.assertIn('"activitySummary"', stdout.getvalue())

    def test_cutover_phase9_exit_report_blocks_primary_when_exit_criteria_are_not_met(self) -> None:
        pilot_user = get_user_model().objects.create_user(username="pilot-exit-hold", password="x")

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
            CUTOVER_ENFORCE_ROLLBACK_EVIDENCE=True,
            CUTOVER_RUNBOOK_REVIEWED_AT="",
            CUTOVER_ROLLBACK_TESTED_AT="",
        ):
            self._seed_cutover_green_baseline()
            response = self.client.get("/api/reports/cutover/phase9-exit")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["decisions"]["assistedExitReady"])
        self.assertFalse(payload["decisions"]["primaryModeReady"])
        self.assertFalse(payload["decisions"]["compatibilityRetirementReady"])
        self.assertIn("Rollback runbook review or rollback drill evidence is missing or stale.", payload["blockers"])
        self.assertIn("Pilot cycle has not yet produced a verified completion.", payload["blockers"])
        rollback_action = next(item for item in payload["recommendedActionItems"] if item["code"] == "refresh_rollback_evidence")
        self.assertEqual(rollback_action["actionType"], "focusRollbackEvidence")

    def test_cutover_phase9_exit_report_turns_ready_after_verified_pilot_and_signoffs(self) -> None:
        pilot_user = get_user_model().objects.create_user(username="pilot-exit-green", password="x")

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
            CUTOVER_ENFORCE_ROLLBACK_EVIDENCE=True,
        ):
            self._seed_cutover_green_baseline()
            self._create_pilot_cycle(pilot_user=pilot_user)
            update_rollback_evidence(
                evidence_type="runbook_review",
                evidence_date=timezone.localdate(),
                changed_by="director",
                notes="Runbook reviewed before primary-mode decision.",
            )
            update_rollback_evidence(
                evidence_type="rollback_drill",
                evidence_date=timezone.localdate(),
                changed_by="director",
                notes="Rollback drill completed before primary-mode decision.",
            )
            ScriptSignoff.objects.filter(script_name__in=["Blueprints.gs", "Corporation.gs"]).update(
                status=ScriptSignoff.Status.VALIDATED,
                signed_off_by="director",
                signed_off_at=timezone.now(),
            )

            response = self.client.get("/api/reports/cutover/phase9-exit")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["decisions"]["assistedExitReady"])
        self.assertTrue(payload["decisions"]["primaryModeReady"])
        self.assertTrue(payload["decisions"]["compatibilityRetirementReady"])
        self.assertEqual(payload["summary"]["blockingFailureCount"], 0)
        self.assertEqual(payload["pilot"]["pilotExpansionGoNoGo"], True)

    def test_cutover_phase10_prep_report_blocks_primary_while_still_in_shadow(self) -> None:
        response = self.client.get("/api/reports/cutover/phase10-prep")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["decisions"]["canEnterPrimaryMode"])
        self.assertFalse(payload["decisions"]["canDisableCompatibilityMode"])
        self.assertTrue(payload["decisions"]["requiresLegacyCompatibility"])
        self.assertIn("System is still in shadow mode; assisted cutover must complete before primary mode.", payload["blockers"])

    def test_cutover_phase10_prep_report_turns_ready_after_phase9_exit_is_green(self) -> None:
        pilot_user = get_user_model().objects.create_user(username="pilot-phase10-green", password="x")

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
            CUTOVER_ENFORCE_ROLLBACK_EVIDENCE=True,
        ):
            self._seed_cutover_green_baseline()
            self._create_pilot_cycle(pilot_user=pilot_user)
            update_rollback_evidence(
                evidence_type="runbook_review",
                evidence_date=timezone.localdate(),
                changed_by="director",
                notes="Runbook reviewed before phase 10 prep decision.",
            )
            update_rollback_evidence(
                evidence_type="rollback_drill",
                evidence_date=timezone.localdate(),
                changed_by="director",
                notes="Rollback drill completed before phase 10 prep decision.",
            )
            ScriptSignoff.objects.filter(script_name__in=["Blueprints.gs", "Corporation.gs"]).update(
                status=ScriptSignoff.Status.VALIDATED,
                signed_off_by="director",
                signed_off_at=timezone.now(),
            )

            response = self.client.get("/api/reports/cutover/phase10-prep")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["decisions"]["canEnterPrimaryMode"])
        self.assertTrue(payload["decisions"]["canDisableCompatibilityMode"])
        self.assertFalse(payload["decisions"]["requiresLegacyCompatibility"])
        self.assertEqual(payload["summary"]["blockingFailureCount"], 0)

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

    def test_cutover_preflight_includes_rollback_evidence_guidance_when_enforced_and_missing(self) -> None:
        with self.settings(
            CUTOVER_MODE="assisted",
            CUTOVER_PILOT_USER_IDS=[10],
            CUTOVER_REQUIRED_SCRIPT_SIGNOFFS=["Blueprints.gs", "Corporation.gs"],
            CUTOVER_LEAD="lead",
            CUTOVER_INCIDENT_COMMANDER="ic",
            CUTOVER_BACKEND_OWNER="backend",
            CUTOVER_DATA_OWNER="data",
            CUTOVER_DIRECTOR_REPRESENTATIVE="director",
            CUTOVER_ROLLBACK_APPROVER="rollback",
            CUTOVER_ENFORCE_ROLLBACK_EVIDENCE=True,
            CUTOVER_RUNBOOK_REVIEWED_AT="",
            CUTOVER_ROLLBACK_TESTED_AT="",
        ):
            self._seed_cutover_green_baseline()
            response = self.client.get("/api/reports/cutover/preflight?trendLimit=3")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        rollback_item = next(item for item in payload["recommendedActionItems"] if item["code"] == "refresh_rollback_evidence")
        self.assertEqual(rollback_item["actionType"], "focusRollbackEvidence")
        self.assertEqual(
            rollback_item["targetSetting"],
            "CUTOVER_RUNBOOK_REVIEWED_AT,CUTOVER_ROLLBACK_TESTED_AT",
        )
        self.assertIn("Rollback runbook review or rollback drill evidence is missing or stale.", payload["preflightBlockers"])

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
                "rollbackSummary": {
                    "status": {"gateSatisfied": True},
                    "runbookReviewedAt": "2026-03-01",
                    "rollbackTestedAt": "2026-03-01",
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
        self.assertIn("rollbackComparison", changes)
        self.assertNotEqual(payload["recommendedActionItems"][0]["actionType"], "persistEvidence")
        self.assertNotIn("payload", payload["latestStoredPreflightSnapshot"])

    def test_cutover_preflight_diff_tracks_rollback_evidence_changes_against_baseline(self) -> None:
        ReportSnapshot.objects.create(
            snapshot_date=timezone.localdate(),
            report_name="cutover_preflight",
            incident_count=0,
            go_no_go=True,
            payload={
                "readiness": {
                    "mode": "assisted",
                    "goNoGo": True,
                    "blockers": [],
                },
                "rollbackSummary": {
                    "status": {"gateSatisfied": True},
                    "runbookReviewedAt": "2026-03-01",
                    "rollbackTestedAt": "2026-03-01",
                },
                "recommendedActions": [],
                "current": {
                    "assignedRoles": 6,
                    "requiredRoles": 6,
                    "validatedSignoffs": 4,
                    "requiredSignoffs": 4,
                    "blockerCount": 0,
                    "incidentCount": 0,
                    "workforceProvenance": {"total": 1, "recommended": 1, "manual": 0, "system": 0},
                },
            },
        )

        with self.settings(
            CUTOVER_MODE="assisted",
            CUTOVER_PILOT_USER_IDS=[10],
            CUTOVER_REQUIRED_SCRIPT_SIGNOFFS=["Blueprints.gs", "Corporation.gs"],
            CUTOVER_LEAD="lead",
            CUTOVER_INCIDENT_COMMANDER="ic",
            CUTOVER_BACKEND_OWNER="backend",
            CUTOVER_DATA_OWNER="data",
            CUTOVER_DIRECTOR_REPRESENTATIVE="director",
            CUTOVER_ROLLBACK_APPROVER="rollback",
            CUTOVER_ENFORCE_ROLLBACK_EVIDENCE=True,
            CUTOVER_RUNBOOK_REVIEWED_AT="",
            CUTOVER_ROLLBACK_TESTED_AT="",
        ):
            self._seed_cutover_green_baseline()
            response = self.client.get("/api/reports/cutover/preflight?trendLimit=3")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        changes = payload["changesVsStoredPreflight"]
        self.assertTrue(changes["rollbackComparison"]["gateChanged"])
        self.assertEqual(changes["rollbackComparison"]["previousGateSatisfied"], True)
        self.assertEqual(changes["rollbackComparison"]["currentGateSatisfied"], False)
        self.assertTrue(changes["rollbackComparison"]["runbookChanged"])
        self.assertTrue(changes["rollbackComparison"]["rollbackTestChanged"])
        rollback_gate_row = next(item for item in changes["detailRows"] if item["label"] == "Rollback gate changed")
        self.assertEqual(rollback_gate_row["actionItem"]["code"], "refresh_rollback_evidence")
        runbook_row = next(item for item in changes["detailRows"] if item["label"] == "Runbook evidence updated")
        self.assertEqual(runbook_row["value"], "2026-03-01 -> missing")
        drill_row = next(item for item in changes["detailRows"] if item["label"] == "Rollback drill evidence updated")
        self.assertEqual(drill_row["value"], "2026-03-01 -> missing")

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

    def test_cutover_preflight_blocks_assisted_expansion_when_pilot_posture_is_not_ready(self) -> None:
        pilot_user = get_user_model().objects.create_user(username="pilot-preflight", password="x")

        with self.settings(
            CUTOVER_MODE="assisted",
            CUTOVER_READ_ONLY_ASSIGNMENT=False,
            CUTOVER_COMPATIBILITY_MODE=True,
            CUTOVER_PILOT_USER_IDS=[pilot_user.id],
            CUTOVER_PILOT_MAX_VERIFY_MISS_RATE_PERCENT=0,
            CUTOVER_PILOT_MAX_ESCALATED_COUNT=0,
            CUTOVER_PILOT_MAX_MANUAL_INTERVENTION_COUNT=0,
            CUTOVER_REQUIRED_SCRIPT_SIGNOFFS=["Blueprints.gs", "Corporation.gs"],
            CUTOVER_LEAD="lead",
            CUTOVER_INCIDENT_COMMANDER="ic",
            CUTOVER_BACKEND_OWNER="backend",
            CUTOVER_DATA_OWNER="data",
            CUTOVER_DIRECTOR_REPRESENTATIVE="director",
            CUTOVER_ROLLBACK_APPROVER="rollback",
        ):
            self._seed_cutover_green_baseline()
            self._create_pilot_policy_breach_state(pilot_user=pilot_user)

            response = self.client.get("/api/reports/cutover/preflight?trendLimit=3")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["effectiveGoNoGo"])
        self.assertIn("Pilot rollout evidence is not yet ready for assisted expansion.", payload["preflightBlockers"])
        self.assertEqual(payload["pilot"]["pilotStage"], "cycle_verified_with_retries")
        self.assertFalse(payload["pilot"]["pilotExpansionGoNoGo"])
        self.assertTrue(payload["pilot"]["policyBlocked"])
        self.assertEqual(payload["current"]["pilotStage"], "cycle_verified_with_retries")
        self.assertTrue(payload["current"]["pilotPolicyBlocked"])
        self.assertEqual(payload["current"]["pilotIncidentCount"], 1)
        retry_action = next(item for item in payload["recommendedActionItems"] if item["code"] == "reduce_pilot_retry_pressure")
        manual_action = next(
            item for item in payload["recommendedActionItems"] if item["code"] == "stabilize_pilot_manual_interventions"
        )
        self.assertEqual(retry_action["actionType"], "focusWorkforceBlockers")
        self.assertEqual(manual_action["actionType"], "focusWorkforceBlockers")

    def test_cutover_preflight_diff_maps_integrated_pilot_blocker_to_action_item(self) -> None:
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
                    "workforceProvenance": {"total": 1, "recommended": 1, "manual": 0, "system": 0},
                },
            },
        )
        pilot_user = get_user_model().objects.create_user(username="pilot-diff", password="x")

        with self.settings(
            CUTOVER_MODE="assisted",
            CUTOVER_READ_ONLY_ASSIGNMENT=False,
            CUTOVER_COMPATIBILITY_MODE=True,
            CUTOVER_PILOT_USER_IDS=[pilot_user.id],
            CUTOVER_PILOT_MAX_VERIFY_MISS_RATE_PERCENT=0,
            CUTOVER_PILOT_MAX_ESCALATED_COUNT=0,
            CUTOVER_PILOT_MAX_MANUAL_INTERVENTION_COUNT=0,
            CUTOVER_REQUIRED_SCRIPT_SIGNOFFS=["Blueprints.gs", "Corporation.gs"],
            CUTOVER_LEAD="lead",
            CUTOVER_INCIDENT_COMMANDER="ic",
            CUTOVER_BACKEND_OWNER="backend",
            CUTOVER_DATA_OWNER="data",
            CUTOVER_DIRECTOR_REPRESENTATIVE="director",
            CUTOVER_ROLLBACK_APPROVER="rollback",
        ):
            self._seed_cutover_green_baseline()
            self._create_pilot_policy_breach_state(pilot_user=pilot_user)

            response = self.client.get("/api/reports/cutover/preflight?trendLimit=3")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        blocker_row = next(
            item
            for item in payload["changesVsStoredPreflight"]["detailRows"]
            if item["label"] == "Blocker added"
            and item["value"] == "Pilot rollout evidence is not yet ready for assisted expansion."
        )
        self.assertEqual(blocker_row["actionItem"]["code"], "reduce_pilot_retry_pressure")

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

        self.assertEqual(ReportSnapshot.objects.count(), 5)
        self.assertIn("shadow_summary", stdout.getvalue())
        self.assertIn("cutover_readiness", stdout.getvalue())
        self.assertIn("cutover_pilot_readiness", stdout.getvalue())
        self.assertIn("cutover_preflight", stdout.getvalue())
        self.assertIn("cutover_phase9_exit", stdout.getvalue())

    def test_report_history_route_returns_recent_snapshots(self) -> None:
        call_command("persist_report_snapshots")

        response = self.client.get("/api/reports/history?limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["snapshots"]), 5)
        self.assertEqual(
            {item["reportName"] for item in payload["snapshots"]},
            {"shadow_summary", "cutover_readiness", "cutover_pilot_readiness", "cutover_preflight", "cutover_phase9_exit"},
        )

    def test_persist_report_history_route_stores_all_snapshot_types(self) -> None:
        response = self.client.post("/api/reports/history/persist")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(ReportSnapshot.objects.count(), 5)
        self.assertEqual(
            {item["reportName"] for item in payload["storedSnapshots"]},
            {"shadow_summary", "cutover_readiness", "cutover_pilot_readiness", "cutover_preflight", "cutover_phase9_exit"},
        )

    def test_report_history_route_can_filter_phase9_exit_snapshot(self) -> None:
        ReportSnapshot.objects.create(
            snapshot_date=timezone.localdate(),
            report_name="cutover_phase9_exit",
            incident_count=2,
            go_no_go=False,
            payload={
                "currentMode": "assisted",
                "pilotStage": "awaiting_verification",
                "decisions": {
                    "assistedExitReady": False,
                    "primaryModeReady": False,
                    "compatibilityRetirementReady": False,
                },
                "summary": {
                    "blockingFailureCount": 2,
                },
                "blockers": ["Pilot cycle has not yet produced a verified completion."],
            },
        )

        response = self.client.get("/api/reports/history?reportName=cutover_phase9_exit&limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["snapshots"]), 1)
        self.assertEqual(payload["snapshots"][0]["reportName"], "cutover_phase9_exit")
        self.assertFalse(payload["snapshots"][0]["payload"]["decisions"]["primaryModeReady"])
        self.assertEqual(payload["snapshots"][0]["payload"]["summary"]["blockingFailureCount"], 2)

    def test_report_history_route_can_filter_by_report_name(self) -> None:
        ReportSnapshot.objects.create(
            snapshot_date=timezone.localdate(),
            report_name="cutover_readiness",
            incident_count=1,
            go_no_go=False,
            payload={
                "mode": "assisted",
                "blockers": ["rollback stale"],
                "scriptSignoffs": {"validatedCount": 2, "requiredCount": 4},
                "rollbackSummary": {
                    "status": {"gateSatisfied": False},
                    "runbookReviewedAt": "2026-03-01",
                    "rollbackTestedAt": "2026-02-20",
                },
            },
        )

        response = self.client.get("/api/reports/history?reportName=cutover_readiness&limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["snapshots"]), 1)
        self.assertEqual(payload["snapshots"][0]["reportName"], "cutover_readiness")
        self.assertFalse(payload["snapshots"][0]["payload"]["rollbackSummary"]["status"]["gateSatisfied"])
        self.assertEqual(payload["snapshots"][0]["payload"]["rollbackSummary"]["runbookReviewedAt"], "2026-03-01")

    def test_report_history_route_can_filter_preflight_snapshot(self) -> None:
        ReportSnapshot.objects.create(
            snapshot_date=timezone.localdate(),
            report_name="cutover_preflight",
            incident_count=1,
            go_no_go=False,
            payload={
                "readiness": {"mode": "assisted", "goNoGo": False, "blockers": []},
                "rollbackSummary": {
                    "status": {
                        "gateSatisfied": False,
                    },
                    "runbookReviewedAt": "2026-03-01",
                    "rollbackTestedAt": "2026-02-20",
                },
                "current": {
                    "assignedRoles": 6,
                    "requiredRoles": 6,
                    "validatedSignoffs": 4,
                    "requiredSignoffs": 4,
                    "blockerCount": 1,
                    "incidentCount": 1,
                    "workforceProvenance": {"total": 1, "recommended": 1, "manual": 0, "system": 0},
                },
            },
        )

        response = self.client.get("/api/reports/history?reportName=cutover_preflight&limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["snapshots"]), 1)
        self.assertEqual(payload["snapshots"][0]["reportName"], "cutover_preflight")
        self.assertFalse(payload["snapshots"][0]["payload"]["rollbackSummary"]["status"]["gateSatisfied"])
        self.assertEqual(payload["snapshots"][0]["payload"]["rollbackSummary"]["runbookReviewedAt"], "2026-03-01")

    def test_report_history_route_can_filter_pilot_snapshot(self) -> None:
        ReportSnapshot.objects.create(
            snapshot_date=timezone.localdate(),
            report_name="cutover_pilot_readiness",
            incident_count=1,
            go_no_go=False,
            payload={
                "pilotStage": "awaiting_verification",
                "rollbackSummary": {
                    "status": {
                        "gateSatisfied": False,
                        "evidenceCurrent": False,
                    },
                    "runbookReviewedAt": "2026-03-01",
                    "runbookAgeDays": 6,
                    "rollbackTestedAt": "2026-02-20",
                    "rollbackTestAgeDays": 16,
                },
            },
        )

        response = self.client.get("/api/reports/history?reportName=cutover_pilot_readiness&limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["snapshots"]), 1)
        self.assertEqual(payload["snapshots"][0]["reportName"], "cutover_pilot_readiness")
        self.assertFalse(payload["snapshots"][0]["payload"]["rollbackSummary"]["status"]["gateSatisfied"])
        self.assertEqual(payload["snapshots"][0]["payload"]["rollbackSummary"]["runbookReviewedAt"], "2026-03-01")

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
                "rollbackSummary": {
                    "status": {"gateSatisfied": False},
                    "runbookReviewedAt": "2026-03-01",
                    "rollbackTestedAt": "2026-02-20",
                },
            },
        )

        response = self.client.get("/api/reports/cutover/trend?limit=5")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["trend"]), 1)
        self.assertEqual(payload["trend"][0]["assignedRoles"], 3)
        self.assertEqual(payload["trend"][0]["validatedSignoffs"], 1)
        self.assertEqual(payload["trend"][0]["blockerCount"], 2)
        self.assertFalse(payload["trend"][0]["rollbackGateSatisfied"])
        self.assertEqual(payload["trend"][0]["runbookReviewedAt"], "2026-03-01")
        self.assertEqual(payload["trend"][0]["rollbackTestedAt"], "2026-02-20")

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
                "operationalSummary": {
                    "verifyMissRatePercent": 100.0,
                    "escalatedCount": 1,
                    "directorInterventionCount": 2,
                    "manualInterventionCount": 1,
                    "tempDonePastSlaCount": 1,
                    "failedOpenCount": 1,
                },
                "policySummary": {
                    "status": {
                        "withinPolicy": False,
                    }
                },
                "rollbackSummary": {
                    "status": {
                        "gateSatisfied": False,
                        "evidenceCurrent": False,
                    },
                    "runbookReviewedAt": "2026-03-01",
                    "runbookAgeDays": 6,
                    "rollbackTestedAt": "2026-02-20",
                    "rollbackTestAgeDays": 16,
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
        self.assertEqual(payload["trend"][0]["verifyMissRatePercent"], 100.0)
        self.assertEqual(payload["trend"][0]["escalatedCount"], 1)
        self.assertEqual(payload["trend"][0]["manualInterventionCount"], 1)
        self.assertEqual(payload["trend"][0]["tempDonePastSlaCount"], 1)
        self.assertTrue(payload["trend"][0]["policyBlocked"])
        self.assertFalse(payload["trend"][0]["rollbackGateSatisfied"])
        self.assertFalse(payload["trend"][0]["rollbackEvidenceCurrent"])
        self.assertEqual(payload["trend"][0]["runbookReviewedAt"], "2026-03-01")
        self.assertEqual(payload["trend"][0]["rollbackTestedAt"], "2026-02-20")


class SdeImportTests(DjangoTestCase):
    def _build_sde_archive(self, *, build_number: int, release_date: str, product_name: str) -> str:
        temp_file = tempfile.NamedTemporaryFile(prefix="sde_test_", suffix=".zip", delete=False)
        archive_path = temp_file.name
        temp_file.close()

        categories = [{"_key": 1, "name": {"en": "Ship"}}]
        groups = [{"_key": 10, "categoryID": 1, "name": {"en": "Frigate"}}]
        types = [
            {"_key": 1000, "groupID": 10, "name": {"en": "Blueprint A"}, "portionSize": 1},
            {"_key": 2000, "groupID": 10, "name": {"en": product_name}, "portionSize": 1},
            {"_key": 3000, "groupID": 10, "name": {"en": "Tritanium"}, "portionSize": 1},
        ]
        blueprints = [
            {
                "_key": 1000,
                "blueprintTypeID": 1000,
                "maxProductionLimit": 300,
                "activities": {
                    "manufacturing": {
                        "time": 120,
                        "materials": [{"typeID": 3000, "quantity": 7}],
                        "products": [{"typeID": 2000, "quantity": 1}],
                    }
                },
            }
        ]
        type_materials = [{"_key": 2000, "materials": [{"materialTypeID": 3000, "quantity": 7}]}]

        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("ccp/_sde.jsonl", json.dumps({"buildNumber": build_number, "releaseDate": release_date}) + "\n")
            archive.writestr("ccp/categories.jsonl", "".join(json.dumps(row) + "\n" for row in categories))
            archive.writestr("ccp/groups.jsonl", "".join(json.dumps(row) + "\n" for row in groups))
            archive.writestr("ccp/types.jsonl", "".join(json.dumps(row) + "\n" for row in types))
            archive.writestr("ccp/blueprints.jsonl", "".join(json.dumps(row) + "\n" for row in blueprints))
            archive.writestr("ccp/typeMaterials.jsonl", "".join(json.dumps(row) + "\n" for row in type_materials))

        return archive_path

    def test_import_sde_archive_replaces_existing_build_and_updates_state(self) -> None:
        first_archive = self._build_sde_archive(build_number=100, release_date="2026-03-01", product_name="First Product")
        second_archive = self._build_sde_archive(build_number=101, release_date="2026-03-02", product_name="Second Product")

        try:
            first_result = import_sde_archive(archive_path=first_archive, source_filename="first.zip", triggered_by="director")
            second_result = import_sde_archive(archive_path=second_archive, source_filename="second.zip", triggered_by="director")
        finally:
            for path in [first_archive, second_archive]:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass

        self.assertTrue(first_result["imported"])
        self.assertTrue(second_result["imported"])
        state = SdeImportState.objects.get(source="ccp_sde")
        self.assertEqual(state.current_build_number, 101)
        self.assertEqual(state.source_filename, "second.zip")

        latest_run = SdeImportRun.objects.first()
        self.assertEqual(latest_run.status, SdeImportRun.Status.SUCCEEDED)
        self.assertEqual(latest_run.imported_build_number, 101)

        with connection.cursor() as cursor:
            cursor.execute("SELECT typeName FROM invTypes WHERE typeID = %s", [2000])
            row = cursor.fetchone()
        self.assertEqual(row[0], "Second Product")

    def test_import_sde_archive_skips_same_build_without_force(self) -> None:
        archive_path = self._build_sde_archive(build_number=120, release_date="2026-03-03", product_name="Stable Product")

        try:
            first_result = import_sde_archive(archive_path=archive_path, source_filename="stable.zip", triggered_by="director")
            second_result = import_sde_archive(archive_path=archive_path, source_filename="stable.zip", triggered_by="director")
        finally:
            try:
                os.unlink(archive_path)
            except FileNotFoundError:
                pass

        self.assertTrue(first_result["imported"])
        self.assertTrue(second_result["skipped"])
        self.assertEqual(SdeImportRun.objects.filter(status=SdeImportRun.Status.SKIPPED).count(), 1)

    @patch("apps.common.views.import_sde_from_url")
    def test_sde_import_route_accepts_url_and_returns_state(self, import_from_url_mock: MagicMock) -> None:
        import_from_url_mock.return_value = {
            "imported": True,
            "skipped": False,
            "state": {
                "source": "ccp_sde",
                "currentBuildNumber": 555,
                "currentReleaseDate": "2026-03-05",
                "archiveSha256": "abc",
                "archiveSourceUrl": "https://example.invalid/ccp.zip",
                "sourceFilename": "ccp.zip",
                "lastCheckedAt": None,
                "lastImportedAt": None,
            },
            "run": {
                "id": 1,
                "status": "succeeded",
                "detectedBuildNumber": 555,
                "importedBuildNumber": 555,
            },
        }

        response = self.client.post(
            "/api/sde/import-from-url",
            data=json.dumps(
                {
                    "archiveUrl": "https://example.invalid/ccp.zip",
                    "triggeredBy": "director",
                    "forceReimport": True,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["imported"])
        self.assertEqual(payload["state"]["currentBuildNumber"], 555)
        import_from_url_mock.assert_called_once_with(
            archive_url="https://example.invalid/ccp.zip",
            triggered_by="director",
            force_reimport=True,
        )

    @patch("apps.common.views.import_sde_from_upload")
    def test_sde_import_upload_route_accepts_zip_file_and_returns_state(self, import_from_upload_mock: MagicMock) -> None:
        import_from_upload_mock.return_value = {
            "imported": True,
            "skipped": False,
            "state": {
                "source": "ccp_sde",
                "currentBuildNumber": 777,
                "currentReleaseDate": "2026-03-06",
                "archiveSha256": "def",
                "archiveSourceUrl": "",
                "sourceFilename": "ccp-upload.zip",
                "lastCheckedAt": None,
                "lastImportedAt": None,
            },
            "run": {
                "id": 2,
                "status": "succeeded",
                "detectedBuildNumber": 777,
                "importedBuildNumber": 777,
            },
        }
        uploaded_file = SimpleUploadedFile("ccp-upload.zip", b"PK\x03\x04fakezip", content_type="application/zip")

        response = self.client.post(
            "/api/sde/import-upload",
            data={
                "archive": uploaded_file,
                "triggeredBy": "director",
                "forceReimport": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["imported"])
        self.assertEqual(payload["state"]["currentBuildNumber"], 777)
        import_from_upload_mock.assert_called_once()
        self.assertEqual(import_from_upload_mock.call_args.kwargs["triggered_by"], "director")
        self.assertEqual(import_from_upload_mock.call_args.kwargs["force_reimport"], True)
