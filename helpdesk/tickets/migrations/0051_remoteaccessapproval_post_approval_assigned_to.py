from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0050_alter_incidentreport_notified_signature_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="remoteaccessapproval",
            name="post_approval_assigned_to",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="cbs_access_requests_to_receive_after_approval",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
