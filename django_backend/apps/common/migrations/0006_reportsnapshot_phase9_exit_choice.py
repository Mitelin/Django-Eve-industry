from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0005_rollbackevidence_rollbackevidenceevent"),
    ]

    operations = [
        migrations.AlterField(
            model_name="reportsnapshot",
            name="report_name",
            field=models.CharField(
                choices=[
                    ("shadow_summary", "shadow_summary"),
                    ("cutover_readiness", "cutover_readiness"),
                    ("cutover_pilot_readiness", "cutover_pilot_readiness"),
                    ("cutover_preflight", "cutover_preflight"),
                    ("cutover_phase9_exit", "cutover_phase9_exit"),
                ],
                db_index=True,
                max_length=50,
            ),
        ),
    ]