from django.db import migrations


def backfill_assignment_log_status(apps, schema_editor):
    TicketAssignmentLog = apps.get_model("tickets", "TicketAssignmentLog")

    logs = (
        TicketAssignmentLog.objects.select_related("ticket")
        .filter(status="")
        .order_by("id")
    )

    for log in logs.iterator():
        ticket = getattr(log, "ticket", None)
        if not ticket:
            continue

        inferred_status = ""
        if log.unassigned_at is None:
            if ticket.status == "closed" and ticket.resolved_at:
                inferred_status = "resolved"
            else:
                inferred_status = ticket.status or ""
        elif ticket.resolved_at and log.unassigned_at == ticket.resolved_at:
            inferred_status = "resolved"
        elif ticket.closed_at and log.unassigned_at == ticket.closed_at:
            inferred_status = "closed"

        if inferred_status:
            TicketAssignmentLog.objects.filter(pk=log.pk, status="").update(status=inferred_status)


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0031_ticketassignmentlog_status"),
    ]

    operations = [
        migrations.RunPython(backfill_assignment_log_status, migrations.RunPython.noop),
    ]
