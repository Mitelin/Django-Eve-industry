from django.conf import settings
from django.db import models

from apps.common.models import TimeStampedModel


class Project(TimeStampedModel):
    STATUS_CHOICES = (
        ("draft", "draft"),
        ("active", "active"),
        ("paused", "paused"),
        ("done", "done"),
        ("archived", "archived"),
    )

    name = models.CharField(max_length=255)
    priority = models.IntegerField(default=3, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft", db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="projects")
    due_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-priority", "name"]

    def __str__(self) -> str:
        return self.name


class ProjectTarget(TimeStampedModel):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="targets")
    type_id = models.BigIntegerField(db_index=True)
    quantity = models.BigIntegerField(default=0)
    is_final_output = models.BooleanField(default=True)


class PlanJob(TimeStampedModel):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="plan_jobs")
    activity_id = models.IntegerField(db_index=True)
    blueprint_type_id = models.BigIntegerField(db_index=True)
    product_type_id = models.BigIntegerField(db_index=True)
    runs = models.IntegerField(default=0)
    expected_duration_s = models.IntegerField(default=0)
    level = models.IntegerField(default=0)
    probability = models.FloatField(null=True, blank=True)
    is_advanced = models.BooleanField(default=False)
    params_hash = models.CharField(max_length=128, db_index=True)


class PlanMaterial(TimeStampedModel):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="plan_materials")
    plan_job = models.ForeignKey(PlanJob, on_delete=models.CASCADE, related_name="materials", null=True, blank=True)
    material_type_id = models.BigIntegerField(db_index=True)
    quantity_total = models.BigIntegerField(default=0)
    activity_id = models.IntegerField(null=True, blank=True)
    level = models.IntegerField(default=0)
    is_input = models.BooleanField(default=True)
    is_intermediate = models.BooleanField(default=False)
