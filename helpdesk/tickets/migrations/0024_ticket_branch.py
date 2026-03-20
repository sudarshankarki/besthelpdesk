from django.conf import settings
from django.db import migrations, models


def populate_ticket_branch(apps, schema_editor):
    Ticket = apps.get_model("tickets", "Ticket")
    app_label, model_name = settings.AUTH_USER_MODEL.split(".")
    User = apps.get_model(app_label, model_name)

    user_branches = {
        user_id: (branch or "").strip()
        for user_id, branch in User.objects.values_list("id", "branch")
    }

    for ticket in Ticket.objects.all().iterator():
        if (ticket.branch or "").strip():
            continue
        branch = user_branches.get(ticket.created_by_id, "")
        if branch:
            Ticket.objects.filter(pk=ticket.pk).update(branch=branch)


def noop_reverse(apps, schema_editor):
    return


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0023_ticket_chat_privacy"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="branch",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.RunPython(populate_ticket_branch, reverse_code=noop_reverse),
    ]
