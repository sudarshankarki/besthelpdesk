from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0025_ticket_resolved_by"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="last_unresolved_reminder_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
