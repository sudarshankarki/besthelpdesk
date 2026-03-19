from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from tickets.models import Ticket
from tickets.purge import purge_ticket_conversation


class Command(BaseCommand):
    help = "Delete chat history + uploads for tickets that stayed new beyond a retention window."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=getattr(settings, "OPEN_TICKET_CONVERSATION_RETENTION_DAYS", 10),
            help="New-ticket retention window in days. Tickets in New status older than this are purged.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show how many tickets would be purged without deleting anything.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]

        if days <= 0:
            self.stderr.write(self.style.ERROR("--days must be greater than 0."))
            return

        cutoff = timezone.now() - timedelta(days=days)
        tickets = Ticket.objects.filter(status="new", created_at__lt=cutoff).only("id")
        ticket_ids = list(tickets.values_list("id", flat=True))

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run: {len(ticket_ids)} new tickets older than {days} days would have their conversations purged."
                )
            )
            return

        total_messages = 0
        total_attachments = 0
        total_images = 0
        for ticket_id in ticket_ids:
            result = purge_ticket_conversation(ticket_id)
            total_messages += result["messages_deleted"]
            total_attachments += result["attachments_deleted"]
            total_images += result["ticket_image_cleared"]

        self.stdout.write(
            self.style.SUCCESS(
                f"Purged {len(ticket_ids)} new tickets older than {days} days "
                f"(messages={total_messages}, attachments={total_attachments}, ticket_images={total_images})."
            )
        )
