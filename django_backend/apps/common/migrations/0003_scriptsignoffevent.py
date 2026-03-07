from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0002_scriptsignoff"),
    ]

    operations = [
        migrations.CreateModel(
            name="ScriptSignoffEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "previous_status",
                    models.CharField(
                        blank=True,
                        choices=[("pending", "pending"), ("validated", "validated"), ("blocked", "blocked")],
                        max_length=20,
                    ),
                ),
                (
                    "new_status",
                    models.CharField(
                        choices=[("pending", "pending"), ("validated", "validated"), ("blocked", "blocked")],
                        max_length=20,
                    ),
                ),
                ("changed_by", models.CharField(blank=True, max_length=120)),
                ("notes", models.TextField(blank=True)),
                ("effective_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("signoff", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="events", to="common.scriptsignoff")),
            ],
            options={
                "ordering": ["-effective_at", "-id"],
            },
        ),
    ]