from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0004_cutoverroleassignment_cutoverroleevent"),
    ]

    operations = [
        migrations.CreateModel(
            name="RollbackEvidence",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("evidence_type", models.CharField(choices=[("runbook_review", "runbook_review"), ("rollback_drill", "rollback_drill")], max_length=40, unique=True)),
                ("evidence_date", models.DateField(blank=True, db_index=True, null=True)),
                ("recorded_by", models.CharField(blank=True, max_length=120)),
                ("notes", models.TextField(blank=True)),
            ],
            options={"ordering": ["evidence_type"]},
        ),
        migrations.CreateModel(
            name="RollbackEvidenceEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("previous_evidence_date", models.DateField(blank=True, null=True)),
                ("new_evidence_date", models.DateField(blank=True, null=True)),
                ("changed_by", models.CharField(blank=True, max_length=120)),
                ("notes", models.TextField(blank=True)),
                ("effective_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("evidence", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="events", to="common.rollbackevidence")),
            ],
            options={"ordering": ["-effective_at", "-id"]},
        ),
    ]