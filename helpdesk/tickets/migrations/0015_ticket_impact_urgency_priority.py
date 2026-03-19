from django.db import migrations, models


def forwards_urgent_to_critical(apps, schema_editor):
    Ticket = apps.get_model("tickets", "Ticket")
    Ticket.objects.filter(priority="urgent").update(priority="critical")


def backwards_critical_to_urgent(apps, schema_editor):
    Ticket = apps.get_model("tickets", "Ticket")
    Ticket.objects.filter(priority="critical").update(priority="urgent")


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0014_ticket_workflow_statuses"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="impact",
            field=models.CharField(
                choices=[
                    ("single_user", "Single user"),
                    ("department", "Department"),
                    ("entire_org", "Entire org"),
                ],
                default="single_user",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="ticket",
            name="urgency",
            field=models.CharField(
                choices=[
                    ("low", "Low"),
                    ("medium", "Medium"),
                    ("high", "High"),
                    ("critical", "Critical"),
                ],
                default="medium",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="ticket",
            name="priority",
            field=models.CharField(
                choices=[
                    ("low", "Low"),
                    ("medium", "Medium"),
                    ("high", "High"),
                    ("critical", "Critical"),
                ],
                default="medium",
                max_length=20,
            ),
        ),
        migrations.RunPython(forwards_urgent_to_critical, backwards_critical_to_urgent),
    ]

