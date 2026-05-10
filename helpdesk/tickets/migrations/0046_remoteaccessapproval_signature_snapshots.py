from django.db import migrations, models

import tickets.models
import tickets.storage


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0045_ticket_cbs_access_request_types"),
    ]

    operations = [
        migrations.AddField(
            model_name="remoteaccessapproval",
            name="requested_signature_snapshot",
            field=models.ImageField(
                blank=True,
                null=True,
                storage=tickets.storage.TicketImageStorage(),
                upload_to=tickets.models.remote_access_signature_snapshot_upload_to,
            ),
        ),
        migrations.AddField(
            model_name="remoteaccessapproval",
            name="access_user_signature_snapshot",
            field=models.ImageField(
                blank=True,
                null=True,
                storage=tickets.storage.TicketImageStorage(),
                upload_to=tickets.models.remote_access_signature_snapshot_upload_to,
            ),
        ),
        migrations.AddField(
            model_name="remoteaccessapproval",
            name="recommended_signature_snapshot",
            field=models.ImageField(
                blank=True,
                null=True,
                storage=tickets.storage.TicketImageStorage(),
                upload_to=tickets.models.remote_access_signature_snapshot_upload_to,
            ),
        ),
        migrations.AddField(
            model_name="remoteaccessapproval",
            name="approved_signature_snapshot",
            field=models.ImageField(
                blank=True,
                null=True,
                storage=tickets.storage.TicketImageStorage(),
                upload_to=tickets.models.remote_access_signature_snapshot_upload_to,
            ),
        ),
    ]
