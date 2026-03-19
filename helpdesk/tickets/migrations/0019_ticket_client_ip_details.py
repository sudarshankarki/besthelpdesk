from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0018_technical_document_access_control"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="client_ip",
            field=models.GenericIPAddressField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ticket",
            name="peer_ip",
            field=models.GenericIPAddressField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ticket",
            name="client_ip_source",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="ticket",
            name="forwarded_for",
            field=models.TextField(blank=True, default=""),
        ),
    ]
