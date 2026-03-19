from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0021_remove_ticket_client_ip"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="TicketChatReadState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("last_seen_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "ticket",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="chat_read_states",
                        to="tickets.ticket",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ticket_chat_read_states",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["ticket", "user"], name="ticketchatread_ticket_user_idx"),
                    models.Index(fields=["user", "last_seen_at"], name="ticketchatread_user_seen_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("ticket", "user"), name="ticketchatread_ticket_user_uniq"),
                ],
            },
        ),
    ]
