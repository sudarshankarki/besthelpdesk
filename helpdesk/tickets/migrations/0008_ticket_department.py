from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0007_ticketmessageattachment"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="department",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
    ]

