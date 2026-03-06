from django.conf import settings
from django.db import models

from apps.common.models import TimeStampedModel


class WorkItem(TimeStampedModel):
    KIND_CHOICES = (
        ("start_job", "start_job"),
    )
    STATUS_CHOICES = (
        ("ready", "ready"),
        ("assigned", "assigned"),
        ("temp_done", "temp_done"),
        ("verified", "verified"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
    )

    project = models.ForeignKey("industry_planner.Project", on_delete=models.CASCADE, related_name="work_items")
    plan_job = models.ForeignKey(
        "industry_planner.PlanJob",
        on_delete=models.CASCADE,
        related_name="work_items",
        null=True,
        blank=True,
    )
    kind = models.CharField(max_length=30, choices=KIND_CHOICES, db_index=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="ready", db_index=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_work_items",
    )
    locked_until = models.DateTimeField(null=True, blank=True, db_index=True)
    attempt = models.IntegerField(default=0)
    priority_score = models.IntegerField(default=0, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    version = models.IntegerField(default=1)

    class Meta:
        indexes = [
            models.Index(fields=["status", "priority_score", "created_at"]),
            models.Index(fields=["assigned_to", "status"]),
        ]


class WorkEvent(TimeStampedModel):
    work_item = models.ForeignKey(WorkItem, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=50, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_events",
    )
    details = models.JSONField(default=dict, blank=True)
