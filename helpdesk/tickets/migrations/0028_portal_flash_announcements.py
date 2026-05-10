from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

import tickets.models
import tickets.storage


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0027_ticketremindersummarylog"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PortalFlashAnnouncement",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=255)),
                ("message", models.TextField(blank=True, default="")),
                (
                    "image",
                    models.ImageField(
                        storage=tickets.storage.TicketImageStorage(),
                        upload_to=tickets.models.portal_flash_image_upload_to,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="portal_flash_announcements",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="portalflashannouncement",
            index=models.Index(fields=["created_at"], name="portal_flash_created_idx"),
        ),
        migrations.CreateModel(
            name="PortalFlashAnnouncementReceipt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("shown_at", models.DateTimeField(auto_now_add=True)),
                (
                    "announcement",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="receipts",
                        to="tickets.portalflashannouncement",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="portal_flash_announcement_receipts",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-shown_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="portalflashannouncementreceipt",
            index=models.Index(fields=["user", "shown_at"], name="portal_flash_user_idx"),
        ),
        migrations.AddConstraint(
            model_name="portalflashannouncementreceipt",
            constraint=models.UniqueConstraint(
                fields=("user", "announcement"),
                name="portal_flash_receipt_unique",
            ),
        ),
    ]
