from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def migrate_remote_access_pending_statuses(apps, schema_editor):
    RemoteAccessApproval = apps.get_model("tickets", "RemoteAccessApproval")
    RemoteAccessApproval.objects.filter(status="pending").update(status="pending_approval")


def reverse_remote_access_pending_statuses(apps, schema_editor):
    RemoteAccessApproval = apps.get_model("tickets", "RemoteAccessApproval")
    RemoteAccessApproval.objects.filter(status__in=["pending_approval", "pending_recommendation"]).update(status="pending")


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("tickets", "0035_ticket_submission_token"),
    ]

    operations = [
        migrations.AddField(
            model_name="remoteaccessapproval",
            name="recommendation_note",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="remoteaccessapproval",
            name="recommended_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="remoteaccessapproval",
            name="recommended_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="remote_access_approvals_recommended",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="remoteaccessapproval",
            name="recommender",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="remote_access_approvals_to_recommend",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="remoteaccessapproval",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending_recommendation", "Pending Recommendation"),
                    ("pending_approval", "Pending Approval"),
                    ("approved", "Approved"),
                    ("rejected", "Rejected"),
                ],
                default="pending_approval",
                max_length=32,
            ),
        ),
        migrations.RunPython(
            migrate_remote_access_pending_statuses,
            reverse_remote_access_pending_statuses,
        ),
    ]
