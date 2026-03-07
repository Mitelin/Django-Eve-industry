from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ReportSnapshot(TimeStampedModel):
    REPORT_NAME_CHOICES = (
        ("shadow_summary", "shadow_summary"),
        ("cutover_readiness", "cutover_readiness"),
        ("cutover_pilot_readiness", "cutover_pilot_readiness"),
        ("cutover_preflight", "cutover_preflight"),
    )

    snapshot_date = models.DateField(default=timezone.localdate, db_index=True)
    report_name = models.CharField(max_length=50, choices=REPORT_NAME_CHOICES, db_index=True)
    payload = models.JSONField(default=dict)
    incident_count = models.IntegerField(default=0)
    go_no_go = models.BooleanField(null=True, blank=True)

    class Meta:
        ordering = ["-snapshot_date", "report_name"]
        constraints = [
            models.UniqueConstraint(fields=["snapshot_date", "report_name"], name="uniq_daily_report_snapshot"),
        ]


class ScriptSignoff(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "pending"
        VALIDATED = "validated", "validated"
        BLOCKED = "blocked", "blocked"

    script_name = models.CharField(max_length=120, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    signed_off_by = models.CharField(max_length=120, blank=True)
    signed_off_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["script_name"]


class ScriptSignoffEvent(TimeStampedModel):
    signoff = models.ForeignKey(ScriptSignoff, on_delete=models.CASCADE, related_name="events")
    previous_status = models.CharField(max_length=20, choices=ScriptSignoff.Status.choices, blank=True)
    new_status = models.CharField(max_length=20, choices=ScriptSignoff.Status.choices)
    changed_by = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    effective_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-effective_at", "-id"]


class CutoverRoleAssignment(TimeStampedModel):
    role_name = models.CharField(max_length=80, unique=True)
    assigned_to = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    assigned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["role_name"]


class CutoverRoleEvent(TimeStampedModel):
    assignment = models.ForeignKey(CutoverRoleAssignment, on_delete=models.CASCADE, related_name="events")
    previous_assigned_to = models.CharField(max_length=120, blank=True)
    new_assigned_to = models.CharField(max_length=120, blank=True)
    changed_by = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    effective_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-effective_at", "-id"]
