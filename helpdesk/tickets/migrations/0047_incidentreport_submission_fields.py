from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("tickets", "0046_remoteaccessapproval_signature_snapshots"),
    ]

    operations = [
        migrations.AddField(
            model_name="incidentreport",
            name="submitted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="submitted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="incident_reports_submitted",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
