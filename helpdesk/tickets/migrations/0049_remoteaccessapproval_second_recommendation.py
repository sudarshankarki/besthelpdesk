from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import tickets.models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("tickets", "0048_incidentreport_correction_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="remoteaccessapproval",
            name="second_recommender",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="remote_access_approvals_to_second_recommend",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="remoteaccessapproval",
            name="second_recommendation_note",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="remoteaccessapproval",
            name="second_recommended_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="remoteaccessapproval",
            name="second_recommended_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="remote_access_approvals_second_recommended",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="remoteaccessapproval",
            name="second_recommended_signature_snapshot",
            field=models.ImageField(
                blank=True,
                null=True,
                storage=tickets.models.TicketImageStorage(),
                upload_to=tickets.models.remote_access_signature_snapshot_upload_to,
            ),
        ),
    ]
