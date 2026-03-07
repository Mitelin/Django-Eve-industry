from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.utils import timezone

from apps.accounts.models import Character
from apps.corp_sync.models import CorpJobSnapshot, SyncRun
from apps.industry_planner.models import PlanJob, Project, ProjectTarget
from apps.workforce.models import WorkEvent, WorkItem
from apps.workforce.services import InvalidWorkItemTransition, NoAvailableWorkItem, StaleVerificationData, VerificationWindowOpen, WorkforceService


class WorkforceServiceTests(TestCase):
    def setUp(self) -> None:
        self.service = WorkforceService()
        self.user = get_user_model().objects.create_user(username="worker", password="x")
        self.other_user = get_user_model().objects.create_user(username="other", password="x")
        self.character = Character.objects.create(
            user=self.user,
            eve_character_id=90000001,
            name="Worker Main",
            corporation_id=123,
            is_main=True,
        )
        self.project = Project.objects.create(name="Workforce Project", priority=5, created_by=self.user)
        self.plan_job = PlanJob.objects.create(
            project=self.project,
            activity_id=1,
            blueprint_type_id=100,
            product_type_id=200,
            runs=3,
            expected_duration_s=120,
            level=1,
            is_advanced=False,
            params_hash="hash-a",
        )

    def test_dispatch_project_creates_ready_work_items(self) -> None:
        dispatched = self.service.dispatch_project(self.project)

        self.assertEqual(len(dispatched), 1)
        work_item = WorkItem.objects.get(project=self.project, plan_job=self.plan_job)
        self.assertEqual(work_item.status, "ready")
        self.assertEqual(work_item.kind, "start_job")
        self.assertEqual(work_item.payload["expectedRuns"], 3)
        self.assertEqual(work_item.events.count(), 1)

    def test_claim_next_assigns_highest_priority_ready_item(self) -> None:
        slow = PlanJob.objects.create(
            project=self.project,
            activity_id=1,
            blueprint_type_id=101,
            product_type_id=201,
            runs=1,
            expected_duration_s=300,
            level=1,
            is_advanced=False,
            params_hash="hash-b",
        )
        self.service.dispatch_project(self.project)
        work_item = WorkItem.objects.get(plan_job=self.plan_job)
        WorkItem.objects.filter(plan_job=slow).update(priority_score=work_item.priority_score - 100)

        claimed = self.service.claim_next(user=self.user)

        self.assertEqual(claimed.plan_job_id, self.plan_job.id)
        claimed.refresh_from_db()
        self.assertEqual(claimed.status, "assigned")
        self.assertEqual(claimed.assigned_to, self.user)
        self.assertEqual(claimed.attempt, 1)
        self.assertIn("assignedAt", claimed.payload)

    def test_claim_next_raises_when_queue_empty(self) -> None:
        with self.assertRaises(NoAvailableWorkItem):
            self.service.claim_next(user=self.user)

    def test_mark_temp_done_is_idempotent_for_same_key(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = self.service.claim_next(user=self.user)

        first = self.service.mark_temp_done(work_item=work_item, actor=self.user, idempotency_key="abc")
        second = self.service.mark_temp_done(work_item=work_item, actor=self.user, idempotency_key="abc")

        self.assertEqual(first.pk, second.pk)
        work_item.refresh_from_db()
        self.assertEqual(work_item.status, "temp_done")
        self.assertEqual(work_item.events.filter(event_type="TEMP_DONE").count(), 1)

    def test_release_returns_item_to_ready(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = self.service.claim_next(user=self.user)

        released = self.service.release(work_item=work_item, actor=self.user)

        self.assertEqual(released.status, "ready")
        self.assertIsNone(released.assigned_to)
        self.assertEqual(released.events.filter(event_type="RELEASED").count(), 1)

    def test_director_requeue_returns_problem_item_to_ready(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = self.service.claim_next(user=self.user)
        work_item = self.service.mark_temp_done(work_item=work_item, actor=self.user, idempotency_key="director-requeue")

        requeued = self.service.director_requeue(work_item=work_item, reason="cleanup", source="recommended_action")

        self.assertEqual(requeued.status, "ready")
        self.assertIsNone(requeued.assigned_to)
        self.assertNotIn("tempDoneKey", requeued.payload)
        self.assertEqual(requeued.events.filter(event_type="DIRECTOR_REQUEUED").count(), 1)
        self.assertEqual(requeued.events.get(event_type="DIRECTOR_REQUEUED").details["source"], "recommended_action")

    def test_director_requeue_rejects_ready_items(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = WorkItem.objects.get(project=self.project, plan_job=self.plan_job)

        with self.assertRaises(InvalidWorkItemTransition):
            self.service.director_requeue(work_item=work_item)

    def test_director_release_returns_assigned_item_to_ready(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = self.service.claim_next(user=self.user)

        released = self.service.director_release(work_item=work_item, reason="cleanup", source="recommended_action")

        self.assertEqual(released.status, "ready")
        self.assertIsNone(released.assigned_to)
        self.assertEqual(released.events.filter(event_type="DIRECTOR_RELEASED").count(), 1)
        self.assertEqual(released.events.get(event_type="DIRECTOR_RELEASED").details["source"], "recommended_action")

    def test_verify_start_job_records_source_in_events(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = self.service.claim_next(user=self.user, lock_ttl=timedelta(minutes=45))
        work_item = self.service.mark_temp_done(work_item=work_item, actor=self.user, idempotency_key="verify-source")
        assigned_at = timezone.datetime.fromisoformat(work_item.payload["assignedAt"])
        sync_run = SyncRun.objects.create(kind="jobs", corporation_id=123, status="ok", finished_at=timezone.now())
        CorpJobSnapshot.objects.create(
            sync_run=sync_run,
            job_id=990,
            activity_id=1,
            blueprint_type_id=100,
            product_type_id=200,
            runs=3,
            installer_id=self.character.eve_character_id,
            start_date=assigned_at + timedelta(minutes=1),
        )

        verified = self.service.verify_start_job(work_item=work_item, source="recommended_action")

        self.assertTrue(verified)
        work_item.refresh_from_db()
        self.assertEqual(work_item.status, "verified")
        self.assertEqual(work_item.events.get(event_type="VERIFIED_OK").details["source"], "recommended_action")

    def test_director_release_rejects_non_assigned_item(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = WorkItem.objects.get(project=self.project, plan_job=self.plan_job)

        with self.assertRaises(InvalidWorkItemTransition):
            self.service.director_release(work_item=work_item)

    def test_expire_locks_requeues_expired_assigned_items(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = self.service.claim_next(user=self.user)
        WorkItem.objects.filter(pk=work_item.pk).update(locked_until=timezone.now() - timedelta(minutes=1))

        expired = self.service.expire_locks()

        self.assertEqual(expired, 1)
        work_item.refresh_from_db()
        self.assertEqual(work_item.status, "ready")
        self.assertIsNone(work_item.assigned_to)

    def test_verify_start_job_marks_verified_when_snapshot_matches(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = self.service.claim_next(user=self.user, lock_ttl=timedelta(minutes=45))
        work_item = self.service.mark_temp_done(work_item=work_item, actor=self.user, idempotency_key="done-1")
        assigned_at = timezone.datetime.fromisoformat(work_item.payload["assignedAt"])
        sync_run = SyncRun.objects.create(kind="jobs", corporation_id=123, status="ok", finished_at=timezone.now())
        CorpJobSnapshot.objects.create(
            sync_run=sync_run,
            job_id=777,
            activity_id=1,
            blueprint_type_id=100,
            product_type_id=200,
            runs=3,
            installer_id=self.character.eve_character_id,
            start_date=assigned_at + timedelta(minutes=2),
        )

        verified = self.service.verify_start_job(work_item=work_item, now=assigned_at + timedelta(minutes=3))

        self.assertTrue(verified)
        work_item.refresh_from_db()
        self.assertEqual(work_item.status, "verified")
        self.assertIsNotNone(work_item.verified_at)
        self.assertEqual(work_item.events.filter(event_type="VERIFIED_OK").count(), 1)

    def test_verify_start_job_requeues_after_sla_miss(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = self.service.claim_next(user=self.user, lock_ttl=timedelta(minutes=45))
        work_item = self.service.mark_temp_done(work_item=work_item, actor=self.user, idempotency_key="done-2")
        assigned_at = timezone.datetime.fromisoformat(work_item.payload["assignedAt"])
        SyncRun.objects.create(kind="jobs", corporation_id=123, status="ok", finished_at=timezone.now())

        verified = self.service.verify_start_job(work_item=work_item, now=assigned_at + timedelta(minutes=31))

        self.assertFalse(verified)
        work_item.refresh_from_db()
        self.assertEqual(work_item.status, "ready")
        self.assertIsNone(work_item.assigned_to)
        self.assertEqual(work_item.events.filter(event_type="VERIFY_MISS").count(), 1)
        self.assertEqual(work_item.events.filter(event_type="REQUEUED").count(), 1)

    def test_verify_start_job_raises_while_sla_window_open_without_match(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = self.service.claim_next(user=self.user, lock_ttl=timedelta(minutes=45))
        work_item = self.service.mark_temp_done(work_item=work_item, actor=self.user, idempotency_key="done-3")
        assigned_at = timezone.datetime.fromisoformat(work_item.payload["assignedAt"])
        SyncRun.objects.create(kind="jobs", corporation_id=123, status="ok", finished_at=timezone.now())

        with self.assertRaises(VerificationWindowOpen):
            self.service.verify_start_job(work_item=work_item, now=assigned_at + timedelta(minutes=5))

    def test_verify_start_job_raises_when_jobs_sync_is_stale(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = self.service.claim_next(user=self.user, lock_ttl=timedelta(minutes=45))
        work_item = self.service.mark_temp_done(work_item=work_item, actor=self.user, idempotency_key="done-stale")
        SyncRun.objects.create(
            kind="jobs",
            corporation_id=123,
            status="ok",
            finished_at=timezone.now() - timedelta(hours=2),
        )

        with self.assertRaises(StaleVerificationData):
            self.service.verify_start_job(work_item=work_item)

    def test_verify_start_job_escalates_after_retry_cap(self) -> None:
        self.service = WorkforceService(max_attempts=1)
        self.service.dispatch_project(self.project)
        work_item = self.service.claim_next(user=self.user, lock_ttl=timedelta(minutes=45))
        work_item = self.service.mark_temp_done(work_item=work_item, actor=self.user, idempotency_key="done-cap")
        assigned_at = timezone.datetime.fromisoformat(work_item.payload["assignedAt"])
        SyncRun.objects.create(kind="jobs", corporation_id=123, status="ok", finished_at=timezone.now())

        verified = self.service.verify_start_job(work_item=work_item, now=assigned_at + timedelta(minutes=31))

        self.assertFalse(verified)
        work_item.refresh_from_db()
        self.assertEqual(work_item.status, "failed")
        self.assertEqual(work_item.events.filter(event_type="ESCALATED").count(), 1)

    def test_verify_batch_reports_verified_requeued_stale_and_escalated(self) -> None:
        self.service = WorkforceService(max_attempts=1)
        first_project = Project.objects.create(name="Verify Project", priority=5, created_by=self.user)
        verified_job = PlanJob.objects.create(
            project=first_project,
            activity_id=1,
            blueprint_type_id=111,
            product_type_id=211,
            runs=1,
            expected_duration_s=10,
            level=1,
            is_advanced=False,
            params_hash="v1",
        )
        stale_job = PlanJob.objects.create(
            project=first_project,
            activity_id=1,
            blueprint_type_id=112,
            product_type_id=212,
            runs=1,
            expected_duration_s=10,
            level=1,
            is_advanced=False,
            params_hash="v2",
        )
        escalate_job = PlanJob.objects.create(
            project=first_project,
            activity_id=1,
            blueprint_type_id=113,
            product_type_id=213,
            runs=1,
            expected_duration_s=10,
            level=1,
            is_advanced=False,
            params_hash="v3",
        )

        verified_item = WorkItem.objects.create(
            project=first_project,
            plan_job=verified_job,
            kind="start_job",
            status="temp_done",
            assigned_to=self.user,
            attempt=1,
            priority_score=1,
            payload={
                "expectedActivityId": 1,
                "expectedBlueprintTypeId": 111,
                "expectedProductTypeId": 211,
                "expectedRuns": 1,
                "assignedAt": (timezone.now() - timedelta(minutes=40)).isoformat(),
            },
        )
        stale_item = WorkItem.objects.create(
            project=first_project,
            plan_job=stale_job,
            kind="start_job",
            status="temp_done",
            assigned_to=self.other_user,
            attempt=1,
            priority_score=1,
            payload={
                "expectedActivityId": 1,
                "expectedBlueprintTypeId": 112,
                "expectedProductTypeId": 212,
                "expectedRuns": 1,
                "assignedAt": (timezone.now() - timedelta(minutes=40)).isoformat(),
            },
        )
        escalate_item = WorkItem.objects.create(
            project=first_project,
            plan_job=escalate_job,
            kind="start_job",
            status="temp_done",
            assigned_to=self.user,
            attempt=1,
            priority_score=1,
            payload={
                "expectedActivityId": 1,
                "expectedBlueprintTypeId": 113,
                "expectedProductTypeId": 213,
                "expectedRuns": 1,
                "assignedAt": (timezone.now() - timedelta(minutes=40)).isoformat(),
            },
        )
        Character.objects.create(
            user=self.other_user,
            eve_character_id=90000002,
            name="Other Main",
            corporation_id=999,
            is_main=True,
        )
        SyncRun.objects.create(kind="jobs", corporation_id=123, status="ok", finished_at=timezone.now())
        SyncRun.objects.create(kind="jobs", corporation_id=999, status="ok", finished_at=timezone.now() - timedelta(hours=2))
        sync_run = SyncRun.objects.filter(kind="jobs", corporation_id=123).order_by("-id").first()
        CorpJobSnapshot.objects.create(
            sync_run=sync_run,
            job_id=10001,
            activity_id=1,
            blueprint_type_id=111,
            product_type_id=211,
            runs=1,
            installer_id=self.character.eve_character_id,
            start_date=timezone.now() - timedelta(minutes=35),
        )

        result = self.service.verify_batch(now=timezone.now())

        self.assertEqual(result, {"verified": 1, "requeued": 0, "stale": 1, "escalated": 1})
        verified_item.refresh_from_db()
        stale_item.refresh_from_db()
        escalate_item.refresh_from_db()
        self.assertEqual(verified_item.status, "verified")
        self.assertEqual(stale_item.status, "temp_done")
        self.assertEqual(escalate_item.status, "failed")

    def test_get_project_progress_counts_statuses(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = self.service.claim_next(user=self.user)
        self.service.mark_temp_done(work_item=work_item, actor=self.user, idempotency_key="done-4")

        progress = self.service.get_project_progress(project=self.project)

        self.assertEqual(progress.project_id, self.project.id)
        self.assertEqual(progress.total, 1)
        self.assertEqual(progress.temp_done, 1)

    def test_get_project_jobs_freshness_reports_sync_state(self) -> None:
        SyncRun.objects.create(kind="jobs", corporation_id=123, status="ok", finished_at=timezone.now())

        freshness = self.service.get_project_jobs_freshness(project=self.project)

        self.assertEqual(freshness.corporation_id, 123)
        self.assertFalse(freshness.is_stale)
        self.assertIsNotNone(freshness.last_success_at)


class WorkforceRouteTests(TestCase):
    def setUp(self) -> None:
        self.service = WorkforceService()
        self.user = get_user_model().objects.create_user(username="route-worker", password="x")
        self.other_user = get_user_model().objects.create_user(username="route-outsider", password="x")
        self.character = Character.objects.create(
            user=self.user,
            eve_character_id=90000101,
            name="Route Worker Main",
            corporation_id=123,
            is_main=True,
        )
        Character.objects.create(
            user=self.other_user,
            eve_character_id=90000102,
            name="Route Outsider Main",
            corporation_id=123,
            is_main=True,
        )
        self.project = Project.objects.create(name="Route Workforce", priority=4, created_by=self.user)
        self.plan_job = PlanJob.objects.create(
            project=self.project,
            activity_id=1,
            blueprint_type_id=100,
            product_type_id=200,
            runs=2,
            expected_duration_s=90,
            level=1,
            is_advanced=False,
            params_hash="route-hash",
        )

    def test_dispatch_and_claim_routes(self) -> None:
        dispatch_response = self.client.post(f"/api/projects/{self.project.id}/dispatch")

        self.assertEqual(dispatch_response.status_code, 200)
        self.assertEqual(len(dispatch_response.json()["workItems"]), 1)

        claim_response = self.client.post(
            "/api/work-items/claim",
            data={"userId": self.user.id},
            content_type="application/json",
        )

        self.assertEqual(claim_response.status_code, 200)
        self.assertEqual(claim_response.json()["workItem"]["status"], "assigned")

    @override_settings(CUTOVER_READ_ONLY_ASSIGNMENT=True, CUTOVER_MODE="assisted")
    def test_assignment_routes_blocked_in_read_only_cutover_mode(self) -> None:
        dispatch_response = self.client.post(f"/api/projects/{self.project.id}/dispatch")
        claim_response = self.client.post(
            "/api/work-items/claim",
            data={"userId": self.user.id},
            content_type="application/json",
        )

        self.assertEqual(dispatch_response.status_code, 409)
        self.assertEqual(claim_response.status_code, 409)
        self.assertEqual(claim_response.json()["cutoverMode"], "assisted")

    def test_non_pilot_worker_routes_blocked_in_assisted_mode(self) -> None:
        with self.settings(CUTOVER_MODE="assisted", CUTOVER_PILOT_USER_IDS=[self.user.id]):
            response = self.client.post(
                "/api/work-items/claim",
                data={"userId": self.other_user.id},
                content_type="application/json",
            )
            detail_response = self.client.get(f"/api/work-items/my-active-detail?userId={self.other_user.id}")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(detail_response.status_code, 403)
        self.assertEqual(response.json()["cutoverMode"], "assisted")

    def test_pilot_worker_routes_still_allowed_in_assisted_mode(self) -> None:
        with self.settings(CUTOVER_MODE="assisted", CUTOVER_PILOT_USER_IDS=[self.user.id]):
            dispatch_response = self.client.post(f"/api/projects/{self.project.id}/dispatch")
            claim_response = self.client.post(
                "/api/work-items/claim",
                data={"userId": self.user.id},
                content_type="application/json",
            )

        self.assertEqual(dispatch_response.status_code, 200)
        self.assertEqual(claim_response.status_code, 200)

    def test_ui_pages_render(self) -> None:
        home_response = self.client.get("/")
        director_response = self.client.get("/director/")
        worker_response = self.client.get("/worker/")

        self.assertEqual(home_response.status_code, 200)
        self.assertContains(home_response, "Operations Console")
        self.assertEqual(director_response.status_code, 200)
        self.assertContains(director_response, "Director Flight Deck")
        self.assertContains(director_response, "Observation Window")
        self.assertContains(director_response, "Readiness Trend")
        self.assertContains(director_response, "Cutover Preflight")
        self.assertContains(director_response, "Preflight Snapshot History")
        self.assertContains(director_response, "Preflight Change Since Last Stored Snapshot")
        self.assertContains(director_response, "Recent Sign-Off Activity")
        self.assertContains(director_response, "Role Ownership")
        self.assertContains(director_response, "Recent Role Activity")
        self.assertContains(director_response, "Action Guidance")
        self.assertContains(director_response, "High Risk Only")
        self.assertContains(director_response, "Needs Manual Attention")
        self.assertContains(director_response, "Manual Attention Summary")
        self.assertContains(director_response, "Run Recommended")
        self.assertContains(director_response, "Run First Recommended")
        self.assertContains(director_response, "Open Guidance: Wait For Verify")
        self.assertContains(director_response, "Open Guidance: Manual Review")
        self.assertContains(director_response, "Update Script Sign-Off")
        self.assertContains(director_response, "Assign Cutover Role")
        self.assertContains(director_response, "Sync Missing Role Owners")
        self.assertContains(director_response, "Sync Missing Script Sign-Offs")
        self.assertContains(director_response, "Bootstrap Governance")
        self.assertContains(director_response, "Persist Evidence")
        self.assertContains(director_response, "Recovery Actions")
        self.assertContains(director_response, "Redispatch Project")
        self.assertContains(director_response, "Run Verify Batch")
        self.assertContains(director_response, "Event Provenance")
        self.assertContains(director_response, "Recommended Events")
        self.assertContains(director_response, "Manual Events")
        self.assertContains(director_response, "System Events")
        self.assertEqual(worker_response.status_code, 200)
        self.assertContains(worker_response, "Worker Command")

    def test_temp_done_and_my_active_routes(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = self.service.claim_next(user=self.user)
        SyncRun.objects.create(kind="jobs", corporation_id=123, status="ok", finished_at=timezone.now())

        temp_done_response = self.client.post(
            f"/api/work-items/{work_item.id}/temp-done",
            data={"userId": self.user.id, "idempotencyKey": "route-key"},
            content_type="application/json",
        )

        self.assertEqual(temp_done_response.status_code, 200)
        self.assertEqual(temp_done_response.json()["workItem"]["status"], "temp_done")

        active_response = self.client.get(f"/api/work-items/my-active?userId={self.user.id}")
        active_detail_response = self.client.get(f"/api/work-items/my-active-detail?userId={self.user.id}")

        self.assertEqual(active_response.status_code, 200)
        self.assertEqual(active_response.json()["workItem"]["id"], work_item.id)
        self.assertEqual(active_detail_response.status_code, 200)
        self.assertEqual(active_detail_response.json()["workItem"]["id"], work_item.id)
        self.assertEqual(active_detail_response.json()["project"]["id"], self.project.id)
        self.assertEqual(active_detail_response.json()["planJob"]["id"], self.plan_job.id)
        self.assertEqual(len(active_detail_response.json()["planMaterials"]), 0)
        self.assertIn("Start industry job", active_detail_response.json()["instructions"]["title"])
        self.assertGreaterEqual(len(active_detail_response.json()["instructions"]["steps"]), 1)
        self.assertGreaterEqual(len(active_detail_response.json()["recentEvents"]), 2)
        self.assertIn("source", active_detail_response.json()["recentEvents"][0])
        self.assertFalse(active_detail_response.json()["staleWarning"])

    def test_my_active_detail_preserves_event_source_in_recent_events(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = self.service.claim_next(user=self.user)
        work_item = self.service.mark_temp_done(work_item=work_item, actor=self.user, idempotency_key="worker-source")
        WorkEvent.objects.create(
            work_item=work_item,
            actor=self.user,
            event_type="VERIFY_MISS",
            details={"reason": "Needs manual review", "source": "manual_action"},
        )

        response = self.client.get(f"/api/work-items/my-active-detail?userId={self.user.id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["recentEvents"][0]["source"], "manual_action")

    def test_release_and_progress_routes(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = self.service.claim_next(user=self.user)
        SyncRun.objects.create(kind="jobs", corporation_id=123, status="ok", finished_at=timezone.now())

        release_response = self.client.post(
            f"/api/work-items/{work_item.id}/release",
            data={"userId": self.user.id, "reason": "switch task"},
            content_type="application/json",
        )
        progress_response = self.client.get(f"/api/projects/{self.project.id}/progress")

        self.assertEqual(release_response.status_code, 200)
        self.assertEqual(release_response.json()["workItem"]["status"], "ready")
        self.assertEqual(progress_response.status_code, 200)
        self.assertEqual(progress_response.json()["ready"], 1)
        self.assertEqual(progress_response.json()["jobsFreshness"]["corporationId"], 123)
        self.assertFalse(progress_response.json()["staleWarning"])

    def test_director_requeue_route_resets_failed_item(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = WorkItem.objects.get(project=self.project, plan_job=self.plan_job)
        work_item.status = "failed"
        work_item.assigned_to = self.user
        work_item.save(update_fields=["status", "assigned_to", "updated_at"])

        response = self.client.post(
            f"/api/work-items/{work_item.id}/director-requeue",
            data='{"reason": "director cleanup", "source": "recommended_action"}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        work_item.refresh_from_db()
        self.assertEqual(work_item.status, "ready")
        self.assertIsNone(work_item.assigned_to)
        self.assertEqual(response.json()["workItem"]["status"], "ready")
        self.assertEqual(work_item.events.get(event_type="DIRECTOR_REQUEUED").details["source"], "recommended_action")

    def test_director_requeue_route_rejects_ready_item(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = WorkItem.objects.get(project=self.project, plan_job=self.plan_job)

        response = self.client.post(
            f"/api/work-items/{work_item.id}/director-requeue",
            data='{"reason": "director cleanup"}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("Only ASSIGNED, TEMP_DONE, or FAILED", response.json()["error"])

    def test_director_release_route_resets_assigned_item(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = self.service.claim_next(user=self.user)

        response = self.client.post(
            f"/api/work-items/{work_item.id}/director-release",
            data='{"reason": "director cleanup", "source": "recommended_action"}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        work_item.refresh_from_db()
        self.assertEqual(work_item.status, "ready")
        self.assertIsNone(work_item.assigned_to)
        self.assertEqual(response.json()["workItem"]["status"], "ready")
        self.assertEqual(work_item.events.get(event_type="DIRECTOR_RELEASED").details["source"], "recommended_action")

    def test_director_verify_route_marks_temp_done_item_verified(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = self.service.claim_next(user=self.user, lock_ttl=timedelta(minutes=45))
        work_item = self.service.mark_temp_done(work_item=work_item, actor=self.user, idempotency_key="director-verify")
        assigned_at = timezone.datetime.fromisoformat(work_item.payload["assignedAt"])
        sync_run = SyncRun.objects.create(kind="jobs", corporation_id=123, status="ok", finished_at=timezone.now())
        CorpJobSnapshot.objects.create(
            sync_run=sync_run,
            job_id=889,
            activity_id=1,
            blueprint_type_id=100,
            product_type_id=200,
            runs=2,
            installer_id=self.character.eve_character_id,
            start_date=assigned_at + timedelta(minutes=1),
        )

        response = self.client.post(
            f"/api/work-items/{work_item.id}/director-verify",
            data='{"source": "recommended_action"}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        work_item.refresh_from_db()
        self.assertEqual(work_item.status, "verified")
        self.assertTrue(response.json()["verified"])
        self.assertEqual(work_item.events.get(event_type="VERIFIED_OK").details["source"], "recommended_action")

    def test_director_verify_route_rejects_non_temp_done_item(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = WorkItem.objects.get(project=self.project, plan_job=self.plan_job)

        response = self.client.post(f"/api/work-items/{work_item.id}/director-verify")

        self.assertEqual(response.status_code, 409)
        self.assertIn("Only TEMP_DONE", response.json()["error"])

    def test_progress_route_marks_stale_when_no_jobs_sync_exists(self) -> None:
        response = self.client.get(f"/api/projects/{self.project.id}/progress")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["staleWarning"])
        self.assertTrue(response.json()["jobsFreshness"]["isStale"])

    def test_queue_route_returns_ready_items(self) -> None:
        self.service.dispatch_project(self.project)

        response = self.client.get("/api/work-items/queue")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["workItems"]), 1)

    def test_verify_batch_route_returns_batch_counts(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = self.service.claim_next(user=self.user, lock_ttl=timedelta(minutes=45))
        work_item = self.service.mark_temp_done(work_item=work_item, actor=self.user, idempotency_key="route-verify")
        assigned_at = timezone.datetime.fromisoformat(work_item.payload["assignedAt"])
        sync_run = SyncRun.objects.create(kind="jobs", corporation_id=123, status="ok", finished_at=timezone.now())
        CorpJobSnapshot.objects.create(
            sync_run=sync_run,
            job_id=888,
            activity_id=1,
            blueprint_type_id=100,
            product_type_id=200,
            runs=2,
            installer_id=self.character.eve_character_id,
            start_date=assigned_at + timedelta(minutes=1),
        )

        response = self.client.post("/api/work-items/verify-batch")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"verified": 1, "requeued": 0, "stale": 0, "escalated": 0})

    def test_director_dashboard_aggregates_projects_queue_and_stale_count(self) -> None:
        self.service.dispatch_project(self.project)
        second_project = Project.objects.create(name="Stale Project", priority=2, created_by=self.user)
        PlanJob.objects.create(
            project=second_project,
            activity_id=1,
            blueprint_type_id=300,
            product_type_id=400,
            runs=1,
            expected_duration_s=50,
            level=1,
            is_advanced=False,
            params_hash="dashboard-hash",
        )
        self.service.dispatch_project(second_project)
        SyncRun.objects.create(kind="jobs", corporation_id=123, status="ok", finished_at=timezone.now())
        claimed = self.service.claim_next(user=self.user)
        self.service.mark_temp_done(work_item=claimed, actor=self.user, idempotency_key="dash-temp")
        failed_item = WorkItem.objects.get(project=second_project)
        failed_item.status = "failed"
        failed_item.save(update_fields=["status", "updated_at"])

        response = self.client.get("/api/dashboard/director")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["projects"]), 2)
        self.assertEqual(payload["queueSummary"]["readyCount"], 0)
        self.assertEqual(len(payload["queueSummary"]["topReady"]), 0)
        self.assertEqual(payload["tempDoneSummary"]["count"], 1)
        self.assertEqual(len(payload["tempDoneSummary"]["items"]), 1)
        self.assertIsNotNone(payload["tempDoneSummary"]["oldestAgeSeconds"])
        self.assertEqual(payload["failedSummary"]["count"], 1)
        self.assertEqual(len(payload["failedSummary"]["items"]), 1)
        self.assertEqual(payload["staleProjectCount"], 0)

    def test_director_project_detail_returns_targets_breakdowns_and_workers(self) -> None:
        second_job = PlanJob.objects.create(
            project=self.project,
            activity_id=11,
            blueprint_type_id=101,
            product_type_id=201,
            runs=5,
            expected_duration_s=180,
            level=2,
            is_advanced=True,
            params_hash="detail-hash-2",
        )
        ProjectTarget.objects.create(project=self.project, type_id=200, quantity=12, is_final_output=True)
        ProjectTarget.objects.create(project=self.project, type_id=201, quantity=6, is_final_output=False)
        self.service.dispatch_project(self.project)
        SyncRun.objects.create(kind="jobs", corporation_id=123, status="ok", finished_at=timezone.now())

        first_item = WorkItem.objects.get(plan_job=self.plan_job)
        second_item = WorkItem.objects.get(plan_job=second_job)
        self.service.claim_next(user=self.user)
        self.service.mark_temp_done(work_item=first_item, actor=self.user, idempotency_key="detail-temp")
        second_item.status = "failed"
        second_item.save(update_fields=["status", "updated_at"])

        response = self.client.get(f"/api/projects/{self.project.id}/director-detail")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["project"]["id"], self.project.id)
        self.assertEqual(len(payload["targets"]), 2)
        self.assertEqual(payload["queueBreakdown"]["byStatus"]["tempDone"], 1)
        self.assertEqual(payload["queueBreakdown"]["byStatus"]["failed"], 1)
        self.assertEqual(len(payload["queueBreakdown"]["byActivity"]), 2)
        self.assertEqual(len(payload["activeWorkers"]), 1)
        self.assertEqual(payload["activeWorkers"][0]["tempDoneCount"], 1)
        self.assertEqual(len(payload["bottlenecks"]["tempDone"]), 1)
        self.assertEqual(len(payload["bottlenecks"]["failed"]), 1)
        self.assertEqual(len(payload["bottlenecks"]["manualAttention"]), 1)
        self.assertEqual(len(payload["bottlenecks"]["manualAttentionSummary"]), 1)
        self.assertEqual(payload["bottlenecks"]["failed"][0]["risk"]["label"], "failed")
        self.assertEqual(payload["bottlenecks"]["failed"][0]["risk"]["code"], "manual_cleanup")
        self.assertEqual(payload["bottlenecks"]["failed"][0]["risk"]["tone"], "bad")
        self.assertEqual(payload["bottlenecks"]["failed"][0]["risk"]["reason"], "Awaiting manual cleanup")
        self.assertEqual(payload["bottlenecks"]["failed"][0]["risk"]["nextAction"], "requeue")
        self.assertEqual(payload["bottlenecks"]["tempDone"][0]["latestEvent"]["eventType"], "TEMP_DONE")
        self.assertEqual(payload["bottlenecks"]["tempDone"][0]["risk"]["code"], "awaiting_verification")
        self.assertEqual(payload["bottlenecks"]["tempDone"][0]["risk"]["label"], "temp-done")
        self.assertEqual(payload["bottlenecks"]["tempDone"][0]["risk"]["tone"], "warn")
        self.assertEqual(payload["bottlenecks"]["tempDone"][0]["risk"]["reason"], "idempotencyKey=detail-temp")
        self.assertEqual(payload["bottlenecks"]["tempDone"][0]["risk"]["nextAction"], "wait_for_verify")
        self.assertEqual(payload["bottlenecks"]["manualAttention"][0]["id"], second_item.id)
        self.assertEqual(payload["bottlenecks"]["manualAttentionSummary"][0]["code"], "manual_cleanup")
        self.assertEqual(payload["bottlenecks"]["manualAttentionSummary"][0]["count"], 1)
        self.assertEqual(payload["bottlenecks"]["manualAttentionSummary"][0]["nextAction"], "requeue")
        self.assertEqual(payload["bottlenecks"]["manualAttentionSummary"][0]["firstWorkItemId"], second_item.id)
        self.assertEqual(payload["bottlenecks"]["manualAttentionSummary"][0]["firstActionableWorkItemId"], second_item.id)
        self.assertGreaterEqual(payload["bottlenecks"]["manualAttentionSummary"][0]["oldestAgeSeconds"], 0)
        self.assertEqual(payload["bottlenecks"]["tempDone"][0]["latestEvent"]["outcomeLabel"], "temp-done")
        self.assertEqual(payload["bottlenecks"]["tempDone"][0]["latestEvent"]["outcomeTone"], "warn")
        self.assertIn("idempotencyKey=detail-temp", payload["bottlenecks"]["tempDone"][0]["latestEvent"]["summary"])
        self.assertIsNone(payload["bottlenecks"]["tempDone"][0]["latestCleanupEvent"])
        self.assertGreaterEqual(len(payload["recentEvents"]), 3)
        self.assertEqual(payload["recentEventSources"]["total"], len(payload["recentEvents"]))
        self.assertEqual(payload["recentEventSources"]["recommended"], 0)
        self.assertEqual(payload["recentEventSources"]["manual"], 0)
        self.assertFalse(payload["staleWarning"])

    def test_director_project_detail_prioritizes_verify_miss_temp_done_items(self) -> None:
        second_job = PlanJob.objects.create(
            project=self.project,
            activity_id=11,
            blueprint_type_id=101,
            product_type_id=201,
            runs=2,
            expected_duration_s=180,
            level=2,
            is_advanced=True,
            params_hash="priority-hash-2",
        )
        third_job = PlanJob.objects.create(
            project=self.project,
            activity_id=12,
            blueprint_type_id=102,
            product_type_id=202,
            runs=1,
            expected_duration_s=180,
            level=3,
            is_advanced=False,
            params_hash="priority-hash-3",
        )
        self.service.dispatch_project(self.project)
        SyncRun.objects.create(kind="jobs", corporation_id=123, status="ok", finished_at=timezone.now())

        first_item = WorkItem.objects.get(plan_job=self.plan_job)
        second_item = WorkItem.objects.get(plan_job=second_job)
        third_item = WorkItem.objects.get(plan_job=third_job)

        self.service.claim_next(user=self.user)
        self.service.mark_temp_done(work_item=first_item, actor=self.user, idempotency_key="priority-temp-1")

        second_item.status = "temp_done"
        second_item.assigned_to = self.user
        second_item.save(update_fields=["status", "assigned_to", "updated_at"])
        WorkEvent.objects.create(
            work_item=second_item,
            actor=self.user,
            event_type="TEMP_DONE",
            details={"idempotencyKey": "priority-temp-2"},
        )
        WorkEvent.objects.create(
            work_item=second_item,
            actor=self.user,
            event_type="VERIFY_MISS",
            details={"reason": "No matching jobs sync evidence"},
        )

        third_item.status = "temp_done"
        third_item.assigned_to = self.user
        third_item.save(update_fields=["status", "assigned_to", "updated_at"])
        WorkEvent.objects.create(
            work_item=third_item,
            actor=self.user,
            event_type="TEMP_DONE",
            details={"idempotencyKey": "priority-temp-3"},
        )

        response = self.client.get(f"/api/projects/{self.project.id}/director-detail")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["bottlenecks"]["tempDone"]), 3)
        self.assertEqual(len(payload["bottlenecks"]["manualAttention"]), 1)
        self.assertEqual(len(payload["bottlenecks"]["manualAttentionSummary"]), 1)
        self.assertEqual(payload["bottlenecks"]["manualAttention"][0]["id"], second_item.id)
        self.assertEqual(payload["bottlenecks"]["tempDone"][0]["id"], second_item.id)
        self.assertEqual(payload["bottlenecks"]["tempDone"][0]["risk"]["code"], "verification_miss")
        self.assertEqual(payload["bottlenecks"]["tempDone"][0]["risk"]["label"], "verify miss")
        self.assertEqual(payload["bottlenecks"]["tempDone"][0]["risk"]["tone"], "bad")
        self.assertEqual(payload["bottlenecks"]["tempDone"][0]["risk"]["reason"], "reason=No matching jobs sync evidence")
        self.assertEqual(payload["bottlenecks"]["tempDone"][0]["risk"]["nextAction"], "retry_verify")
        self.assertEqual(payload["bottlenecks"]["manualAttentionSummary"][0]["code"], "verification_miss")
        self.assertEqual(payload["bottlenecks"]["manualAttentionSummary"][0]["count"], 1)
        self.assertEqual(payload["bottlenecks"]["manualAttentionSummary"][0]["nextAction"], "retry_verify")
        self.assertEqual(payload["bottlenecks"]["manualAttentionSummary"][0]["firstWorkItemId"], second_item.id)
        self.assertEqual(payload["bottlenecks"]["manualAttentionSummary"][0]["firstActionableWorkItemId"], second_item.id)
        self.assertGreaterEqual(payload["bottlenecks"]["manualAttentionSummary"][0]["oldestAgeSeconds"], 0)

    def test_director_project_detail_latest_cleanup_summary_shows_recommended_source(self) -> None:
        self.service.dispatch_project(self.project)
        work_item = self.service.claim_next(user=self.user)
        work_item = self.service.mark_temp_done(work_item=work_item, actor=self.user, idempotency_key="cleanup-source")
        WorkEvent.objects.create(
            work_item=work_item,
            actor=self.user,
            event_type="VERIFY_MISS",
            details={"reason": "No matching jobs sync evidence", "source": "recommended_action"},
        )

        response = self.client.get(f"/api/projects/{self.project.id}/director-detail")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        latest_cleanup = payload["bottlenecks"]["tempDone"][0]["latestCleanupEvent"]
        self.assertIsNotNone(latest_cleanup)
        self.assertEqual(latest_cleanup["source"], "recommended_action")
        self.assertIn("source=recommended_action", latest_cleanup["summary"])
        self.assertEqual(payload["recentEvents"][0]["source"], "recommended_action")
        self.assertEqual(payload["recentEventSources"]["recommended"], 1)
        self.assertEqual(payload["recentEventSources"]["manual"], 0)
        self.assertGreaterEqual(payload["recentEventSources"]["system"], 1)