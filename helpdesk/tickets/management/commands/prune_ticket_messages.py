from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from tickets.models import TicketMessage, TicketMessageAttachment
from tickets.purge import _try_delete_minio_objects


class Command(BaseCommand):
    help = "Delete ticket chat messages older than retention days."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=getattr(settings, "MESSAGE_RETENTION_DAYS", 180),
            help="Retention window in days. Messages older than this are removed.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show how many messages would be deleted without deleting them.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]

        if days <= 0:
            self.stderr.write(self.style.ERROR("--days must be greater than 0."))
            return

        cutoff = timezone.now() - timedelta(days=days)
        queryset = TicketMessage.objects.filter(created_at__lt=cutoff)
        count = queryset.count()
        attachment_keys = list(
            TicketMessageAttachment.objects.filter(message__in=queryset).values_list("object_key", flat=True)
        )

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run: {count} ticket messages and {len(attachment_keys)} attachments older than {days} days would be deleted."
                )
            )
            return

        _try_delete_minio_objects(attachment_keys)
        deleted_count, _ = queryset.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted_count} ticket messages older than {days} days "
                f"(created before {cutoff:%Y-%m-%d %H:%M:%S %Z})."
            )
        )
