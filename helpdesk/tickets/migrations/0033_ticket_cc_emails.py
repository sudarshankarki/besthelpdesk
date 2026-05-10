from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0032_backfill_ticketassignmentlog_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="cc_emails",
            field=models.TextField(blank=True, default=""),
        ),
    ]
