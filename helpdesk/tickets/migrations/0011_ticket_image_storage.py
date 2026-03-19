from django.db import migrations, models

import tickets.models
import tickets.storage


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0010_ticket_closed_by"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ticket",
            name="image",
            field=models.ImageField(
                blank=True,
                null=True,
                storage=tickets.storage.TicketImageStorage(),
                upload_to=tickets.models.ticket_image_upload_to,
            ),
        ),
    ]

