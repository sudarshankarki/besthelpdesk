from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0034_remoteaccessapproval"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="submission_token",
            field=models.CharField(blank=True, editable=False, max_length=64, null=True, unique=True),
        ),
    ]
