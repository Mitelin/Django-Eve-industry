from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0006_reportsnapshot_phase9_exit_choice"),
    ]

    operations = [
        migrations.CreateModel(
            name="SdeImportState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("source", models.CharField(default="ccp_sde", max_length=40, unique=True)),
                ("current_build_number", models.BigIntegerField(blank=True, db_index=True, null=True)),
                ("current_release_date", models.CharField(blank=True, max_length=40)),
                ("archive_sha256", models.CharField(blank=True, max_length=64)),
                ("archive_source_url", models.CharField(blank=True, max_length=500)),
                ("source_filename", models.CharField(blank=True, max_length=255)),
                ("last_checked_at", models.DateTimeField(blank=True, null=True)),
                ("last_imported_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["source"]},
        ),
        migrations.CreateModel(
            name="SdeImportRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("started", "started"),
                            ("skipped", "skipped"),
                            ("succeeded", "succeeded"),
                            ("failed", "failed"),
                            ("validation_failed", "validation_failed"),
                        ],
                        db_index=True,
                        default="started",
                        max_length=30,
                    ),
                ),
                ("source_type", models.CharField(default="url", max_length=20)),
                ("source_url", models.CharField(blank=True, max_length=500)),
                ("source_filename", models.CharField(blank=True, max_length=255)),
                ("archive_sha256", models.CharField(blank=True, max_length=64)),
                ("triggered_by", models.CharField(blank=True, max_length=120)),
                ("detected_build_number", models.BigIntegerField(blank=True, db_index=True, null=True)),
                ("detected_release_date", models.CharField(blank=True, max_length=40)),
                ("previous_build_number", models.BigIntegerField(blank=True, null=True)),
                ("imported_build_number", models.BigIntegerField(blank=True, null=True)),
                ("force_reimport", models.BooleanField(default=False)),
                ("table_counts", models.JSONField(default=dict)),
                ("notes", models.TextField(blank=True)),
                ("error_text", models.TextField(blank=True)),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
    ]