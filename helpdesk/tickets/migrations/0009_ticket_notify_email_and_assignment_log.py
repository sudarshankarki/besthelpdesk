from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0008_ticket_department"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="notify_email",
            field=models.EmailField(blank=True, default="", max_length=254),
        ),
        migrations.CreateModel(
            name="TicketAssignmentLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("assigned_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("unassigned_at", models.DateTimeField(blank=True, null=True)),
                (
                    "assigned_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ticket_assignments_made",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "assigned_to",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ticket_assignment_logs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "ticket",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="assignment_logs",
                        to="tickets.ticket",
                    ),
                ),
            ],
            options={
                "ordering": ["-assigned_at"],
            },
        ),
        migrations.AddIndex(
            model_name="ticketassignmentlog",
            index=models.Index(fields=["ticket", "assigned_at"], name="assign_ticket_assigned_idx"),
        ),
        migrations.AddIndex(
            model_name="ticketassignmentlog",
            index=models.Index(fields=["assigned_to", "assigned_at"], name="assign_assignee_assigned_idx"),
        ),
        migrations.AddIndex(
            model_name="ticketassignmentlog",
            index=models.Index(fields=["unassigned_at"], name="assign_unassigned_idx"),
        ),
    ]

