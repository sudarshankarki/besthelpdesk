from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0051_remoteaccessapproval_post_approval_assigned_to"),
    ]

    operations = [
        migrations.AlterField(
            model_name="remoteaccessapproval",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending_recommendation", "Pending Recommendation"),
                    ("pending_approval", "Pending Approval"),
                    ("approved", "Approved"),
                    ("returned", "Returned"),
                    ("rejected", "Rejected"),
                ],
                default="pending_approval",
                max_length=32,
            ),
        ),
    ]
