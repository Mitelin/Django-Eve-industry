from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.accounts.models import Character
from apps.corp_sync.models import CorpJobSnapshot
from apps.corp_sync.services import SyncCoordinator
from apps.industry_planner.models import PlanJob, Project
from apps.workforce.models import WorkEvent, WorkItem


class WorkforceError(RuntimeError):
    pass


class NoAvailableWorkItem(WorkforceError):
    pass


class InvalidWorkItemTransition(WorkforceError):
    pass


class VerificationWindowOpen(WorkforceError):
    pass


class StaleVerificationData(WorkforceError):
    pass


@dataclass(frozen=True)
class ProjectProgress:
    project_id: int
    total: int
    ready: int
    assigned: int
    temp_done: int
    verified: int
    failed: int
    cancelled: int


@dataclass(frozen=True)
class ProjectFreshness:
    corporation_id: int | None
    last_success_at: str | None
    age_seconds: int | None
    is_stale: bool


class WorkforceService:
    def __init__(
        self,
        *,
        sync_coordinator: SyncCoordinator | None = None,
        verification_stale_after: timedelta = timedelta(hours=1),
        max_attempts: int = 5,
    ) -> None:
        self.sync_coordinator = sync_coordinator or SyncCoordinator()
        self.verification_stale_after = verification_stale_after
        self.max_attempts = max_attempts

    def dispatch_project(self, project: Project) -> list[WorkItem]:
        active_plan_job_ids = list(project.plan_jobs.values_list("id", flat=True))
        obsolete_items = project.work_items.exclude(plan_job_id__in=active_plan_job_ids)
        obsolete_items.exclude(status__in=["verified", "cancelled"]).update(status="cancelled", locked_until=None)

        dispatched: list[WorkItem] = []
        for plan_job in project.plan_jobs.order_by("id"):
            payload = self._build_payload(plan_job)
            work_item, created = WorkItem.objects.get_or_create(
                project=project,
                plan_job=plan_job,
                kind="start_job",
                defaults={
                    "status": "ready",
                    "priority_score": self._priority_score(project, plan_job),
                    "payload": payload,
                },
            )
            if created:
                self._emit_event(work_item, "DISPATCHED", None, payload)
            elif work_item.status not in {"assigned", "temp_done", "verified"}:
                changed = False
                new_priority = self._priority_score(project, plan_job)
                if work_item.priority_score != new_priority:
                    work_item.priority_score = new_priority
                    changed = True
                if work_item.payload != payload:
                    work_item.payload = payload
                    changed = True
                if work_item.status == "cancelled":
                    work_item.status = "ready"
                    changed = True
                if changed:
                    work_item.version += 1
                    work_item.save(update_fields=["priority_score", "payload", "status", "version", "updated_at"])
                    self._emit_event(work_item, "REDISPATCHED", None, payload)
            dispatched.append(work_item)
        return dispatched

    def claim_next(self, *, user: Any, lock_ttl: timedelta = timedelta(minutes=15)) -> WorkItem:
        with transaction.atomic():
            work_item = (
                WorkItem.objects.select_for_update()
                .filter(status="ready")
                .order_by("-priority_score", "created_at", "id")
                .first()
            )
            if work_item is None:
                raise NoAvailableWorkItem("No READY work items available")

            now = timezone.now()
            work_item.status = "assigned"
            work_item.assigned_to = user
            work_item.locked_until = now + lock_ttl
            work_item.attempt += 1
            work_item.version += 1
            work_item.save(update_fields=["status", "assigned_to", "locked_until", "attempt", "version", "updated_at"])
            payload = dict(work_item.payload)
            payload["assignedAt"] = now.isoformat()
            work_item.payload = payload
            work_item.save(update_fields=["payload", "updated_at"])
            self._emit_event(work_item, "CLAIMED", user, {"lockUntil": work_item.locked_until.isoformat()})
            return work_item

    def release(self, *, work_item: WorkItem, actor: Any, reason: str = "manual_release") -> WorkItem:
        with transaction.atomic():
            locked = WorkItem.objects.select_for_update().get(pk=work_item.pk)
            if locked.status != "assigned":
                raise InvalidWorkItemTransition("Only ASSIGNED work items can be released")
            if locked.assigned_to_id != actor.id:
                raise InvalidWorkItemTransition("Work item is assigned to a different user")

            locked.status = "ready"
            locked.assigned_to = None
            locked.locked_until = None
            locked.version += 1
            locked.save(update_fields=["status", "assigned_to", "locked_until", "version", "updated_at"])
            self._emit_event(locked, "RELEASED", actor, {"reason": reason})
            return locked

    def director_requeue(self, *, work_item: WorkItem, reason: str = "director_requeue", source: str | None = None) -> WorkItem:
        with transaction.atomic():
            locked = WorkItem.objects.select_for_update().get(pk=work_item.pk)
            if locked.status not in {"assigned", "temp_done", "failed"}:
                raise InvalidWorkItemTransition("Only ASSIGNED, TEMP_DONE, or FAILED work items can be requeued")

            previous_status = locked.status
            previous_assigned_to_id = locked.assigned_to_id
            payload = dict(locked.payload)
            payload.pop("tempDoneKey", None)
            payload.pop("tempDoneAt", None)

            locked.status = "ready"
            locked.assigned_to = None
            locked.locked_until = None
            locked.verified_at = None
            locked.payload = payload
            locked.version += 1
            locked.save(
                update_fields=["status", "assigned_to", "locked_until", "verified_at", "payload", "version", "updated_at"]
            )
            self._emit_event(
                locked,
                "DIRECTOR_REQUEUED",
                None,
                {
                    "reason": reason,
                    "source": source,
                    "previousStatus": previous_status,
                    "previousAssignedToUserId": previous_assigned_to_id,
                },
            )
            return locked

    def director_release(self, *, work_item: WorkItem, reason: str = "director_release", source: str | None = None) -> WorkItem:
        with transaction.atomic():
            locked = WorkItem.objects.select_for_update().get(pk=work_item.pk)
            if locked.status != "assigned":
                raise InvalidWorkItemTransition("Only ASSIGNED work items can be director-released")

            previous_assigned_to_id = locked.assigned_to_id
            locked.status = "ready"
            locked.assigned_to = None
            locked.locked_until = None
            locked.version += 1
            locked.save(update_fields=["status", "assigned_to", "locked_until", "version", "updated_at"])
            self._emit_event(
                locked,
                "DIRECTOR_RELEASED",
                None,
                {
                    "reason": reason,
                    "source": source,
                    "previousAssignedToUserId": previous_assigned_to_id,
                },
            )
            return locked

    def mark_temp_done(self, *, work_item: WorkItem, actor: Any, idempotency_key: str) -> WorkItem:
        with transaction.atomic():
            locked = WorkItem.objects.select_for_update().get(pk=work_item.pk)
            if locked.status == "temp_done" and locked.payload.get("tempDoneKey") == idempotency_key:
                return locked
            if locked.status != "assigned":
                raise InvalidWorkItemTransition("Only ASSIGNED work items can move to TEMP_DONE")
            if locked.assigned_to_id != actor.id:
                raise InvalidWorkItemTransition("Work item is assigned to a different user")

            payload = dict(locked.payload)
            payload["tempDoneKey"] = idempotency_key
            payload["tempDoneAt"] = timezone.now().isoformat()
            locked.payload = payload
            locked.status = "temp_done"
            locked.version += 1
            locked.save(update_fields=["payload", "status", "version", "updated_at"])
            self._emit_event(locked, "TEMP_DONE", actor, {"idempotencyKey": idempotency_key})
            return locked

    def expire_locks(self, *, now: object | None = None) -> int:
        now = now or timezone.now()
        expired_ids = list(
            WorkItem.objects.filter(status="assigned", locked_until__lt=now).values_list("id", flat=True)
        )
        if not expired_ids:
            return 0

        updated = WorkItem.objects.filter(id__in=expired_ids).update(
            status="ready",
            assigned_to=None,
            locked_until=None,
            version=1,
        )
        for work_item in WorkItem.objects.filter(id__in=expired_ids):
            self._emit_event(work_item, "LOCK_EXPIRED", None, {"expiredAt": now.isoformat()})
        return updated

    def verify_batch(self, *, now: object | None = None, sla: timedelta = timedelta(minutes=30)) -> dict[str, int]:
        now = now or timezone.now()
        verified = 0
        requeued = 0
        stale = 0
        escalated = 0
        temp_done_items = WorkItem.objects.filter(status="temp_done").select_related("assigned_to", "plan_job")
        for work_item in temp_done_items:
            try:
                if self.verify_start_job(work_item=work_item, now=now, sla=sla):
                    verified += 1
            except VerificationWindowOpen:
                continue
            except StaleVerificationData:
                stale += 1
            else:
                current_status = WorkItem.objects.get(pk=work_item.pk).status
                if current_status == "ready":
                    requeued += 1
                elif current_status == "failed":
                    escalated += 1
        return {"verified": verified, "requeued": requeued, "stale": stale, "escalated": escalated}

    def verify_start_job(
        self,
        *,
        work_item: WorkItem,
        now: object | None = None,
        sla: timedelta = timedelta(minutes=30),
        source: str | None = None,
    ) -> bool:
        now = now or timezone.now()
        if work_item.status != "temp_done":
            raise InvalidWorkItemTransition("Only TEMP_DONE work items can be verified")

        self._ensure_fresh_job_sync(work_item)

        assignment_time = self._parse_payload_datetime(work_item.payload.get("assignedAt")) or work_item.updated_at
        deadline = assignment_time + sla
        if now < deadline:
            match = self._find_matching_job(work_item, assignment_time)
            if match is None:
                raise VerificationWindowOpen("Verification SLA window is still open")
            return self._mark_verified(work_item, match, now, source=source)

        match = self._find_matching_job(work_item, assignment_time)
        if match is not None:
            return self._mark_verified(work_item, match, now, source=source)
        self._mark_failed_and_requeue(work_item, now, source=source)
        return False

    def get_my_active(self, *, user: Any) -> WorkItem | None:
        return (
            WorkItem.objects.filter(assigned_to=user, status__in=["assigned", "temp_done"])
            .order_by("-updated_at", "-id")
            .first()
        )

    def get_queue(self, *, limit: int = 20) -> list[WorkItem]:
        return list(WorkItem.objects.filter(status="ready").order_by("-priority_score", "created_at", "id")[:limit])

    def get_project_progress(self, *, project: Project) -> ProjectProgress:
        counts = project.work_items.values("status").annotate(count=Count("id"))
        data = {entry["status"]: entry["count"] for entry in counts}
        return ProjectProgress(
            project_id=project.id,
            total=sum(data.values()),
            ready=data.get("ready", 0),
            assigned=data.get("assigned", 0),
            temp_done=data.get("temp_done", 0),
            verified=data.get("verified", 0),
            failed=data.get("failed", 0),
            cancelled=data.get("cancelled", 0),
        )

    def get_project_jobs_freshness(self, *, project: Project) -> ProjectFreshness:
        corporation_id = self._get_project_corporation_id(project)
        if corporation_id is None:
            return ProjectFreshness(corporation_id=None, last_success_at=None, age_seconds=None, is_stale=True)

        freshness = self.sync_coordinator.get_freshness(
            "jobs",
            corporation_id,
            stale_after=self.verification_stale_after,
        )
        return ProjectFreshness(
            corporation_id=corporation_id,
            last_success_at=freshness.last_success_at.isoformat().replace("+00:00", "Z")
            if freshness.last_success_at
            else None,
            age_seconds=freshness.age_seconds,
            is_stale=freshness.is_stale,
        )

    def _find_matching_job(self, work_item: WorkItem, assignment_time) -> CorpJobSnapshot | None:
        expected = work_item.payload
        assigned_character_ids = list(
            Character.objects.filter(user_id=work_item.assigned_to_id).values_list("eve_character_id", flat=True)
        )
        if not assigned_character_ids:
            return None

        return (
            CorpJobSnapshot.objects.filter(
                installer_id__in=assigned_character_ids,
                activity_id=int(expected.get("expectedActivityId") or 0),
                blueprint_type_id=int(expected.get("expectedBlueprintTypeId") or 0),
                product_type_id=int(expected.get("expectedProductTypeId") or 0),
                runs=int(expected.get("expectedRuns") or 0),
                start_date__gte=assignment_time - timedelta(minutes=5),
                start_date__lte=assignment_time + timedelta(minutes=30),
            )
            .order_by("start_date", "id")
            .first()
        )

    def _mark_verified(self, work_item: WorkItem, match: CorpJobSnapshot, now, *, source: str | None = None) -> bool:
        with transaction.atomic():
            locked = WorkItem.objects.select_for_update().get(pk=work_item.pk)
            if locked.status != "temp_done":
                return False
            locked.status = "verified"
            locked.verified_at = now
            locked.locked_until = None
            locked.version += 1
            locked.save(update_fields=["status", "verified_at", "locked_until", "version", "updated_at"])
            self._emit_event(locked, "VERIFIED_OK", None, {"matchedJobId": match.job_id, "source": source})
            return True

    def _mark_failed_and_requeue(self, work_item: WorkItem, now, *, source: str | None = None) -> None:
        with transaction.atomic():
            locked = WorkItem.objects.select_for_update().get(pk=work_item.pk)
            if locked.status != "temp_done":
                return
            locked.status = "failed"
            locked.version += 1
            locked.save(update_fields=["status", "version", "updated_at"])
            self._emit_event(locked, "VERIFY_MISS", None, {"failedAt": now.isoformat(), "source": source})
            if int(locked.attempt or 0) >= self.max_attempts:
                self._emit_event(
                    locked,
                    "ESCALATED",
                    None,
                    {"reason": "retry_cap_reached", "attempt": locked.attempt, "source": source},
                )
                return
            locked.status = "ready"
            locked.assigned_to = None
            locked.locked_until = None
            locked.version += 1
            locked.save(update_fields=["status", "assigned_to", "locked_until", "version", "updated_at"])
            self._emit_event(locked, "REQUEUED", None, {"reason": "verify_miss", "source": source})

    def _ensure_fresh_job_sync(self, work_item: WorkItem) -> None:
        corporation_id = self._get_assigned_corporation_id(work_item)
        if corporation_id is None:
            raise StaleVerificationData("Cannot verify without assigned character corporation context")
        freshness = self.sync_coordinator.get_freshness(
            "jobs",
            corporation_id,
            stale_after=self.verification_stale_after,
        )
        if freshness.is_stale:
            raise StaleVerificationData("Jobs sync data is stale for verification")

    @staticmethod
    def _get_project_corporation_id(project: Project) -> int | None:
        character = (
            Character.objects.filter(user_id=project.created_by_id)
            .order_by("-is_main", "id")
            .first()
        )
        if character is None:
            return None
        return int(character.corporation_id)

    @staticmethod
    def _get_assigned_corporation_id(work_item: WorkItem) -> int | None:
        character = (
            Character.objects.filter(user_id=work_item.assigned_to_id)
            .order_by("-is_main", "id")
            .first()
        )
        if character is None:
            return None
        return int(character.corporation_id)

    @staticmethod
    def _build_payload(plan_job: PlanJob) -> dict[str, Any]:
        return {
            "expectedActivityId": plan_job.activity_id,
            "expectedBlueprintTypeId": plan_job.blueprint_type_id,
            "expectedProductTypeId": plan_job.product_type_id,
            "expectedRuns": plan_job.runs,
            "expectedDurationS": plan_job.expected_duration_s,
        }

    @staticmethod
    def _priority_score(project: Project, plan_job: PlanJob) -> int:
        return int(project.priority) * 1_000_000 - int(plan_job.expected_duration_s or 0)

    @staticmethod
    def _emit_event(work_item: WorkItem, event_type: str, actor: Any, details: dict[str, Any]) -> WorkEvent:
        return WorkEvent.objects.create(work_item=work_item, event_type=event_type, actor=actor, details=details)

    @staticmethod
    def _parse_payload_datetime(value: Any):
        if not value:
            return None
        return timezone.datetime.fromisoformat(value.replace("Z", "+00:00"))