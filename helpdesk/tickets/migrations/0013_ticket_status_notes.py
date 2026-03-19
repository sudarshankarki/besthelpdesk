from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0012_group_mailbox_email"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="resolved_note",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="ticket",
            name="closed_note",
            field=models.TextField(blank=True, default=""),
        ),
    ]

