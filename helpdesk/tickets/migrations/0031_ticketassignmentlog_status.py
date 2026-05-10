from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0030_technicaldocument_branch_department_scope"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticketassignmentlog",
            name="status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("new", "New"),
                    ("acknowledged", "Acknowledged"),
                    ("in_progress", "In Progress"),
                    ("waiting_on_user", "Waiting on User"),
                    ("waiting_on_third_party", "Waiting on Third Party"),
                    ("resolved", "Resolved"),
                    ("closed", "Closed"),
                    ("cancelled_duplicate", "Cancelled / Duplicate"),
                ],
                default="",
                max_length=32,
            ),
        ),
    ]
