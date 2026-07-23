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
        ("cutover_phase9_exit", "cutover_phase9_exit"),
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


class RollbackEvidence(TimeStampedModel):
    class EvidenceType(models.TextChoices):
        RUNBOOK_REVIEW = "runbook_review", "runbook_review"
        ROLLBACK_DRILL = "rollback_drill", "rollback_drill"

    evidence_type = models.CharField(max_length=40, choices=EvidenceType.choices, unique=True)
    evidence_date = models.DateField(null=True, blank=True, db_index=True)
    recorded_by = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["evidence_type"]


class RollbackEvidenceEvent(TimeStampedModel):
    evidence = models.ForeignKey(RollbackEvidence, on_delete=models.CASCADE, related_name="events")
    previous_evidence_date = models.DateField(null=True, blank=True)
    new_evidence_date = models.DateField(null=True, blank=True)
    changed_by = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    effective_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-effective_at", "-id"]


class SdeImportState(TimeStampedModel):
    source = models.CharField(max_length=40, unique=True, default="ccp_sde")
    current_build_number = models.BigIntegerField(null=True, blank=True, db_index=True)
    current_release_date = models.CharField(max_length=40, blank=True)
    archive_sha256 = models.CharField(max_length=64, blank=True)
    archive_source_url = models.CharField(max_length=500, blank=True)
    source_filename = models.CharField(max_length=255, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_imported_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["source"]


class SdeImportRun(TimeStampedModel):
    class Status(models.TextChoices):
        STARTED = "started", "started"
        SKIPPED = "skipped", "skipped"
        SUCCEEDED = "succeeded", "succeeded"
        FAILED = "failed", "failed"
        VALIDATION_FAILED = "validation_failed", "validation_failed"

    status = models.CharField(max_length=30, choices=Status.choices, default=Status.STARTED, db_index=True)
    source_type = models.CharField(max_length=20, default="url")
    source_url = models.CharField(max_length=500, blank=True)
    source_filename = models.CharField(max_length=255, blank=True)
    archive_sha256 = models.CharField(max_length=64, blank=True)
    triggered_by = models.CharField(max_length=120, blank=True)
    detected_build_number = models.BigIntegerField(null=True, blank=True, db_index=True)
    detected_release_date = models.CharField(max_length=40, blank=True)
    previous_build_number = models.BigIntegerField(null=True, blank=True)
    imported_build_number = models.BigIntegerField(null=True, blank=True)
    force_reimport = models.BooleanField(default=False)
    table_counts = models.JSONField(default=dict)
    notes = models.TextField(blank=True)
    error_text = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
