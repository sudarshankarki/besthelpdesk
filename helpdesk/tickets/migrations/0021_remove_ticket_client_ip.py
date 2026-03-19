from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0020_remove_ticket_ip_metadata_fields"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="ticket",
            name="client_ip",
        ),
    ]
