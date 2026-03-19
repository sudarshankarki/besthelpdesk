from django.db import migrations, models


def forwards_open_to_new(apps, schema_editor):
    Ticket = apps.get_model("tickets", "Ticket")
    Ticket.objects.filter(status="open").update(status="new")


def backwards_new_to_open(apps, schema_editor):
    Ticket = apps.get_model("tickets", "Ticket")
    Ticket.objects.filter(status="new").update(status="open")


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0013_ticket_status_notes"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ticket",
            name="status",
            field=models.CharField(
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
                default="new",
                max_length=32,
            ),
        ),
        migrations.RunPython(forwards_open_to_new, backwards_new_to_open),
    ]

