from __future__ import annotations

import logging

from django.db import transaction

from .minio import get_minio_config, get_s3_client
from .models import Ticket, TicketMessage, TicketMessageAttachment

logger = logging.getLogger(__name__)


def _try_delete_minio_objects(object_keys: list[str]) -> None:
    if not object_keys:
        return
    try:
        cfg = get_minio_config()
        s3 = get_s3_client()
    except Exception:
        logger.warning("MinIO not configured; skipping object delete for %s keys", len(object_keys))
        return

    # Delete up to 1000 keys per request.
    for i in range(0, len(object_keys), 1000):
        chunk = object_keys[i : i + 1000]
        try:
            s3.delete_objects(
                Bucket=cfg.bucket,
                Delete={"Objects": [{"Key": key} for key in chunk], "Quiet": True},
            )
        except Exception:
            logger.exception("Failed deleting %s objects from MinIO", len(chunk))


def purge_ticket_conversation(ticket_id: int) -> dict[str, int]:
    """
    Deletes all chat messages and message attachments for a ticket.
    Also deletes attached objects from MinIO (best effort) and clears Ticket.image.
    """
    ticket = Ticket.objects.filter(pk=ticket_id).first()
    if not ticket:
        return {"messages_deleted": 0, "attachments_deleted": 0, "ticket_image_cleared": 0}

    attachments = list(
        TicketMessageAttachment.objects.filter(ticket_id=ticket_id).values_list("object_key", flat=True)
    )
    _try_delete_minio_objects(attachments)

    with transaction.atomic():
        attachments_deleted, _ = TicketMessageAttachment.objects.filter(ticket_id=ticket_id).delete()
        messages_deleted, _ = TicketMessage.objects.filter(ticket_id=ticket_id).delete()

        ticket_image_cleared = 0
        if ticket.image:
            try:
                ticket.image.delete(save=False)
            except Exception:
                logger.exception("Failed deleting Ticket.image for ticket_id=%s", ticket_id)
            Ticket.objects.filter(pk=ticket_id).update(image=None)
            ticket_image_cleared = 1

    return {
        "messages_deleted": messages_deleted,
        "attachments_deleted": attachments_deleted,
        "ticket_image_cleared": ticket_image_cleared,
    }

