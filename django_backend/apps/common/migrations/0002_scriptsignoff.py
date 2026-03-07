from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ScriptSignoff",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("script_name", models.CharField(max_length=120, unique=True)),
                (
                    "status",
                    models.CharField(
                        choices=[("pending", "pending"), ("validated", "validated"), ("blocked", "blocked")],
                        db_index=True,
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("signed_off_by", models.CharField(blank=True, max_length=120)),
                ("signed_off_at", models.DateTimeField(blank=True, null=True)),
                ("notes", models.TextField(blank=True)),
            ],
            options={
                "ordering": ["script_name"],
            },
        ),
    ]