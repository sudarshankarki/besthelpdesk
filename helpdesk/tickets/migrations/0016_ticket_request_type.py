from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0015_ticket_impact_urgency_priority"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="request_type",
            field=models.CharField(
                choices=[
                    ("incident", "Incident"),
                    ("service", "Service Request"),
                    ("access", "Access Request"),
                    ("change", "Change"),
                ],
                default="incident",
                max_length=20,
            ),
        ),
    ]

