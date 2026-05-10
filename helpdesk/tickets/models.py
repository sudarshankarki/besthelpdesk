import os
import secrets
import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError
from django.db import models
from django.db.utils import OperationalError, ProgrammingError
from django.utils import timezone
from django.utils.text import get_valid_filename

from .storage import TicketImageStorage


def _normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def parse_email_list(value: str) -> list[str]:
    parts = re.split(r"[,;\r\n]+", value or "")
    emails = []
    seen = set()
    for part in parts:
        email = _normalize_email(part)
        if not email or email in seen:
            continue
        seen.add(email)
        emails.append(email)
    return emails


def _normalize_email_list(value: str) -> str:
    return ", ".join(parse_email_list(value))


def parse_department_list(value: str) -> list[str]:
    parts = re.split(r"[,;\r\n]+", value or "")
    departments = []
    seen = set()
    for part in parts:
        department = (part or "").strip()
        key = department.casefold()
        if not department or key in seen:
            continue
        seen.add(key)
        departments.append(department)
    return departments


def _normalize_department_list(value: str) -> str:
    return ", ".join(parse_department_list(value))


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


def portal_flash_image_upload_to(instance: "PortalFlashAnnouncement", filename: str) -> str:
    name = get_valid_filename(os.path.basename(filename or "upload"))
    return f"portal_flashes/{uuid.uuid4().hex}/{name}"


def incident_report_signature_upload_to(instance: "IncidentReport", filename: str) -> str:
    name = get_valid_filename(os.path.basename(filename or "signature"))
    ticket_id = getattr(getattr(instance, "ticket", None), "ticket_id", "") or "unknown"
    return f"incident_reports/{ticket_id}/{uuid.uuid4().hex}/{name}"


def incident_report_signoff_signature_upload_to(instance: "IncidentReportSignoff", filename: str) -> str:
    name = get_valid_filename(os.path.basename(filename or "signature"))
    report = getattr(instance, "incident_report", None)
    ticket_id = getattr(getattr(report, "ticket", None), "ticket_id", "") or "unknown"
    return f"incident_reports/{ticket_id}/signoffs/{uuid.uuid4().hex}/{name}"


def incident_report_attachment_upload_to(instance: "IncidentReportAttachment", filename: str) -> str:
    name = get_valid_filename(os.path.basename(filename or "attachment"))
    report = getattr(instance, "incident_report", None)
    ticket_id = getattr(getattr(report, "ticket", None), "ticket_id", "") or "unknown"
    return f"incident_reports/{ticket_id}/attachments/{uuid.uuid4().hex}/{name}"


def remote_access_signature_snapshot_upload_to(instance: "RemoteAccessApproval", filename: str) -> str:
    name = get_valid_filename(os.path.basename(filename or "signature"))
    ticket_id = getattr(getattr(instance, "ticket", None), "ticket_id", "") or "unknown"
    return f"remote_access_approvals/{ticket_id}/signatures/{uuid.uuid4().hex}/{name}"


def incident_report_person_display(user) -> str:
    if not user:
        return ""
    full_name = (user.get_full_name() or "").strip()
    return full_name or (getattr(user, "username", "") or "").strip()


def default_portal_flash_end_at():
    return timezone.now() + timedelta(days=7)


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
        ("cbs_access_ho", "CBS Access Request (Head Office)"),
        ("cbs_access_branch", "CBS Access Request (Branch)"),
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
    cc_emails = models.TextField(blank=True, default="")
    department = models.CharField(max_length=100, blank=True, default="")
    additional_departments = models.TextField(blank=True, default="")
    branch = models.CharField(max_length=100, blank=True, default="")
    description = models.TextField()
    impact = models.CharField(max_length=20, choices=IMPACT_CHOICES, default="single_user")
    urgency = models.CharField(max_length=20, choices=URGENCY_CHOICES, default="medium")
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='medium')
    status = models.CharField(max_length=32, choices=TICKET_STATUS, default="new")
    submission_token = models.CharField(max_length=64, null=True, blank=True, unique=True, editable=False)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_note = models.TextField(blank=True, default="")
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_tickets",
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_note = models.TextField(blank=True, default="")
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="closed_tickets",
    )
    last_unresolved_reminder_at = models.DateTimeField(null=True, blank=True)

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
        self.cc_emails = _normalize_email_list(self.cc_emails)
        self.department = (self.department or "").strip()
        self.additional_departments = _normalize_department_list(self.additional_departments)
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

        solved_statuses = {"resolved", "closed"}
        now = timezone.now()
        if self.status == "resolved" and self.resolved_at is None:
            self.resolved_at = now
        if self.status == "closed" and self.closed_at is None:
            self.closed_at = now

        assignment_end_time = self.resolved_at or self.closed_at or now
        assignment_end_status = "resolved" if self.status == "closed" and self.resolved_at else self.status
        if self.status in solved_statuses:
            self.assigned_to = None

        active_statuses = {
            "new",
            "acknowledged",
            "in_progress",
            "waiting_on_user",
            "waiting_on_third_party",
        }
        if previous_status in solved_statuses and self.status in active_statuses:
            self.resolved_at = None
            self.closed_at = None
            self.closed_by = None
            self.resolved_by = None
            self.last_unresolved_reminder_at = None

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

        active_assignment_logs = TicketAssignmentLog.objects.filter(
            ticket_id=self.pk,
            unassigned_at__isnull=True,
        )
        if previous_assigned_to_id != self.assigned_to_id or self.status in solved_statuses:
            log_end_time = assignment_end_time if self.status in solved_statuses else timezone.now()
            active_assignment_logs.update(
                unassigned_at=log_end_time,
                status=assignment_end_status,
            )
            if self.assigned_to_id:
                actor_id = getattr(self, "_assignment_actor_id", None)
                TicketAssignmentLog.objects.create(
                    ticket_id=self.pk,
                    assigned_to_id=self.assigned_to_id,
                    assigned_by_id=actor_id,
                    assigned_at=timezone.now(),
                    status=self.status,
                )
        elif previous_status != self.status:
            active_assignment_logs.update(status=self.status)

    @property
    def resolution_duration(self):
        end_time = self.resolved_at or self.closed_at
        if not end_time:
            return None
        return end_time - self.created_at

    @property
    def status_age(self):
        return timezone.now() - self.created_at

    @property
    def is_solved(self):
        return self.status in {"resolved", "closed", "cancelled_duplicate"}

    @property
    def display_assignee(self):
        if self.assigned_to_id:
            return self.assigned_to
        if self.status not in {"resolved", "closed"}:
            return None
        if hasattr(self, "_display_assignee"):
            return self._display_assignee
        if not self.pk:
            return None
        latest_assignment = (
            self.assignment_logs.select_related("assigned_to")
            .order_by("-assigned_at", "-id")
            .first()
        )
        return getattr(latest_assignment, "assigned_to", None)

    @property
    def display_resolved_by(self):
        if self.resolved_by_id:
            return self.resolved_by
        if not self.resolved_at or self.status not in {"resolved", "closed"}:
            return None
        return self.display_assignee

    @property
    def cc_email_list(self):
        return parse_email_list(self.cc_emails)

    @property
    def overdue_attention_level(self):
        if self.is_solved:
            return ""

        age = self.status_age
        if self.priority == "critical" and age >= timedelta(days=1):
            return "critical"
        if self.priority == "high" and age >= timedelta(days=3):
            return "high"
        if self.priority == "medium" and age >= timedelta(days=5):
            return "medium"
        if self.priority == "low" and age >= timedelta(days=5):
            return "low"
        return ""

    @property
    def overdue_attention_label(self):
        level = self.overdue_attention_level
        if level == "critical":
            return "Critical overdue"
        if level == "high":
            return "High overdue"
        if level == "medium":
            return "Medium overdue"
        if level == "low":
            return "Low overdue"
        return ""

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


class IncidentReport(models.Model):
    SERVICE_CBS = "cbs"
    SERVICE_NETWORK = "network"
    SERVICE_ATM = "atm"
    SERVICE_INTERNET_BANKING = "internet_banking"
    SERVICE_CHOICES = [
        (SERVICE_CBS, "CBS"),
        (SERVICE_NETWORK, "Network"),
        (SERVICE_ATM, "ATM"),
        (SERVICE_INTERNET_BANKING, "Internet Banking"),
    ]

    SEVERITY_CRITICAL = "critical"
    SEVERITY_HIGH = "high"
    SEVERITY_MEDIUM = "medium"
    SEVERITY_LOW = "low"
    SEVERITY_CHOICES = [
        (SEVERITY_CRITICAL, "Critical"),
        (SEVERITY_HIGH, "High"),
        (SEVERITY_MEDIUM, "Medium"),
        (SEVERITY_LOW, "Low"),
    ]

    ticket = models.OneToOneField(
        Ticket,
        on_delete=models.CASCADE,
        related_name="incident_report",
    )
    
    # BREACH REPORTING EMPLOYEE'S INFORMATION
    reporting_employee_name = models.CharField(max_length=255, blank=True, default="")
    reporting_employee_designation = models.CharField(max_length=255, blank=True, default="")
    reporting_employee_email = models.EmailField(blank=True, default="")
    reporting_employee_contact = models.CharField(max_length=20, blank=True, default="")
    date_of_report = models.DateTimeField(null=True, blank=True)

    service_affected = models.CharField(max_length=32, choices=SERVICE_CHOICES)
    downtime_duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    branch_impacted = models.CharField(max_length=100, blank=True, default="")
    regulatory_impact = models.BooleanField(default=False)
    incident_title = models.CharField(max_length=255, blank=True, default="")
    incident_id = models.CharField(max_length=100, blank=True, default="")
    detected_at = models.CharField(max_length=100, blank=True, default="")
    
    # INCIDENT DETAILS (New Template Fields)
    date_time_of_occurrence = models.DateTimeField(null=True, blank=True)
    date_time_of_detection = models.DateTimeField(null=True, blank=True)
    source_of_incident = models.TextField(blank=True, default="")
    incident_location_ip = models.CharField(max_length=255, blank=True, default="")
    incident_description = models.TextField(blank=True, default="")
    
    reported_by = models.CharField(max_length=255, blank=True, default="")
    incident_commander = models.CharField(max_length=255, blank=True, default="")
    incident_commander_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="incident_reports_as_commander",
    )
    severity_level = models.CharField(max_length=100, blank=True, default="")
    severity_choice = models.CharField(max_length=20, choices=SEVERITY_CHOICES, blank=True, default="")
    current_status = models.CharField(max_length=100, blank=True, default="")
    summary_what_happened = models.TextField(blank=True, default="")
    summary_detected = models.TextField(blank=True, default="")
    summary_affected = models.TextField(blank=True, default="")
    
    # IMPACT ASSESSMENT (New Template Fields)
    unit_or_department_impacted = models.CharField(max_length=255, blank=True, default="")
    systems_impacted = models.TextField(blank=True, default="")
    network_impacted = models.TextField(blank=True, default="")
    operations_impacted = models.TextField(blank=True, default="")
    
    impact_branch_department = models.CharField(max_length=255, blank=True, default="")
    impact_users = models.CharField(max_length=255, blank=True, default="")
    impact_operational = models.TextField(blank=True, default="")
    impact_regulatory = models.TextField(blank=True, default="")
    timeline_detection = models.CharField(max_length=100, blank=True, default="")
    timeline_initial_triage = models.CharField(max_length=100, blank=True, default="")
    timeline_containment_started = models.CharField(max_length=100, blank=True, default="")
    timeline_recovery_started = models.CharField(max_length=100, blank=True, default="")
    timeline_service_restored = models.CharField(max_length=100, blank=True, default="")
    timeline_incident_closed = models.CharField(max_length=100, blank=True, default="")
    
    # INCIDENT RECOVERY DETAILS (New Template Fields)
    recovery_actions = models.TextField(blank=True, default="")
    recovery_timeframe = models.CharField(max_length=255, blank=True, default="")
    post_recovery_verification = models.TextField(blank=True, default="")
    recovery_communication = models.TextField(blank=True, default="")
    
    containment_actions = models.TextField(blank=True, default="")
    temporary_workarounds = models.TextField(blank=True, default="")
    escalations_raised = models.TextField(blank=True, default="")
    eradication_root_cause = models.TextField(blank=True, default="")
    eradication_fix_applied = models.TextField(blank=True, default="")
    eradication_validation_steps = models.TextField(blank=True, default="")
    eradication_systems_restored = models.TextField(blank=True, default="")
    
    # INCIDENT RESPONSE DETAILS (New Template Fields)
    quarantine_process = models.TextField(blank=True, default="")
    immediate_actions = models.TextField(blank=True, default="")
    root_cause_analysis = models.TextField(blank=True, default="")
    eradication_method = models.TextField(blank=True, default="")
    lessons_learned = models.TextField(blank=True, default="")
    recommendations_for_improvement = models.TextField(blank=True, default="")
    action_plan = models.TextField(blank=True, default="")
    
    communication_stakeholders = models.TextField(blank=True, default="")
    communication_update_frequency = models.CharField(max_length=255, blank=True, default="")
    communication_latest_update = models.TextField(blank=True, default="")
    evidence_ticket_case = models.CharField(max_length=255, blank=True, default="")
    evidence_logs = models.TextField(blank=True, default="")
    evidence_attachments = models.TextField(blank=True, default="")
    evidence_vendors = models.TextField(blank=True, default="")
    
    # INCIDENT INFORMATION SHARING (New Template Fields)
    unit_or_department_requiring_notification = models.CharField(max_length=255, blank=True, default="")
    point_of_contact = models.CharField(max_length=255, blank=True, default="")
    date_of_notification = models.DateTimeField(null=True, blank=True)
    
    review_root_cause_summary = models.TextField(blank=True, default="")
    review_lessons_learned = models.TextField(blank=True, default="")
    review_preventive_actions = models.TextField(blank=True, default="")
    review_action_owners = models.TextField(blank=True, default="")
    registered_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="incident_reports_as_registered_user",
    )
    notified_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="incident_reports_as_notified_user",
    )
    incident_registered_person = models.CharField(max_length=255, blank=True, default="")
    incident_notified_person = models.CharField(max_length=255, blank=True, default="")
    registered_signature = models.ImageField(
        upload_to=incident_report_signature_upload_to,
        storage=TicketImageStorage(),
        null=True,
        blank=True,
    )
    notified_signature = models.ImageField(
        upload_to=incident_report_signature_upload_to,
        storage=TicketImageStorage(),
        null=True,
        blank=True,
    )
    registered_signed_at = models.DateTimeField(null=True, blank=True)
    notified_signed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="incident_reports_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="incident_reports_updated",
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="incident_reports_submitted",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    correction_requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="incident_report_corrections_requested",
    )
    correction_requested_at = models.DateTimeField(null=True, blank=True)
    correction_note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Incident report for {self.ticket.ticket_id or self.ticket_id}"

    def save(self, *args, **kwargs):
        previous = None
        if self.pk:
            previous = (
                IncidentReport.objects.filter(pk=self.pk)
                .only(
                    "registered_user_id",
                    "notified_user_id",
                    "registered_signature",
                    "notified_signature",
                    "registered_signed_at",
                    "notified_signed_at",
                )
                .first()
            )

        if previous and previous.registered_user_id != self.registered_user_id:
            if self.registered_signature:
                try:
                    self.registered_signature.delete(save=False)
                except Exception:
                    pass
            self.registered_signature = None
            self.registered_signed_at = None

        if previous and previous.notified_user_id != self.notified_user_id:
            if self.notified_signature:
                try:
                    self.notified_signature.delete(save=False)
                except Exception:
                    pass
            self.notified_signature = None
            self.notified_signed_at = None

        if not (self.incident_registered_person or "").strip():
            self.incident_registered_person = incident_report_person_display(getattr(self, "registered_user", None))
        if not (self.incident_notified_person or "").strip():
            self.incident_notified_person = incident_report_person_display(getattr(self, "notified_user", None))
        if self.incident_commander_user_id:
            self.incident_commander = incident_report_person_display(getattr(self, "incident_commander_user", None))
        strip_fields = [
            "reporting_employee_name",
            "reporting_employee_designation",
            "reporting_employee_email",
            "reporting_employee_contact",
            "service_affected",
            "branch_impacted",
            "incident_title",
            "incident_id",
            "detected_at",
            "source_of_incident",
            "incident_location_ip",
            "incident_description",
            "reported_by",
            "incident_commander",
            "severity_level",
            "current_status",
            "summary_what_happened",
            "summary_detected",
            "summary_affected",
            "unit_or_department_impacted",
            "systems_impacted",
            "network_impacted",
            "operations_impacted",
            "impact_branch_department",
            "impact_users",
            "impact_operational",
            "impact_regulatory",
            "timeline_detection",
            "timeline_initial_triage",
            "timeline_containment_started",
            "timeline_recovery_started",
            "timeline_service_restored",
            "timeline_incident_closed",
            "recovery_actions",
            "recovery_timeframe",
            "post_recovery_verification",
            "recovery_communication",
            "containment_actions",
            "temporary_workarounds",
            "escalations_raised",
            "quarantine_process",
            "immediate_actions",
            "root_cause_analysis",
            "eradication_method",
            "lessons_learned",
            "recommendations_for_improvement",
            "action_plan",
            "eradication_root_cause",
            "eradication_fix_applied",
            "eradication_validation_steps",
            "eradication_systems_restored",
            "communication_stakeholders",
            "communication_update_frequency",
            "communication_latest_update",
            "evidence_ticket_case",
            "evidence_logs",
            "evidence_attachments",
            "evidence_vendors",
            "unit_or_department_requiring_notification",
            "point_of_contact",
            "review_root_cause_summary",
            "review_lessons_learned",
            "review_preventive_actions",
            "review_action_owners",
            "incident_registered_person",
            "incident_notified_person",
        ]
        for field_name in strip_fields:
            setattr(self, field_name, (getattr(self, field_name, "") or "").strip())
        return super().save(*args, **kwargs)

    @property
    def formatted_downtime(self):
        minutes = self.downtime_duration_minutes
        if minutes is None:
            return "-"

        total_minutes = int(minutes)
        hours, remainder = divmod(total_minutes, 60)
        days, hours = divmod(hours, 24)
        if days > 0:
            return f"{days}d {hours}h {remainder}m"
        if hours > 0:
            return f"{hours}h {remainder}m"
        return f"{remainder}m"

    @property
    def display_title(self):
        return self.incident_title or getattr(self.ticket, "subject", "") or "Incident Report"

    @property
    def display_incident_reference(self):
        return self.incident_id or self.evidence_ticket_case or getattr(self.ticket, "ticket_id", "") or "-"

    @property
    def display_registered_person(self):
        return self.incident_registered_person or incident_report_person_display(getattr(self, "registered_user", None)) or "-"

    @property
    def display_notified_person(self):
        active_signoffs = list(self.signoffs.filter(role=IncidentReportSignoff.ROLE_NOTIFIED).select_related("user").order_by("level", "id"))
        if active_signoffs:
            parts = []
            final_level = max(signoff.level for signoff in active_signoffs if signoff.level)
            for signoff in active_signoffs:
                if signoff.level == final_level:
                    role_label = "Acknowledged By:"
                else:
                    role_label = "Reviewed By:"
                parts.append(f"{role_label} {signoff.display_person}")
            return ", ".join(parts)
        return self.incident_notified_person or incident_report_person_display(getattr(self, "notified_user", None)) or "-"

    @property
    def active_notified_signoffs(self):
        return self.signoffs.filter(role=IncidentReportSignoff.ROLE_NOTIFIED).select_related("user").order_by("level", "id")

    class Meta:
        ordering = ["-updated_at", "-created_at"]


class IncidentReportSignoff(models.Model):
    ROLE_NOTIFIED = "notified"
    ROLE_CHOICES = [
        (ROLE_NOTIFIED, "Notified"),
    ]

    incident_report = models.ForeignKey(
        IncidentReport,
        on_delete=models.CASCADE,
        related_name="signoffs",
    )
    role = models.CharField(max_length=32, choices=ROLE_CHOICES, default=ROLE_NOTIFIED)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="incident_report_signoffs",
    )
    level = models.PositiveIntegerField(default=1)
    signed_display_name = models.CharField(max_length=255, blank=True, default="")
    snapshot_signature = models.ImageField(
        upload_to=incident_report_signoff_signature_upload_to,
        storage=TicketImageStorage(),
        null=True,
        blank=True,
    )
    signed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        report_ref = getattr(self.incident_report, "display_incident_reference", None) or self.incident_report_id
        return f"Incident signoff {self.get_role_display()} L{self.level} for {report_ref}"

    def save(self, *args, **kwargs):
        previous = None
        if self.pk:
            previous = (
                IncidentReportSignoff.objects.filter(pk=self.pk)
                .only("user_id", "snapshot_signature", "signed_at", "signed_display_name")
                .first()
            )

        if previous and previous.user_id != self.user_id:
            if self.snapshot_signature:
                try:
                    self.snapshot_signature.delete(save=False)
                except Exception:
                    pass
            self.snapshot_signature = None
            self.signed_at = None
            self.signed_display_name = ""

        if self.user_id and not self.signed_display_name:
            self.signed_display_name = incident_report_person_display(getattr(self, "user", None))

        self.signed_display_name = (self.signed_display_name or "").strip()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.snapshot_signature:
            try:
                self.snapshot_signature.delete(save=False)
            except Exception:
                pass
        return super().delete(*args, **kwargs)

    @property
    def display_person(self):
        return self.signed_display_name or incident_report_person_display(getattr(self, "user", None)) or "-"

    class Meta:
        ordering = ["role", "level", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["incident_report", "role", "level"],
                name="incident_report_signoff_unique_level",
            ),
            models.UniqueConstraint(
                fields=["incident_report", "role", "user"],
                name="incident_report_signoff_unique_user",
            ),
        ]


class IncidentReportAttachment(models.Model):
    incident_report = models.ForeignKey(
        IncidentReport,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    file = models.FileField(
        upload_to=incident_report_attachment_upload_to,
        storage=TicketImageStorage(),
    )
    original_name = models.CharField(max_length=255, blank=True, default="")
    content_type = models.CharField(max_length=255, blank=True, default="")
    size = models.BigIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="incident_report_attachments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        report_ref = getattr(self.incident_report, "display_incident_reference", None) or self.incident_report_id
        return f"Incident evidence attachment for {report_ref}: {self.filename}"

    def save(self, *args, **kwargs):
        if self.file:
            self.original_name = (self.original_name or os.path.basename(self.file.name or "")).strip()
            self.size = int(self.size or getattr(self.file, "size", 0) or 0)
            if not self.content_type:
                self.content_type = (getattr(self.file, "content_type", "") or "").strip()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.file:
            try:
                self.file.delete(save=False)
            except Exception:
                pass
        return super().delete(*args, **kwargs)

    @property
    def filename(self):
        return (self.original_name or os.path.basename(getattr(self.file, "name", "") or "") or "attachment").strip()

    class Meta:
        ordering = ["created_at", "id"]


class RemoteAccessApproval(models.Model):
    STATUS_PENDING_RECOMMENDATION = "pending_recommendation"
    STATUS_PENDING_APPROVAL = "pending_approval"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING_RECOMMENDATION, "Pending Recommendation"),
        (STATUS_PENDING_APPROVAL, "Pending Approval"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
    ]

    ticket = models.OneToOneField(
        Ticket,
        on_delete=models.CASCADE,
        related_name="remote_access_approval",
    )
    recommender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="remote_access_approvals_to_recommend",
    )
    second_recommender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="remote_access_approvals_to_second_recommend",
    )
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="remote_access_approvals_to_review",
    )
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_PENDING_APPROVAL)
    recommendation_note = models.TextField(blank=True, default="")
    second_recommendation_note = models.TextField(blank=True, default="")
    decision_note = models.TextField(blank=True, default="")
    requested_at = models.DateTimeField(auto_now_add=True)
    recommended_at = models.DateTimeField(null=True, blank=True)
    second_recommended_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    recommended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="remote_access_approvals_recommended",
    )
    second_recommended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="remote_access_approvals_second_recommended",
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="remote_access_approvals_decided",
    )
    requested_signature_snapshot = models.ImageField(
        upload_to=remote_access_signature_snapshot_upload_to,
        storage=TicketImageStorage(),
        null=True,
        blank=True,
    )
    access_user_signature_snapshot = models.ImageField(
        upload_to=remote_access_signature_snapshot_upload_to,
        storage=TicketImageStorage(),
        null=True,
        blank=True,
    )
    recommended_signature_snapshot = models.ImageField(
        upload_to=remote_access_signature_snapshot_upload_to,
        storage=TicketImageStorage(),
        null=True,
        blank=True,
    )
    second_recommended_signature_snapshot = models.ImageField(
        upload_to=remote_access_signature_snapshot_upload_to,
        storage=TicketImageStorage(),
        null=True,
        blank=True,
    )
    approved_signature_snapshot = models.ImageField(
        upload_to=remote_access_signature_snapshot_upload_to,
        storage=TicketImageStorage(),
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-requested_at", "-id"]

    def __str__(self):
        return f"{self.ticket.ticket_id} - {self.get_status_display()}"

    @classmethod
    def initial_status_for(cls, recommender=None, second_recommender=None) -> str:
        if getattr(recommender, "id", None) or getattr(second_recommender, "id", None):
            return cls.STATUS_PENDING_RECOMMENDATION
        return cls.STATUS_PENDING_APPROVAL

    @property
    def current_stage(self) -> str:
        if self.status == self.STATUS_PENDING_RECOMMENDATION:
            if self.recommender_id and not self.recommended_by_id:
                return "recommendation"
            if self.second_recommender_id and not self.second_recommended_by_id:
                return "second_recommendation"
            return "recommendation"
        if self.status == self.STATUS_PENDING_APPROVAL:
            return "approval"
        return ""

    @property
    def current_stage_label(self) -> str:
        if self.current_stage == "recommendation":
            return "Recommendation"
        if self.current_stage == "second_recommendation":
            return "Second Recommendation"
        if self.current_stage == "approval":
            return "Approval"
        return ""

    @property
    def current_reviewer(self):
        if self.status == self.STATUS_PENDING_RECOMMENDATION:
            if self.recommender_id and not self.recommended_by_id:
                return self.recommender
            if self.second_recommender_id and not self.second_recommended_by_id:
                return self.second_recommender
            return self.recommender or self.second_recommender
        if self.status == self.STATUS_PENDING_APPROVAL:
            return self.approver
        return None

    @property
    def recommendation_status_label(self) -> str:
        if not self.recommender_id:
            return "Not Required"
        if self.status == self.STATUS_PENDING_RECOMMENDATION:
            return "Pending"
        if self.status == self.STATUS_REJECTED and self.recommended_by_id and not self.decided_by_id:
            return "Rejected"
        if self.recommended_by_id:
            return "Recommended"
        return "Pending"

    @property
    def approval_status_label(self) -> str:
        if self.status == self.STATUS_PENDING_RECOMMENDATION:
            return "Waiting for Recommendation"
        if self.status == self.STATUS_PENDING_APPROVAL:
            return "Pending"
        if self.status == self.STATUS_APPROVED:
            return "Approved"
        if self.status == self.STATUS_REJECTED and self.decided_by_id:
            return "Rejected"
        if self.status == self.STATUS_REJECTED:
            return "Not Reached"
        return "Pending"

    def can_decide(self, user) -> bool:
        if not getattr(user, "is_authenticated", False):
            return False
        actor_id = getattr(user, "id", None)
        if self.status == self.STATUS_PENDING_RECOMMENDATION:
            if self.recommender_id and not self.recommended_by_id:
                return bool(actor_id and actor_id == self.recommender_id)
            if self.second_recommender_id and not self.second_recommended_by_id:
                return bool(actor_id and actor_id == self.second_recommender_id)
            return False
        if self.status == self.STATUS_PENDING_APPROVAL:
            return bool(actor_id and actor_id == self.approver_id)
        return False

    def record_decision(self, decision, actor, note=""):
        normalized_decision = (decision or "").strip().lower()
        if normalized_decision not in {self.STATUS_APPROVED, self.STATUS_REJECTED}:
            raise ValueError("Invalid remote access approval decision.")
        if not self.can_decide(actor):
            raise PermissionError("This user cannot decide the current remote access stage.")

        decided_at = timezone.now()
        normalized_note = (note or "").strip()
        update_fields = ["status"]
        if self.status == self.STATUS_PENDING_RECOMMENDATION:
            if self.recommender_id and not self.recommended_by_id:
                self.recommendation_note = normalized_note
                self.recommended_at = decided_at
                self.recommended_by = actor
                self.status = (
                    self.STATUS_PENDING_RECOMMENDATION
                    if normalized_decision == self.STATUS_APPROVED and self.second_recommender_id
                    else self.STATUS_PENDING_APPROVAL
                    if normalized_decision == self.STATUS_APPROVED
                    else self.STATUS_REJECTED
                )
                update_fields.extend(["recommendation_note", "recommended_at", "recommended_by"])
                if normalized_decision == self.STATUS_APPROVED:
                    self.copy_signature_snapshot("recommended_signature_snapshot", actor, save=False)
                    if self.recommended_signature_snapshot:
                        update_fields.append("recommended_signature_snapshot")
            else:
                self.second_recommendation_note = normalized_note
                self.second_recommended_at = decided_at
                self.second_recommended_by = actor
                self.status = (
                    self.STATUS_PENDING_APPROVAL
                    if normalized_decision == self.STATUS_APPROVED
                    else self.STATUS_REJECTED
                )
                update_fields.extend(["second_recommendation_note", "second_recommended_at", "second_recommended_by"])
                if normalized_decision == self.STATUS_APPROVED:
                    self.copy_signature_snapshot("second_recommended_signature_snapshot", actor, save=False)
                    if self.second_recommended_signature_snapshot:
                        update_fields.append("second_recommended_signature_snapshot")
        else:
            self.status = normalized_decision
            self.decision_note = normalized_note
            self.decided_at = decided_at
            self.decided_by = actor
            update_fields.extend(["decision_note", "decided_at", "decided_by"])
            if normalized_decision == self.STATUS_APPROVED:
                self.copy_signature_snapshot("approved_signature_snapshot", actor, save=False)
                if self.approved_signature_snapshot:
                    update_fields.append("approved_signature_snapshot")
        self.save(update_fields=update_fields)

    def copy_signature_snapshot(self, field_name, user, save=True):
        signature = getattr(user, "signature_image", None)
        if not signature:
            return False
        try:
            signature.open("rb")
            payload = signature.read()
        except Exception:
            return False
        finally:
            try:
                signature.close()
            except Exception:
                pass
        if not payload:
            return False

        existing = getattr(self, field_name, None)
        if existing:
            try:
                existing.delete(save=False)
            except Exception:
                pass

        source_name = os.path.basename(signature.name or f"{field_name}.png")
        target_name = f"{field_name}_{source_name}"
        getattr(self, field_name).save(target_name, ContentFile(payload), save=False)
        if save:
            self.save(update_fields=[field_name])
        return True


def _get_ticket_remote_access_approval(ticket):
    if ticket is None:
        return None
    state = getattr(ticket, "_state", None)
    fields_cache = getattr(state, "fields_cache", {}) if state is not None else {}
    if "remote_access_approval" in fields_cache:
        return fields_cache["remote_access_approval"]
    if (getattr(ticket, "subject", "") or "").strip().casefold() != "remote access request":
        return None
    try:
        return ticket.remote_access_approval
    except (AttributeError, RemoteAccessApproval.DoesNotExist):
        return None


def can_access_ticket_chat(user, ticket) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if _get_ticket_remote_access_approval(ticket) is not None:
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
    if _get_ticket_remote_access_approval(ticket) is not None:
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
    status = models.CharField(max_length=32, choices=Ticket.TICKET_STATUS, blank=True, default="")

    class Meta:
        ordering = ["-assigned_at"]
        indexes = [
            models.Index(fields=["ticket", "assigned_at"], name="assign_ticket_assigned_idx"),
            models.Index(fields=["assigned_to", "assigned_at"], name="assign_assignee_assigned_idx"),
            models.Index(fields=["unassigned_at"], name="assign_unassigned_idx"),
        ]

    @property
    def effective_unassigned_at(self):
        if self.unassigned_at:
            return self.unassigned_at
        ticket = getattr(self, "ticket", None)
        if not ticket:
            return None
        return ticket.resolved_at or ticket.closed_at

    @property
    def history_status(self):
        ticket = getattr(self, "ticket", None)
        if not ticket:
            return self.status or ""

        if not self.unassigned_at:
            if ticket.status == "closed" and ticket.resolved_at:
                return "resolved"
            return ticket.status or self.status or ""

        if self.status:
            return self.status

        if ticket.resolved_at and self.unassigned_at == ticket.resolved_at:
            return "resolved"

        if ticket.closed_at and self.unassigned_at == ticket.closed_at:
            return "closed"

        return ""

    @property
    def history_status_display(self):
        status = self.history_status
        return dict(Ticket.TICKET_STATUS).get(status, status or "")

    @property
    def duration(self):
        end = self.effective_unassigned_at or timezone.now()
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


class TicketReminderSummaryLog(models.Model):
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ticket_reminder_summary_logs",
    )
    sent_at = models.DateTimeField(default=timezone.now)
    ticket_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-sent_at"]
        indexes = [
            models.Index(fields=["assignee", "sent_at"], name="ticketrem_assignee_sent_idx"),
        ]


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
    VISIBILITY_BRANCH = "branch"
    VISIBILITY_DEPARTMENT = "department"
    VISIBILITY_RESTRICTED = "restricted"
    VISIBILITY_SUPPORT_ONLY = "support_only"
    VISIBILITY_CHOICES = [
        (VISIBILITY_PUBLIC, "All users"),
        (VISIBILITY_BRANCH, "Branch"),
        (VISIBILITY_DEPARTMENT, "Department"),
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
    allowed_departments = models.ManyToManyField(
        "accounts.Department",
        blank=True,
        related_name="department_scoped_technical_documents",
    )
    allowed_branches = models.ManyToManyField(
        "accounts.Branch",
        blank=True,
        related_name="branch_scoped_technical_documents",
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

    def branch_scope_display(self) -> str:
        prefetched = getattr(self, "_prefetched_objects_cache", {}).get("allowed_branches")
        if prefetched is not None:
            branch_names = sorted(branch.name for branch in prefetched)
        else:
            branch_names = list(self.allowed_branches.order_by("name").values_list("name", flat=True))
        return ", ".join(branch_names) if branch_names else "All branches"

    def department_scope_display(self) -> str:
        prefetched = getattr(self, "_prefetched_objects_cache", {}).get("allowed_departments")
        if prefetched is not None:
            department_names = sorted(department.name for department in prefetched)
        else:
            department_names = list(
                self.allowed_departments.order_by("name").values_list("name", flat=True)
            )
        return ", ".join(department_names) if department_names else "All departments"

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


class PortalFlashAnnouncement(models.Model):
    CATEGORY_IT = "it"
    CATEGORY_BANK = "bank"
    CATEGORY_CHOICES = [
        (CATEGORY_IT, "IT Related"),
        (CATEGORY_BANK, "Bank Related"),
    ]

    category = models.CharField(max_length=16, choices=CATEGORY_CHOICES, default=CATEGORY_IT)
    title = models.CharField(max_length=255)
    message = models.TextField(blank=True, default="")
    image = models.ImageField(
        upload_to=portal_flash_image_upload_to,
        storage=TicketImageStorage(),
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="portal_flash_announcements",
    )
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(default=default_portal_flash_end_at)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["created_at"], name="portal_flash_created_idx"),
        ]

    def __str__(self):
        return self.title or f"Portal flash #{self.pk}"

    @property
    def is_active(self):
        now = timezone.now()
        return self.starts_at <= now <= self.ends_at

    def delete(self, *args, **kwargs):
        image_name = getattr(self.image, "name", "")
        image_storage = getattr(self.image, "storage", None)
        result = super().delete(*args, **kwargs)
        if image_name and image_storage:
            try:
                image_storage.delete(image_name)
            except Exception:
                pass
        return result


class PortalFlashAnnouncementReceipt(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="portal_flash_announcement_receipts",
    )
    announcement = models.ForeignKey(
        PortalFlashAnnouncement,
        on_delete=models.CASCADE,
        related_name="receipts",
    )
    shown_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "announcement"],
                name="portal_flash_receipt_unique",
            )
        ]
        indexes = [
            models.Index(fields=["user", "shown_at"], name="portal_flash_user_idx"),
        ]
        ordering = ["-shown_at", "-id"]

    def __str__(self):
        return f"{self.user_id}:{self.announcement_id}"
