from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db.models import Max
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.urls import reverse
from django.utils import timezone

from accounts.utils import get_outgoing_from_email
from tickets.models import Ticket, TicketReminderSummaryLog


class Command(BaseCommand):
    help = "Send reminder emails for unresolved high/critical tickets every few days."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=3,
            help="Minimum unresolved age in days before reminders start.",
        )
        parser.add_argument(
            "--repeat-days",
            type=int,
            default=3,
            help="Repeat reminder cadence in days.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show how many reminder emails would be sent without updating tickets.",
        )
        parser.add_argument(
            "--site-url",
            default=(getattr(settings, "SITE_URL", "") or "").strip(),
            help="Optional base URL used to include a clickable ticket link in emails (e.g. https://helpdesk.example.com).",
        )

    def handle(self, *args, **options):
        days = int(options["days"])
        repeat_days = int(options["repeat_days"])
        dry_run = bool(options["dry_run"])
        site_url = (options.get("site_url") or "").strip().rstrip("/")

        if days <= 0:
            self.stderr.write(self.style.ERROR("--days must be greater than 0."))
            return
        if repeat_days <= 0:
            self.stderr.write(self.style.ERROR("--repeat-days must be greater than 0."))
            return

        now = timezone.now()
        overdue_cutoff = now - timedelta(days=days)
        repeat_cutoff = now - timedelta(days=repeat_days)
        active_statuses = {
            "new",
            "acknowledged",
            "in_progress",
            "waiting_on_user",
            "waiting_on_third_party",
        }
        tickets = (
            Ticket.objects.filter(
                priority__in={"critical", "high"},
                status__in=active_statuses,
                created_at__lte=overdue_cutoff,
                assigned_to__isnull=False,
            )
            .select_related("created_by", "assigned_to")
            .order_by("created_at")
        )

        tickets_by_assignee = {}
        for ticket in tickets:
            if not ticket.assigned_to_id:
                continue
            tickets_by_assignee.setdefault(ticket.assigned_to_id, []).append(ticket)

        last_sent_lookup = {
            row["assignee_id"]: row["last_sent_at"]
            for row in (
                TicketReminderSummaryLog.objects.filter(assignee_id__in=tickets_by_assignee.keys())
                .values("assignee_id")
                .annotate(last_sent_at=Max("sent_at"))
            )
        }

        due_groups = []
        for assignee_id, assignee_tickets in tickets_by_assignee.items():
            last_sent_at = last_sent_lookup.get(assignee_id)
            if last_sent_at and last_sent_at > repeat_cutoff:
                continue
            due_groups.append((assignee_tickets[0].assigned_to, assignee_tickets))

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run: {len(due_groups)} unresolved high/critical reminder summaries would be sent."
                )
            )
            return

        sent_count = 0
        skipped_count = 0
        for assignee, assignee_tickets in due_groups:
            assignee_email = (getattr(assignee, "email", "") or "").strip()
            if not assignee_email:
                skipped_count += 1
                continue

            ordered_tickets = sorted(
                assignee_tickets,
                key=lambda item: (
                    0 if item.priority == "critical" else 1,
                    item.created_at,
                ),
            )
            ticket_lines = []
            for ticket in ordered_tickets:
                ticket_path = reverse("ticket_detail", args=[ticket.id])
                ticket_url = f"{site_url}{ticket_path}" if site_url else ticket_path
                created_at_local = timezone.localtime(ticket.created_at).strftime("%b %d, %Y %H:%M")
                age_days = max((now - ticket.created_at).days, days)
                requester_name = ticket.created_by.get_full_name() or ticket.created_by.username
                ticket_lines.extend(
                    [
                        f"- {ticket.ticket_id} | {ticket.get_priority_display()} | {ticket.get_status_display()} | {ticket.subject}",
                        f"  Department: {ticket.department or '-'}",
                        f"  Created At: {created_at_local}",
                        f"  Open For: {age_days} day(s)",
                        f"  Requester: {requester_name}",
                        f"  Ticket Link: {ticket_url}",
                        "",
                    ]
                )

            mail_subject = (
                f"Reminder: {len(ordered_tickets)} unresolved high/critical "
                f"{'ticket' if len(ordered_tickets) == 1 else 'tickets'} assigned to you"
            )
            mail_body = (
                f"Hello {assignee.get_full_name() or assignee.username},\n\n"
                "This is a reminder summary for your unresolved high/critical tickets.\n\n"
                f"Total Due Tickets: {len(ordered_tickets)}\n\n"
                + "\n".join(ticket_lines).rstrip()
                + "\n"
            )

            try:
                send_mail(
                    subject=mail_subject,
                    message=mail_body,
                    from_email=get_outgoing_from_email(),
                    recipient_list=[assignee_email],
                    fail_silently=False,
                )
            except Exception:
                self.stderr.write(
                    self.style.WARNING(
                        f"Reminder summary for {assignee.get_username()} could not be sent."
                    )
                )
                continue

            TicketReminderSummaryLog.objects.create(
                assignee=assignee,
                sent_at=now,
                ticket_count=len(ordered_tickets),
            )
            sent_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Sent {sent_count} unresolved reminder summary emails (skipped without assignee email: {skipped_count})."
            )
        )
