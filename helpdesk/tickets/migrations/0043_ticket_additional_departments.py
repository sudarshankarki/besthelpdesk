from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0042_incidentreport_action_plan_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="additional_departments",
            field=models.TextField(blank=True, default=""),
        ),
    ]
