from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="ReportSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("snapshot_date", models.DateField(db_index=True, default=django.utils.timezone.localdate)),
                ("report_name", models.CharField(choices=[("shadow_summary", "shadow_summary"), ("cutover_readiness", "cutover_readiness")], db_index=True, max_length=50)),
                ("payload", models.JSONField(default=dict)),
                ("incident_count", models.IntegerField(default=0)),
                ("go_no_go", models.BooleanField(blank=True, null=True)),
            ],
            options={
                "ordering": ["-snapshot_date", "report_name"],
            },
        ),
        migrations.AddConstraint(
            model_name="reportsnapshot",
            constraint=models.UniqueConstraint(fields=("snapshot_date", "report_name"), name="uniq_daily_report_snapshot"),
        ),
    ]