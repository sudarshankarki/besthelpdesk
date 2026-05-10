from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0026_ticket_last_unresolved_reminder_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="TicketReminderSummaryLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sent_at", models.DateTimeField()),
                ("ticket_count", models.PositiveIntegerField(default=0)),
                (
                    "assignee",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ticket_reminder_summary_logs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-sent_at"],
            },
        ),
        migrations.AddIndex(
            model_name="ticketremindersummarylog",
            index=models.Index(fields=["assignee", "sent_at"], name="ticketrem_assignee_sent_idx"),
        ),
    ]
