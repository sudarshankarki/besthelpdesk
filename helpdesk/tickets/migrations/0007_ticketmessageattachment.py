from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0006_ticketmessage_indexes"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="TicketMessageAttachment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("object_key", models.CharField(max_length=512, unique=True)),
                ("filename", models.CharField(max_length=255)),
                ("content_type", models.CharField(blank=True, max_length=255)),
                ("size", models.BigIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("message", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="attachment", to="tickets.ticketmessage")),
                ("ticket", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="attachments", to="tickets.ticket")),
                ("uploaded_by", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ticket_attachments", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="ticketmessageattachment",
            index=models.Index(fields=["ticket", "created_at"], name="attach_ticket_created_idx"),
        ),
        migrations.AddIndex(
            model_name="ticketmessageattachment",
            index=models.Index(fields=["created_at"], name="attach_created_idx"),
        ),
    ]

