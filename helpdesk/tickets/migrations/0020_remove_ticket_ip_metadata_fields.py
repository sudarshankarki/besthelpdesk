from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0019_ticket_client_ip_details"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="ticket",
            name="peer_ip",
        ),
        migrations.RemoveField(
            model_name="ticket",
            name="client_ip_source",
        ),
        migrations.RemoveField(
            model_name="ticket",
            name="forwarded_for",
        ),
    ]
