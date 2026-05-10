from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("tickets", "0047_incidentreport_submission_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="incidentreport",
            name="correction_note",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="correction_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="correction_requested_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="incident_report_corrections_requested",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
