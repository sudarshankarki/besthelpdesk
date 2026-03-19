from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0005_ticket_resolution_fields"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="ticketmessage",
            index=models.Index(fields=["ticket", "created_at"], name="ticketmsg_ticket_created_idx"),
        ),
        migrations.AddIndex(
            model_name="ticketmessage",
            index=models.Index(fields=["created_at"], name="ticketmsg_created_idx"),
        ),
    ]

