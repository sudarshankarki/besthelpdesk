from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from tickets.models import Ticket
from tickets.purge import purge_ticket_conversation


class Command(BaseCommand):
    help = "Delete chat history for closed tickets after a retention window based on closed_at."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=getattr(settings, "CLOSED_TICKET_CONVERSATION_RETENTION_DAYS", 10),
            help="Closed-ticket retention window in days before conversations are purged.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show how many closed tickets would be purged without deleting anything.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]

        if days <= 0:
            self.stderr.write(self.style.ERROR("--days must be greater than 0."))
            return

        cutoff = timezone.now() - timedelta(days=days)
        tickets = (
            Ticket.objects.filter(status="closed", closed_at__isnull=False, closed_at__lt=cutoff)
            .filter(Q(messages__isnull=False) | Q(image__isnull=False))
            .only("id")
            .distinct()
        )
        ticket_ids = list(tickets.values_list("id", flat=True))

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run: {len(ticket_ids)} closed tickets older than {days} days would have their conversations purged."
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
                f"Purged {len(ticket_ids)} closed tickets older than {days} days "
                f"(messages={total_messages}, attachments={total_attachments}, ticket_images={total_images})."
            )
        )
