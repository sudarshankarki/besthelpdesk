from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0036_remoteaccessapproval_recommendation_chain"),
    ]

    operations = [
        migrations.CreateModel(
            name="IncidentReport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "service_affected",
                    models.CharField(
                        choices=[
                            ("cbs", "CBS"),
                            ("network", "Network"),
                            ("atm", "ATM"),
                            ("internet_banking", "Internet Banking"),
                        ],
                        max_length=32,
                    ),
                ),
                ("downtime_duration_minutes", models.PositiveIntegerField(blank=True, null=True)),
                ("branch_impacted", models.CharField(blank=True, default="", max_length=100)),
                ("regulatory_impact", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "ticket",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="incident_report",
                        to="tickets.ticket",
                    ),
                ),
            ],
            options={
                "ordering": ["-updated_at", "-created_at"],
            },
        ),
    ]
