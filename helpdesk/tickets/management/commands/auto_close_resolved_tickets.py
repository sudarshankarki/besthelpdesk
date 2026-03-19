from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.urls import reverse
from django.utils import timezone

from accounts.utils import get_outgoing_from_email
from tickets.models import Ticket


class Command(BaseCommand):
    help = "Auto-close tickets that have been in Resolved status beyond a threshold."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=int(getattr(settings, "TICKET_AUTO_CLOSE_DAYS", 10)),
            help="Auto-close window in days (based on resolved_at).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show how many tickets would be auto-closed without updating anything.",
        )
        parser.add_argument(
            "--site-url",
            default=(getattr(settings, "SITE_URL", "") or "").strip(),
            help="Optional base URL used to include a clickable ticket link in emails (e.g. https://helpdesk.example.com).",
        )

    def handle(self, *args, **options):
        days = int(options["days"])
        dry_run = bool(options["dry_run"])
        site_url = (options.get("site_url") or "").strip().rstrip("/")

        if days <= 0:
            self.stderr.write(self.style.ERROR("--days must be greater than 0."))
            return

        cutoff = timezone.now() - timedelta(days=days)
        tickets = (
            Ticket.objects.filter(status="resolved", resolved_at__isnull=False, resolved_at__lt=cutoff)
            .select_related("created_by")
            .order_by("resolved_at")
        )
        ticket_ids = list(tickets.values_list("id", flat=True))

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run: {len(ticket_ids)} resolved tickets older than {days} days would be auto-closed."
                )
            )
            return

        closed_count = 0
        emailed_count = 0
        for ticket in tickets:
            ticket.status = "closed"
            ticket.closed_by = None
            ticket.closed_note = ticket.closed_note or f"Auto-closed after {days} days without requester confirmation."
            ticket.save()
            closed_count += 1

            requester_email = (getattr(ticket.created_by, "email", "") or "").strip()
            if not requester_email:
                continue

            ticket_path = reverse("ticket_detail", args=[ticket.id])
            ticket_url = f"{site_url}{ticket_path}" if site_url else ticket_path

            closed_at = ticket.closed_at
            closed_at_local = timezone.localtime(closed_at).strftime("%b %d, %Y %H:%M") if closed_at else "-"

            mail_subject = f"Ticket Auto-Closed: {ticket.ticket_id}"
            mail_body = (
                f"Hello {ticket.created_by.get_full_name() or ticket.created_by.username},\n\n"
                f"Your helpdesk ticket has been auto-closed because it stayed in Resolved status for more than {days} days.\n\n"
                f"Ticket Number: {ticket.ticket_id}\n"
                f"Subject: {ticket.subject}\n"
                f"Department: {ticket.department or '-'}\n"
                f"Impact: {ticket.get_impact_display()}\n"
                f"Urgency: {ticket.get_urgency_display()}\n"
                f"Priority: {ticket.get_priority_display()}\n"
                f"Status: {ticket.get_status_display()}\n"
                f"Closed At: {closed_at_local}\n"
                f"Closure Details:\n{ticket.closed_note or '-'}\n\n"
                f"Ticket Link:\n{ticket_url}\n"
            )

            try:
                send_mail(
                    subject=mail_subject,
                    message=mail_body,
                    from_email=get_outgoing_from_email(),
                    recipient_list=[requester_email],
                    fail_silently=False,
                )
                emailed_count += 1
            except Exception:
                self.stderr.write(
                    self.style.WARNING(
                        f"Ticket {ticket.ticket_id} auto-closed, but requester email could not be sent."
                    )
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Auto-closed {closed_count} tickets (emails sent: {emailed_count})."
            )
        )
