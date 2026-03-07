from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0003_scriptsignoffevent"),
    ]

    operations = [
        migrations.CreateModel(
            name="CutoverRoleAssignment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("role_name", models.CharField(max_length=80, unique=True)),
                ("assigned_to", models.CharField(blank=True, max_length=120)),
                ("notes", models.TextField(blank=True)),
                ("assigned_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "ordering": ["role_name"],
            },
        ),
        migrations.CreateModel(
            name="CutoverRoleEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("previous_assigned_to", models.CharField(blank=True, max_length=120)),
                ("new_assigned_to", models.CharField(blank=True, max_length=120)),
                ("changed_by", models.CharField(blank=True, max_length=120)),
                ("notes", models.TextField(blank=True)),
                ("effective_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("assignment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="events", to="common.cutoverroleassignment")),
            ],
            options={
                "ordering": ["-effective_at", "-id"],
            },
        ),
    ]