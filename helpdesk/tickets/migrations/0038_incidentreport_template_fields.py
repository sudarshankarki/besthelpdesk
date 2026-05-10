from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import tickets.models
import tickets.storage


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("tickets", "0037_incidentreport"),
    ]

    operations = [
        migrations.AddField(
            model_name="incidentreport",
            name="communication_latest_update",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="communication_stakeholders",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="communication_update_frequency",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="containment_actions",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="incident_reports_created",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="current_status",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="detected_at",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="eradication_fix_applied",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="eradication_root_cause",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="eradication_systems_restored",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="eradication_validation_steps",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="escalations_raised",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="evidence_attachments",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="evidence_logs",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="evidence_ticket_case",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="evidence_vendors",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="impact_branch_department",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="impact_operational",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="impact_regulatory",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="impact_users",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="incident_commander",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="incident_id",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="incident_notified_person",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="incident_registered_person",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="incident_title",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="notified_signature",
            field=models.ImageField(
                blank=True,
                null=True,
                storage=tickets.storage.TicketImageStorage(),
                upload_to=tickets.models.incident_report_signature_upload_to,
            ),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="registered_signature",
            field=models.ImageField(
                blank=True,
                null=True,
                storage=tickets.storage.TicketImageStorage(),
                upload_to=tickets.models.incident_report_signature_upload_to,
            ),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="reported_by",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="review_action_owners",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="review_lessons_learned",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="review_preventive_actions",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="review_root_cause_summary",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="severity_level",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="summary_affected",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="summary_detected",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="summary_what_happened",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="temporary_workarounds",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="timeline_containment_started",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="timeline_detection",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="timeline_incident_closed",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="timeline_initial_triage",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="timeline_recovery_started",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="timeline_service_restored",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="incidentreport",
            name="updated_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="incident_reports_updated",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
