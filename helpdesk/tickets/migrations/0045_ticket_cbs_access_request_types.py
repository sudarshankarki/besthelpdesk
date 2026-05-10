from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0044_incidentreport_commander_user"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ticket",
            name="request_type",
            field=models.CharField(
                choices=[
                    ("incident", "Incident"),
                    ("service", "Service Request"),
                    ("access", "Access Request"),
                    ("cbs_access_ho", "CBS Access Request (Head Office)"),
                    ("cbs_access_branch", "CBS Access Request (Branch)"),
                    ("change", "Change"),
                ],
                default="incident",
                max_length=20,
            ),
        ),
    ]
