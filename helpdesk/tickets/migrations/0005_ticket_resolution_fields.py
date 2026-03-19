from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0004_ticketmessage"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="closed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ticket",
            name="resolved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
