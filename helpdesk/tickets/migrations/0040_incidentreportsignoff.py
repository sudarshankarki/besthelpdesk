from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import tickets.models
import tickets.storage


def backfill_notified_signoffs(apps, schema_editor):
    IncidentReport = apps.get_model("tickets", "IncidentReport")
    IncidentReportSignoff = apps.get_model("tickets", "IncidentReportSignoff")

    for report in IncidentReport.objects.all().iterator():
        has_legacy_notified = bool(
            report.notified_user_id
            or (report.incident_notified_person or "").strip()
            or report.notified_signature
            or report.notified_signed_at
        )
        if not has_legacy_notified:
            continue
        if IncidentReportSignoff.objects.filter(incident_report_id=report.id, role="notified").exists():
            continue

        signoff = IncidentReportSignoff(
            incident_report_id=report.id,
            role="notified",
            user_id=report.notified_user_id,
            level=1,
            signed_display_name=(report.incident_notified_person or "").strip(),
            signed_at=report.notified_signed_at,
        )
        signature_name = getattr(getattr(report, "notified_signature", None), "name", "") or ""
        if signature_name:
            signoff.snapshot_signature = signature_name
        signoff.save()


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("tickets", "0039_incidentreport_user_signoff"),
    ]

    operations = [
        migrations.CreateModel(
            name="IncidentReportSignoff",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=[("notified", "Notified")], default="notified", max_length=32)),
                ("level", models.PositiveIntegerField(default=1)),
                ("signed_display_name", models.CharField(blank=True, default="", max_length=255)),
                (
                    "snapshot_signature",
                    models.ImageField(
                        blank=True,
                        null=True,
                        storage=tickets.storage.TicketImageStorage(),
                        upload_to=tickets.models.incident_report_signoff_signature_upload_to,
                    ),
                ),
                ("signed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "incident_report",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="signoffs",
                        to="tickets.incidentreport",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="incident_report_signoffs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["role", "level", "id"],
            },
        ),
        migrations.AddConstraint(
            model_name="incidentreportsignoff",
            constraint=models.UniqueConstraint(
                fields=("incident_report", "role", "level"),
                name="incident_report_signoff_unique_level",
            ),
        ),
        migrations.AddConstraint(
            model_name="incidentreportsignoff",
            constraint=models.UniqueConstraint(
                fields=("incident_report", "role", "user"),
                name="incident_report_signoff_unique_user",
            ),
        ),
        migrations.RunPython(backfill_notified_signoffs, migrations.RunPython.noop),
    ]
