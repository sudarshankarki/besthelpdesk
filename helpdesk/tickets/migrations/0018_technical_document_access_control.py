from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0017_technical_documents"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="technicaldocument",
            name="allowed_users",
            field=models.ManyToManyField(
                blank=True,
                related_name="permitted_technical_documents",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="technicaldocument",
            name="description",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="technicaldocument",
            name="visibility",
            field=models.CharField(
                choices=[
                    ("public", "All users"),
                    ("restricted", "Restricted"),
                    ("support_only", "IT Support only"),
                ],
                default="public",
                max_length=16,
            ),
        ),
    ]

