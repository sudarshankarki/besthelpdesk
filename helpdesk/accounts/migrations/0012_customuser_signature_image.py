from django.db import migrations, models
import accounts.models
import tickets.storage


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0011_authenticationsettings_agent_workload_view"),
    ]

    operations = [
        migrations.AddField(
            model_name="customuser",
            name="signature_image",
            field=models.ImageField(
                blank=True,
                null=True,
                storage=tickets.storage.TicketImageStorage(),
                upload_to=accounts.models.user_signature_upload_to,
            ),
        ),
    ]
