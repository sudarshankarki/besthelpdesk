from django.db import migrations, models
from django.utils import timezone

import tickets.models


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0028_portal_flash_announcements"),
    ]

    operations = [
        migrations.AddField(
            model_name="portalflashannouncement",
            name="category",
            field=models.CharField(
                choices=[("it", "IT Related"), ("bank", "Bank Related")],
                default="it",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="portalflashannouncement",
            name="starts_at",
            field=models.DateTimeField(default=timezone.now),
        ),
        migrations.AddField(
            model_name="portalflashannouncement",
            name="ends_at",
            field=models.DateTimeField(default=tickets.models.default_portal_flash_end_at),
        ),
    ]
