from django.db import models
from django.db import IntegrityError
from django.db.utils import OperationalError, ProgrammingError
from django.conf import settings
from django.utils import timezone
from django.utils.text import get_valid_filename
import os
import secrets
import uuid

from .storage import TicketImageStorage


def _normalize_email(value: str) -> str:
    return (value or "").strip().lower()


class GroupMailboxEmail(models.Model):
    email = models.EmailField(unique=True)
    department = models.ForeignKey(
        "accounts.Department",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="group_mailboxes",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["email"]

    def __str__(self):
        return self.email or ""

    def save(self, *args, **kwargs):
        self.email = _normalize_email(self.email)
        return super().save(*args, **kwargs)


def _group_mailbox_emails() -> set[str]:
    """
    DB is the source of truth once at least one GroupMailboxEmail exists.
    Before that, fall back to settings.GROUP_MAILBOX_EMAILS for backward compatibility.
    """
    try:
        emails = list(GroupMailboxEmail.objects.values_list("email", flat=True))
        if emails:
            return {_normalize_email(item) for item in emails if item}
    except (OperationalError, ProgrammingError):
        pass

    return {
        _normalize_email(item)
        for item in (getattr(settings, "GROUP_MAILBOX_EMAILS", []) or [])
        if (item or "").strip()
    }


def _get_group_mailbox(email: str) -> "GroupMailboxEmail | None":
    normalized = _normalize_email(email)
    if not normalized:
        return None
    try:
        return (
            GroupMailboxEmail.objects.select_related("department")
            .filter(email__iexact=normalized)
            .first()
        )
    except (OperationalError, ProgrammingError):
        return None


def is_group_mailbox_email(email: str) -> bool:
    normalized = _normalize_email(email)
    if not normalized:
        return False
    return normalized in _group_mailbox_emails()


def _department_from_group_notify_email(notify_email: str) -> str | None:
    email = _normalize_email(notify_email)
    if not email or "@" not in email:
        return None

    if email not in _group_mailbox_emails():
        return None

    mailbox = _get_group_mailbox(email)
    if mailbox and mailbox.department_id:
        return mailbox.department.name

    local_part = (email.split("@", 1)[0] or "").strip()
    if not local_part:
        return None

    try:
        from accounts.models import Department

        dept_name = (
            Department.objects.filter(name__iexact=local_part)
            .values_list("name", flat=True)
            .first()
        )
        if dept_name:
            return dept_name
    except Exception:
        pass

    if local_part.isalpha() and len(local_part) <= 4:
        return local_part.upper()

    normalized = local_part.replace(".", " ").replace("_", " ").replace("-", " ")
    normalized = " ".join(normalized.split())
    return normalized.title() if normalized else local_part


def _is_ticket_department_member(user, ticket) -> bool:
    user_department = (getattr(user, "department", "") or "").strip()
    ticket_department = (getattr(ticket, "department", "") or "").strip()
    ticket_branch = (getattr(ticket, "branch", "") or "").strip()
    if not ticket_branch:
        requester = getattr(ticket, "created_by", None)
        ticket_branch = (getattr(requester, "branch", "") or "").strip()
    user_branch = (getattr(user, "branch", "") or "").strip()
    return bool(
        user_department
        and ticket_department
        and user_branch
        and ticket_branch
        and user_department.casefold() == ticket_department.casefold()
        and user_branch.casefold() == ticket_branch.casefold()
    )


def ticket_image_upload_to(instance: "Ticket", filename: str) -> str:
    name = get_valid_filename(os.path.basename(filename or "upload"))
    ticket_id = instance.ticket_id or "unknown"
    return f"ticket_images/{ticket_id}/{uuid.uuid4().hex}/{name}"


class Ticket(models.Model):
    TICKET_STATUS = [
        ("new", "New"),
        ("acknowledged", "Acknowledged"),
        ('in_progress', 'In Progress'),
        ("waiting_on_user", "Waiting on User"),
        ("waiting_on_third_party", "Waiting on Third Party"),
        ('resolved', 'Resolved'),
        ('closed', 'Closed'),
        ("cancelled_duplicate", "Cancelled / Duplicate"),
    ]

    IMPACT_CHOICES = [
        ("single_user", "Single user"),
        ("department", "Department"),
        ("entire_org", "Entire org"),
    ]

    URGENCY_CHOICES = [
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("critical", "Critical"),
    ]

    PRIORITY_CHOICES = [
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("critical", "Critical"),
    ]

    REQUEST_TYPE_CHOICES = [
        ("incident", "Incident"),
        ("service", "Service Request"),
        ("access", "Access Request"),
        ("change", "Change"),
    ]

    # Keep the default auto-increment ID as primary key
    # id = models.AutoField(primary_key=True)  # This is default and implicit

    # Custom ticket ID for display (NOT primary key)
    ticket_id = models.CharField(
        max_length=20,
        unique=True,
        editable=False,
        null=True,  # Allow null temporarily for migration
        blank=True  # Allow blank temporarily for migration
    )

    # Link to the user who created the ticket
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='tickets'
    )

    subject = models.CharField(max_length=200)
    request_type = models.CharField(
        max_length=20,
        choices=REQUEST_TYPE_CHOICES,
        default="incident",
    )
    notify_email = models.EmailField(blank=True, default="")
    department = models.CharField(max_length=100, blank=True, default="")
    branch = models.CharField(max_length=100, blank=True, default="")
    description = models.TextField()
    impact = models.CharField(max_length=20, choices=IMPACT_CHOICES, default="single_user")
    urgency = models.CharField(max_length=20, choices=URGENCY_CHOICES, default="medium")
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='medium')
    status = models.CharField(max_length=32, choices=TICKET_STATUS, default="new")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_note = models.TextField(blank=True, default="")
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_note = models.TextField(blank=True, default="")
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="closed_tickets",
    )

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_tickets'
    )
    chat_is_private = models.BooleanField(default=False)
    image = models.ImageField(
        upload_to=ticket_image_upload_to,
        storage=TicketImageStorage(),
        null=True,
        blank=True,
    )
    def __str__(self):
        return f"Ticket #{self.ticket_id} - {self.subject}"

    @staticmethod
    def calculate_priority(impact: str, urgency: str) -> str:
        impact = (impact or "").strip()
        urgency = (urgency or "").strip()

        if impact == "entire_org" and urgency in {"high", "critical"}:
            return "critical"

        if impact == "department" and urgency in {"high", "critical"}:
            return "high"

        if impact == "entire_org" and urgency == "medium":
            return "high"

        if impact == "single_user" and urgency == "low":
            return "low"

        return "medium"

    def save(self, *args, **kwargs):
        self.notify_email = _normalize_email(self.notify_email)
        self.department = (self.department or "").strip()
        self.branch = (self.branch or "").strip()
        if not self.department:
            derived = _department_from_group_notify_email(self.notify_email)
            if derived:
                self.department = derived
        if not self.branch:
            creator = getattr(self, "created_by", None)
            self.branch = (getattr(creator, "branch", "") or "").strip()

        previous_status = None
        previous_assigned_to_id = None
        if self.pk:
            previous_status = Ticket.objects.filter(pk=self.pk).values_list("status", flat=True).first()
            previous_assigned_to_id = Ticket.objects.filter(pk=self.pk).values_list("assigned_to_id", flat=True).first()

        now = timezone.now()
        if self.status == "resolved" and self.resolved_at is None:
            self.resolved_at = now
        if self.status == "closed" and self.closed_at is None:
            self.closed_at = now
        active_statuses = {
            "new",
            "acknowledged",
            "in_progress",
            "waiting_on_user",
            "waiting_on_third_party",
        }
        if previous_status in {"resolved", "closed"} and self.status in active_statuses:
            self.resolved_at = None
            self.closed_at = None
            self.closed_by = None

        creating = self.pk is None
        if not self.ticket_id:
            self.ticket_id = self.generate_ticket_id()

        # ticket_id is unique and generated randomly; retry on rare collisions.
        for _ in range(10):
            try:
                super().save(*args, **kwargs)
                break
            except IntegrityError:
                if not creating:
                    raise
                self.ticket_id = self.generate_ticket_id()
        else:
            raise RuntimeError("Could not generate a unique ticket_id after multiple attempts.")

        if previous_assigned_to_id != self.assigned_to_id:
            now = timezone.now()
            TicketAssignmentLog.objects.filter(ticket_id=self.pk, unassigned_at__isnull=True).update(
                unassigned_at=now
            )
            if self.assigned_to_id:
                actor_id = getattr(self, "_assignment_actor_id", None)
                TicketAssignmentLog.objects.create(
                    ticket_id=self.pk,
                    assigned_to_id=self.assigned_to_id,
                    assigned_by_id=actor_id,
                    assigned_at=now,
                )

    @property
    def resolution_duration(self):
        end_time = self.closed_at or self.resolved_at
        if not end_time:
            return None
        return end_time - self.created_at

    @property
    def status_age(self):
        return timezone.now() - self.created_at

    def formatted_ttr(self):
        duration = self.resolution_duration
        if not duration:
            return "Not resolved yet"
        total_minutes = int(duration.total_seconds() // 60)
        hours, minutes = divmod(total_minutes, 60)
        days, hours = divmod(hours, 24)
        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        return f"{hours}h {minutes}m"

    @classmethod
    def generate_ticket_id(cls):
        """Generate a unique random ticket id like BFC-7K3M2Q9PXR."""
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        for _ in range(50):
            suffix = "".join(secrets.choice(alphabet) for _ in range(10))
            candidate = f"BFC-{suffix}"
            if not cls.objects.filter(ticket_id=candidate).exists():
                return candidate
        raise RuntimeError("Failed to generate unique ticket_id; try again.")

    class Meta:
        ordering = ['-created_at']


def can_access_ticket_chat(user, ticket) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(ticket, "created_by_id", None) == getattr(user, "id", None):
        return True
    if getattr(ticket, "assigned_to_id", None) == getattr(user, "id", None):
        return True
    if getattr(ticket, "chat_is_private", False):
        return False
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False) or getattr(user, "is_itsupport", False):
        return True
    return _is_ticket_department_member(user, ticket)


def can_manage_ticket_chat_privacy(user, ticket) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    return bool(
        getattr(user, "is_staff", False)
        or getattr(user, "is_superuser", False)
        or getattr(ticket, "created_by_id", None) == getattr(user, "id", None)
    )


def get_ticket_chat_access_user_ids(ticket, actor_user_id):
    targets = []
    for user_id in (ticket.created_by_id, ticket.assigned_to_id):
        if not user_id or user_id == actor_user_id or user_id in targets:
            continue
        targets.append(user_id)
    return targets


class TicketAssignmentLog(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="assignment_logs")
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ticket_assignment_logs",
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ticket_assignments_made",
    )
    assigned_at = models.DateTimeField(default=timezone.now)
    unassigned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-assigned_at"]
        indexes = [
            models.Index(fields=["ticket", "assigned_at"], name="assign_ticket_assigned_idx"),
            models.Index(fields=["assigned_to", "assigned_at"], name="assign_assignee_assigned_idx"),
            models.Index(fields=["unassigned_at"], name="assign_unassigned_idx"),
        ]

    @property
    def duration(self):
        end = self.unassigned_at or timezone.now()
        return end - self.assigned_at

    def formatted_duration(self):
        duration = self.duration
        if not duration:
            return ""

        total_minutes = int(duration.total_seconds() // 60)
        hours, minutes = divmod(total_minutes, 60)
        days, hours = divmod(hours, 24)

        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"


class TicketMessage(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ticket_messages"
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["ticket", "created_at"], name="ticketmsg_ticket_created_idx"),
            models.Index(fields=["created_at"], name="ticketmsg_created_idx"),
        ]

    def __str__(self):
        return f"{self.ticket.ticket_id} | {self.author.username}"


class TicketChatReadState(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="chat_read_states")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ticket_chat_read_states",
    )
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["ticket", "user"], name="ticketchatread_ticket_user_uniq"),
        ]
        indexes = [
            models.Index(fields=["ticket", "user"], name="ticketchatread_ticket_user_idx"),
            models.Index(fields=["user", "last_seen_at"], name="ticketchatread_user_seen_idx"),
        ]

    def __str__(self):
        return f"{self.ticket.ticket_id} | {self.user.username}"


class TicketMessageAttachment(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="attachments")
    message = models.OneToOneField(
        TicketMessage, on_delete=models.CASCADE, related_name="attachment"
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ticket_attachments"
    )
    object_key = models.CharField(max_length=512, unique=True)
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=255, blank=True)
    size = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["ticket", "created_at"], name="attach_ticket_created_idx"),
            models.Index(fields=["created_at"], name="attach_created_idx"),
        ]

    @staticmethod
    def build_object_key(ticket_id: int, filename: str) -> str:
        name = get_valid_filename(os.path.basename(filename or "upload"))
        return f"tickets/{ticket_id}/{uuid.uuid4().hex}/{name}"

    def __str__(self):
        return f"{self.ticket.ticket_id} | {self.filename}"


class TechnicalDocument(models.Model):
    VISIBILITY_PUBLIC = "public"
    VISIBILITY_RESTRICTED = "restricted"
    VISIBILITY_SUPPORT_ONLY = "support_only"
    VISIBILITY_CHOICES = [
        (VISIBILITY_PUBLIC, "All users"),
        (VISIBILITY_RESTRICTED, "Restricted"),
        (VISIBILITY_SUPPORT_ONLY, "IT Support only"),
    ]

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    visibility = models.CharField(
        max_length=16,
        choices=VISIBILITY_CHOICES,
        default=VISIBILITY_PUBLIC,
    )
    allowed_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="permitted_technical_documents",
    )
    object_key = models.CharField(max_length=512, unique=True)
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=255, blank=True)
    size = models.BigIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="technical_documents",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["created_at"], name="techdoc_created_idx"),
        ]

    @staticmethod
    def build_object_key(filename: str) -> str:
        name = get_valid_filename(os.path.basename(filename or "upload"))
        return f"tech_docs/{uuid.uuid4().hex}/{name}"

    def __str__(self):
        return self.title or self.filename or f"Document #{self.pk}"

    def delete(self, *args, **kwargs):
        object_key = self.object_key
        try:
            from .minio import get_minio_config, get_s3_client

            cfg = get_minio_config()
            s3 = get_s3_client()
            if object_key:
                s3.delete_object(Bucket=cfg.bucket, Key=object_key)
        except Exception:
            pass
        return super().delete(*args, **kwargs)
