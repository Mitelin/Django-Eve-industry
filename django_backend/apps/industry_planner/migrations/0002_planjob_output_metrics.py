from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("industry_planner", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="planjob",
            name="duration_per_run_s",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="planjob",
            name="output_quantity_per_run",
            field=models.IntegerField(default=1),
        ),
    ]
