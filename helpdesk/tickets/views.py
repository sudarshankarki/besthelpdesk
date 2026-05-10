import base64
import hashlib
import hmac
import json
import mimetypes
import os
import posixpath
import secrets
import shutil
import subprocess
import tempfile
import time
import zipfile
from datetime import date, datetime, timedelta, timezone as dt_timezone
from html import escape
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree as ET

from django.contrib.auth.decorators import login_required
from django.template.loader import render_to_string
from django.contrib.auth.decorators import user_passes_test
from django.contrib import messages
from django.contrib.sessions.models import Session
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.core.files.uploadhandler import FileUploadHandler
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.db import IntegrityError, transaction
from django.db.utils import OperationalError, ProgrammingError
from django.http import FileResponse, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.db.models import Count, Max, OuterRef, Q, Subquery
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import get_valid_filename
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_POST

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from PIL import Image, ImageDraw, ImageFont

from .chat_rules import is_ticket_chat_locked, ticket_chat_locked_message
from .minio import get_minio_config, get_s3_client
from accounts.auth_mode import is_agent_workload_view_enabled
from accounts.models import Branch, CustomUser, Department
from accounts.utils import get_outgoing_from_email
from .models import (
    PortalFlashAnnouncement,
    IncidentReport,
    IncidentReportAttachment,
    IncidentReportSignoff,
    RemoteAccessApproval,
    TechnicalDocument,
    Ticket,
    TicketAssignmentLog,
    TicketChatReadState,
    TicketMessage,
    TicketMessageAttachment,
    can_access_ticket_chat,
    can_manage_ticket_chat_privacy,
    incident_report_person_display,
    parse_department_list,
    parse_email_list,
)
from .forms import (
    CBSAccessRequestForm,
    CBS_BRANCH_USER_GROUP_CHOICES,
    CBS_USER_GROUP_CHOICES,
    IncidentReportForm,
    IncidentReportNotifiedSignoffFormSet,
    IncidentResponseTemplateForm,
    RemoteAccessApprovalDecisionForm,
    RemoteAccessRequestForm,
    TicketAssigneeUpdateForm,
    TicketChatPrivacyForm,
    TicketForm,
    TicketUpdateForm,
)
from .notifications import build_chat_notification_payload, get_chat_notification_target_ids
from .purge import _try_delete_minio_objects

try:
    from botocore.exceptions import ClientError  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    ClientError = None  # type: ignore


TICKET_CHAT_ATTACHMENT_MAX_FILES = 5
INCIDENT_REPORT_ATTACHMENT_MAX_FILES = 5
TECH_DOC_ALLOWED_EXTENSIONS = {".pdf", ".xls", ".xlsx"}
TECH_DOC_ALLOWED_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
SUPPORT_STATUS_GROUPS = {
    "new": ("new", "acknowledged"),
    "in_progress": ("in_progress", "waiting_on_user", "waiting_on_third_party"),
    "resolved": ("resolved",),
    "closed": ("closed", "cancelled_duplicate"),
}
IT_SUPPORT_DEPARTMENT_NAME = "IT"
IT_SUPPORT_BRANCH_NAME = "Head Office"
TECH_DOC_EXCEL_PREVIEWABLE_EXTENSIONS = {".xlsx"}
TECH_DOC_EXCEL_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
TECH_DOC_EXCEL_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WORD_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
CBS_ACCESS_TEMPLATE_DOCX = os.path.join(settings.BASE_DIR, "templates", "forms", "cbs_access_request_template.docx")
CBS_ACCESS_BRANCH_TEMPLATE_DOCX = os.path.join(settings.BASE_DIR, "templates", "forms", "cbs_access_branch_request_template.docx")
BFC_INCIDENT_REPORT_TEMPLATE_DOCX = os.path.join(
    settings.BASE_DIR,
    "templates",
    "forms",
    "BFC- Incident Report Template.docx",
)
INCIDENT_REPORT_TEMPLATE_DOCX = BFC_INCIDENT_REPORT_TEMPLATE_DOCX
if not os.path.exists(INCIDENT_REPORT_TEMPLATE_DOCX):
    alternate_path = os.path.join(settings.BASE_DIR, "templates", "forms", "incident_report_template.docx")
    if os.path.exists(alternate_path):
        INCIDENT_REPORT_TEMPLATE_DOCX = alternate_path
    else:
        INCIDENT_REPORT_TEMPLATE_DOCX = os.path.join(settings.BASE_DIR, "templates", "forms", "Incident Report Template.docx")
DOCX_LOGO_IMAGE_PATHS = (
    os.path.join(settings.BASE_DIR, "static", "images", "logo.png"),
    os.path.join(settings.BASE_DIR, "staticfiles", "images", "logo.png"),
)
DOCX_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DOCX_WORD_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DOCX_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
ET.register_namespace("w", WORD_NS["w"])
ET.register_namespace("r", DOCX_WORD_REL_NS)
ET.register_namespace("rel", DOCX_REL_NS)
ET.register_namespace("ct", DOCX_CONTENT_TYPES_NS)
ET.register_namespace("wp", "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing")
ET.register_namespace("a", "http://schemas.openxmlformats.org/drawingml/2006/main")
ET.register_namespace("pic", "http://schemas.openxmlformats.org/drawingml/2006/picture")


def _serialize_docx_package_xml(root, namespace, prefix):
    payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    namespace_declaration = f' xmlns:{prefix}="{namespace}"'.encode("utf-8")
    return (
        payload.replace(namespace_declaration, f' xmlns="{namespace}"'.encode("utf-8"))
        .replace(f"<{prefix}:".encode("utf-8"), b"<")
        .replace(f"</{prefix}:".encode("utf-8"), b"</")
    )


class RequestOnlyMemoryFileUploadHandler(FileUploadHandler):
    """Keep status-email attachments in memory so they are never persisted on disk."""

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.file = BytesIO()

    def receive_data_chunk(self, raw_data, start):
        self.file.write(raw_data)

    def file_complete(self, file_size):
        self.file.seek(0)
        return InMemoryUploadedFile(
            file=self.file,
            field_name=self.field_name,
            name=self.file_name,
            content_type=self.content_type,
            size=file_size,
            charset=self.charset,
            content_type_extra=self.content_type_extra,
        )


def _use_request_only_upload_handlers(request):
    request.upload_handlers = [RequestOnlyMemoryFileUploadHandler(request)]


def _format_ws_datetime(dt):
    return timezone.localtime(dt).strftime("%Y-%m-%d %H:%M")


def _clean_query_value(value):
    return (value or "").strip()


def _parse_filter_date(value):
    value = _clean_query_value(value)
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_local_datetime_input(value):
    value = _clean_query_value(value)
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _get_support_filters(request):
    allowed_priorities = {value for value, _label in Ticket.PRIORITY_CHOICES}
    filters = {
        "q": _clean_query_value(request.GET.get("q")),
        "status": _clean_query_value(request.GET.get("status")),
        "priority": _clean_query_value(request.GET.get("priority")),
        "department": _clean_query_value(request.GET.get("department")),
        "branch": _clean_query_value(request.GET.get("branch")),
        "status_group": _clean_query_value(request.GET.get("status_group")),
        "created_by_username": _clean_query_value(request.GET.get("created_by_username")),
        "assigned_to_username": _clean_query_value(request.GET.get("assigned_to_username")),
        "assignment_scope": _clean_query_value(request.GET.get("assignment_scope")),
        "date_from": _clean_query_value(request.GET.get("date_from")),
        "date_to": _clean_query_value(request.GET.get("date_to")),
    }
    if filters["status_group"] not in SUPPORT_STATUS_GROUPS:
        filters["status_group"] = ""
    if filters["priority"] not in allowed_priorities:
        filters["priority"] = ""
    if filters["assignment_scope"] not in {"", "unassigned", "assigned"}:
        filters["assignment_scope"] = ""
    restricted_branch = _restricted_support_branch_for_department(filters["department"])
    if restricted_branch:
        filters["branch"] = restricted_branch
    date_from = _parse_filter_date(filters["date_from"])
    date_to = _parse_filter_date(filters["date_to"])
    if date_from and date_to and date_from > date_to:
        filters["date_from"], filters["date_to"] = filters["date_to"], filters["date_from"]
    return filters


def _apply_support_filters(queryset, filters, *, include_approval_tickets=False, include_special_requests=False):
    if not include_approval_tickets:
        queryset = queryset.filter(remote_access_approval__isnull=True)
    if not include_special_requests:
        queryset = queryset.exclude(request_type__in=("incident", "cbs_access_ho", "cbs_access_branch"))
    if filters["status_group"]:
        queryset = queryset.filter(status__in=SUPPORT_STATUS_GROUPS[filters["status_group"]])
    if filters["status"]:
        queryset = queryset.filter(status=filters["status"])
    if filters["priority"]:
        queryset = queryset.filter(priority=filters["priority"])
    if filters["department"]:
        queryset = queryset.filter(department__iexact=filters["department"])
    if filters["branch"]:
        queryset = queryset.filter(
            Q(branch__iexact=filters["branch"])
            | (Q(branch="") & Q(created_by__branch__iexact=filters["branch"]))
        )
    if filters["q"]:
        queryset = queryset.filter(ticket_id__icontains=filters["q"])
    if filters["created_by_username"]:
        queryset = queryset.filter(created_by__username__icontains=filters["created_by_username"])
    if filters["assignment_scope"] == "unassigned":
        queryset = queryset.filter(assigned_to__isnull=True)
    elif filters["assignment_scope"] == "assigned":
        queryset = queryset.filter(assigned_to__isnull=False)
    elif filters["assigned_to_username"]:
        matching_user_ids = set(
            CustomUser.objects.filter(username__icontains=filters["assigned_to_username"]).values_list("id", flat=True)
        )
        resolved_owner_ticket_ids = []
        if matching_user_ids:
            resolved_owner_ticket_ids = [
                ticket_id
                for ticket_id, owner_id in _resolved_owner_ids_by_ticket_id(
                    queryset.filter(status__in=SUPPORT_STATUS_GROUPS["resolved"])
                ).items()
                if owner_id in matching_user_ids
            ]
        queryset = queryset.filter(
            Q(assigned_to__username__icontains=filters["assigned_to_username"])
            | Q(id__in=resolved_owner_ticket_ids)
        )

    date_from = _parse_filter_date(filters["date_from"])
    if date_from:
        queryset = queryset.filter(created_at__date__gte=date_from)

    date_to = _parse_filter_date(filters["date_to"])
    if date_to:
        queryset = queryset.filter(created_at__date__lte=date_to)

    return queryset


def _has_active_support_filters(filters):
    return any(filters.values())


def _build_agent_workload(tickets, *, limit=10, filters=None, queue_url_name=None):
    # Count active workload from the current assignee, then add resolved tickets
    # back to the agent who last owned and resolved them.
    active_ticket_workload = tickets.filter(assigned_to__isnull=False)
    active_ticket_workload = active_ticket_workload.exclude(
        status__in=SUPPORT_STATUS_GROUPS["closed"] + SUPPORT_STATUS_GROUPS["resolved"]
    )
    workload_by_assignee = {
        item["assigned_to__id"]: item["total"]
        for item in active_ticket_workload.values("assigned_to__id").annotate(total=Count("id"))
    }
    resolved_owner_ids_by_ticket_id = _resolved_owner_ids_by_ticket_id(
        tickets.filter(status__in=SUPPORT_STATUS_GROUPS["resolved"])
    )
    for owner_id in resolved_owner_ids_by_ticket_id.values():
        workload_by_assignee[owner_id] = workload_by_assignee.get(owner_id, 0) + 1

    support_agents = CustomUser.objects.filter(
        Q(is_itsupport=True)
        | Q(is_staff=True)
        | Q(assigned_tickets__isnull=False)
        | Q(resolved_tickets__isnull=False)
        | Q(ticket_assignment_logs__status="resolved")
    ).distinct().values("id", "username")
    agent_workload = []
    for user in support_agents:
        agent_workload.append(
            {
                "assigned_to__id": user["id"],
                "assigned_to__username": user["username"],
                "total": workload_by_assignee.get(user["id"], 0),
            }
        )

    agent_workload = sorted(
        agent_workload,
        key=lambda item: (-item["total"], item["assigned_to__username"] or ""),
    )
    visible_workload = agent_workload if limit is None else agent_workload[:limit]

    if queue_url_name:
        resolved_filters = filters or {}
        for item in visible_workload:
            item["queue_url"] = _build_support_url(
                queue_url_name,
                resolved_filters,
                assigned_to_username=item["assigned_to__username"],
            )

    return visible_workload


def _agent_workload_total_for_user(agent_workload, user):
    user_id = getattr(user, "id", None)
    if not user_id:
        return 0
    for item in agent_workload:
        if item.get("assigned_to__id") == user_id:
            return item.get("total", 0)
    return 0


def _build_support_url(route_name, filters, **overrides):
    params = {}
    for key, value in {**filters, **overrides}.items():
        if value in {None, ""}:
            continue
        params[key] = value
    base_url = reverse(route_name)
    query = urlencode(params)
    return f"{base_url}?{query}" if query else base_url


def _count_support_status_group(queryset, group_name):
    return queryset.filter(status__in=SUPPORT_STATUS_GROUPS[group_name]).count()


def _count_support_status_group_by_assignment(queryset, group_name, is_assigned):
    return queryset.filter(
        status__in=SUPPORT_STATUS_GROUPS[group_name],
        assigned_to__isnull=not is_assigned,
    ).count()


def _support_department_filter_options(selected_department=""):
    department_names_by_key = {}

    for name in Department.objects.values_list("name", flat=True):
        cleaned_name = (name or "").strip()
        if not cleaned_name:
            continue
        department_names_by_key.setdefault(cleaned_name.casefold(), cleaned_name)

    cleaned_selected_department = (selected_department or "").strip()
    if cleaned_selected_department:
        department_names_by_key.setdefault(
            cleaned_selected_department.casefold(),
            cleaned_selected_department,
        )

    return sorted(department_names_by_key.values(), key=str.casefold)


def _restricted_support_branch_for_department(selected_department=""):
    cleaned_department = (selected_department or "").strip()
    if cleaned_department.casefold() == IT_SUPPORT_DEPARTMENT_NAME.casefold():
        return IT_SUPPORT_BRANCH_NAME
    return ""


def _support_branch_filter_options(selected_branch="", selected_department=""):
    restricted_branch = _restricted_support_branch_for_department(selected_department)
    if restricted_branch:
        return [restricted_branch]

    branch_names_by_key = {}

    for name in Branch.objects.values_list("name", flat=True):
        cleaned_name = (name or "").strip()
        if not cleaned_name:
            continue
        branch_names_by_key.setdefault(cleaned_name.casefold(), cleaned_name)

    cleaned_selected_branch = (selected_branch or "").strip()
    if cleaned_selected_branch:
        branch_names_by_key.setdefault(cleaned_selected_branch.casefold(), cleaned_selected_branch)

    return sorted(branch_names_by_key.values(), key=str.casefold)


def _department_ticket_support_queryset(user):
    department = _user_department_name(user)
    if not department:
        return Ticket.objects.none()
    return (
        Ticket.objects.select_related("created_by", "assigned_to")
        .order_by("-created_at")
        .filter(department__iexact=department)
    )


def _normalize_department(value):
    return (value or "").strip().casefold()


def _normalize_branch(value):
    return (value or "").strip().casefold()


def _user_department_name(user):
    return (getattr(user, "department", "") or "").strip()


def _user_branch_name(user):
    return (getattr(user, "branch", "") or "").strip()


def _ticket_branch_name(ticket):
    branch = (getattr(ticket, "branch", "") or "").strip()
    if branch:
        return branch
    requester = getattr(ticket, "created_by", None)
    return (getattr(requester, "branch", "") or "").strip()


def _department_ticket_q(user):
    department = _user_department_name(user)
    branch = _user_branch_name(user)
    if not department or not branch:
        return Q(pk__in=[])
    department_q = Q(department__iexact=department) | (
        Q(request_type="incident") & Q(additional_departments__icontains=department)
    )
    return department_q & (
        Q(branch__iexact=branch) | (Q(branch="") & Q(created_by__branch__iexact=branch))
    )


def _remote_access_ticket_q(user):
    if not getattr(user, "is_authenticated", False):
        return Q(pk__in=[])
    return (
        Q(created_by=user, remote_access_approval__isnull=False)
        | Q(remote_access_approval__recommender=user)
        | Q(remote_access_approval__recommended_by=user)
        | Q(remote_access_approval__second_recommender=user)
        | Q(remote_access_approval__second_recommended_by=user)
        | Q(remote_access_approval__approver=user)
        | Q(remote_access_approval__decided_by=user)
    )


def _incident_report_signer_ticket_q(user):
    if not getattr(user, "is_authenticated", False):
        return Q(pk__in=[])
    return (
        Q(request_type="incident")
        & (
            Q(incident_report__registered_user=user)
            | Q(incident_report__notified_user=user)
            | Q(incident_report__incident_commander_user=user)
            | Q(
                incident_report__signoffs__role=IncidentReportSignoff.ROLE_NOTIFIED,
                incident_report__signoffs__user=user,
            )
        )
    )


def _is_department_ticket_member(user, ticket):
    if not getattr(user, "is_authenticated", False):
        return False
    user_department = _normalize_department(_user_department_name(user))
    ticket_department = _normalize_department(getattr(ticket, "department", ""))
    additional_departments = {
        _normalize_department(item)
        for item in parse_department_list(getattr(ticket, "additional_departments", ""))
    }
    user_branch = _normalize_branch(_user_branch_name(user))
    ticket_branch = _normalize_branch(_ticket_branch_name(ticket))
    department_matches = user_department == ticket_department or (
        getattr(ticket, "request_type", "") == "incident"
        and user_department in additional_departments
    )
    return bool(
        user_department
        and user_branch
        and ticket_branch
        and department_matches
        and user_branch == ticket_branch
    )


def _can_claim_department_ticket(user, ticket):
    if not getattr(user, "is_authenticated", False):
        return False
    if _is_support_user(user):
        return False
    if not _is_department_ticket_member(user, ticket):
        return False
    if ticket.assigned_to_id:
        return False
    if ticket.status in {"resolved", "closed", "cancelled_duplicate"}:
        return False
    if ticket.created_by_id == user.id:
        return False
    return True


def _apply_ticket_display_status(ticket, remote_access_approval=None):
    solved_statuses = {"resolved", "closed", "cancelled_duplicate"}
    approval_kind = _approval_request_kind(ticket)
    cbs_approved = (
        approval_kind == "CBS Access"
        and remote_access_approval is not None
        and remote_access_approval.status == RemoteAccessApproval.STATUS_APPROVED
    )
    if remote_access_approval is not None and not cbs_approved:
        ticket.display_status_value = remote_access_approval.status
        ticket.display_status_label = remote_access_approval.get_status_display()
        ticket.display_status_chip_class = _status_chip_class(remote_access_approval.status)
        ticket.show_priority_badge = False
    else:
        ticket.display_status_value = ticket.status
        ticket.display_status_label = ticket.get_status_display()
        ticket.display_status_chip_class = _status_chip_class(ticket.status)
        ticket.show_priority_badge = True
        if cbs_approved and ticket.status not in solved_statuses:
            ticket.display_status_label = f"Approved / {ticket.get_status_display()}"
    return cbs_approved


def _notify_user(user_id, payload):
    if not user_id:
        return
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        async_to_sync(channel_layer.group_send)(
            f"user_notify_{user_id}",
            {"type": "notify", "payload": payload},
        )
    except Exception:
        return


def _ticket_detail_url(request, ticket):
    return request.build_absolute_uri(reverse("ticket_detail", args=[ticket.id]))


def _format_user_contact(user):
    username = getattr(user, "username", "") or "-"
    email = (getattr(user, "email", "") or "").strip()
    if email:
        return f"{username} <{email}>"
    return username


def _build_assignment_email_body(request, ticket, assigned_by):
    assignee_name = getattr(ticket.assigned_to, "first_name", "") or getattr(ticket.assigned_to, "username", "") or "there"
    description = (ticket.description or "").strip() or "-"
    remote_access_approval = _get_remote_access_approval(ticket)
    approved_cbs_attachment_note = ""
    if (
        _approval_request_kind(ticket) == "CBS Access"
        and remote_access_approval is not None
        and remote_access_approval.status == RemoteAccessApproval.STATUS_APPROVED
    ):
        approved_cbs_attachment_note = "\nApproved CBS access request document is attached with this email.\n"
    return (
        f"Dear {assignee_name},\n\n"
        f"I hope you are doing well.\n\n"
        f"{_format_user_contact(ticket.created_by)} has raised the following ticket for service.\n\n"
        f"Ticket ID: {ticket.ticket_id}\n"
        f"Subject: {ticket.subject}\n"
        f"Status: {ticket.get_status_display()}\n"
        f"Priority: {ticket.get_priority_display()}\n"
        f"Department: {ticket.department or '-'}\n"
        f"Request Type: {ticket.get_request_type_display()}\n"
        f"Requester: {_format_user_contact(ticket.created_by)}\n"
        f"Assigned By: {_format_user_contact(assigned_by)}\n\n"
        f"User Message:\n{description}\n\n"
        f"{approved_cbs_attachment_note}"
        f"Open Ticket:\n{_ticket_detail_url(request, ticket)}\n"
    )


def _incident_report_signoff_url(request, ticket, sign_role=""):
    base_url = request.build_absolute_uri(reverse("ticket_incident_report", args=[ticket.id]))
    if sign_role:
        return f"{base_url}?{urlencode({'sign_role': sign_role})}#incident-sign-off"
    return f"{base_url}#incident-sign-off"


def _incident_report_formal_signoff_labels(signoffs):
    ordered_signoffs = sorted(
        [signoff for signoff in signoffs if getattr(signoff, "level", None)],
        key=lambda signoff: (signoff.level, getattr(signoff, "id", 0) or 0),
    )
    if not ordered_signoffs:
        return {}
    final_level = max(signoff.level for signoff in ordered_signoffs)
    labels = {}
    for signoff in ordered_signoffs:
        if signoff.level == final_level:
            labels[getattr(signoff, "id", signoff.level)] = "Acknowledged By:"
        else:
            labels[getattr(signoff, "id", signoff.level)] = "Reviewed By:"
    return labels


def _apply_incident_report_formal_signoff_labels(signoffs):
    labels = _incident_report_formal_signoff_labels(signoffs)
    for signoff in signoffs:
        signoff.formal_label = labels.get(getattr(signoff, "id", None), "Acknowledged By:")
    return signoffs


def _apply_incident_report_signoff_sequence(signoffs, user=None):
    ordered_signoffs = _apply_incident_report_formal_signoff_labels(list(signoffs))
    for signoff in ordered_signoffs:
        earlier_unsigned = [
            item
            for item in ordered_signoffs
            if item.level < signoff.level and item.user_id and not item.snapshot_signature
        ]
        signoff.waiting_for_prior_signoff = bool(earlier_unsigned)
        signoff.waiting_for_label = ", ".join(item.formal_label for item in earlier_unsigned)
        signoff.assigned_to_current_user = bool(getattr(user, "is_authenticated", False)) and signoff.user_id == user.id
        signoff.can_sign_now = (
            signoff.assigned_to_current_user
            and not signoff.snapshot_signature
            and not earlier_unsigned
        )
    return ordered_signoffs


def _build_incident_report_signer_email_body(request, ticket, incident_report, recipient, assigned_by, roles):
    recipient_name = getattr(recipient, "get_full_name", lambda: "")().strip() or getattr(recipient, "username", "") or "there"
    role_lines = []
    for role_key, role_label in roles:
        role_lines.append(f"- {role_label}: {_incident_report_signoff_url(request, ticket, role_key)}")

    service_label = incident_report.get_service_affected_display() if getattr(incident_report, "service_affected", "") else "-"
    return (
        f"Dear {recipient_name},\n\n"
        f"I hope you are doing well.\n\n"
        f"You have been assigned to sign an incident report in the BestSupport portal.\n\n"
        f"Incident Ticket ID: {ticket.ticket_id}\n"
        f"Subject: {ticket.subject}\n"
        f"Service Affected: {service_label}\n"
        f"Current Status: {incident_report.current_status or '-'}\n"
        f"Assigned By: {_format_user_contact(assigned_by)}\n\n"
        f"Sign-Off Roles:\n" + "\n".join(role_lines) + "\n\n"
        f"Open Incident Report:\n{_incident_report_signoff_url(request, ticket)}\n\n"
        "Please sign while logged in with your own portal account. "
        "Only the assigned user can apply that signature.\n\n"
        "If your signature is not available yet, please contact the system administrator to upload it on your user profile.\n"
    )


def _notify_incident_report_signers(request, ticket, incident_report, assigned_by, previous_signer_state=None):
    previous_signer_state = previous_signer_state or {}
    pending_by_user_id = {}
    registered_signer = getattr(incident_report, "registered_user", None)
    if (
        registered_signer is not None
        and registered_signer.id != getattr(assigned_by, "id", None)
        and previous_signer_state.get("registered_user_id") != registered_signer.id
    ):
        pending_by_user_id.setdefault(registered_signer.id, {"user": registered_signer, "roles": []})["roles"].append(
            ("registered", "Incident Registered By")
        )

    previous_notified_assignments = set(previous_signer_state.get("notified_assignments") or [])
    for signoff in _apply_incident_report_formal_signoff_labels(list(_ordered_notified_signoffs(incident_report))):
        signer = signoff.user
        if signer is None or signer.id == getattr(assigned_by, "id", None):
            continue

        assignment_key = (signoff.user_id, signoff.level)
        if assignment_key in previous_notified_assignments:
            continue

        pending_by_user_id.setdefault(signer.id, {"user": signer, "roles": []})["roles"].append(
            (f"notified-{signoff.id}", f"{signoff.formal_label} for Incident Response Report")
        )

    warnings = []
    sent_count = 0
    for payload in pending_by_user_id.values():
        signer = payload["user"]
        recipient_email = (getattr(signer, "email", "") or "").strip()
        signer_name = getattr(signer, "get_full_name", lambda: "")().strip() or getattr(signer, "username", "") or "Selected user"
        if not recipient_email:
            warnings.append(f"{signer_name} has no email address for incident sign-off notification.")
            continue

        subject = f"Incident Report Sign-Off Needed: {ticket.ticket_id}"
        body = _build_incident_report_signer_email_body(
            request,
            ticket,
            incident_report,
            signer,
            assigned_by,
            payload["roles"],
        )
        try:
            _send_email_message(subject, body, [recipient_email])
        except Exception:
            warnings.append(f"Incident sign-off email could not be sent to {signer_name}.")
            continue

        sent_count += 1
        role_summary = ", ".join(role_label for _role_key, role_label in payload["roles"])
        _notify_user(
            signer.id,
            {
                "kind": "incident_report_signoff",
                "level": "warning",
                "title": "Incident report signature needed",
                "message": f"{ticket.ticket_id}: {role_summary} assigned to you",
                "url": reverse("ticket_incident_report", args=[ticket.id]),
                "ticket_id": ticket.id,
                "ticket_code": ticket.ticket_id,
                "delay": 15000,
            },
        )

    return sent_count, warnings


def _build_incident_report_submission_email_body(request, ticket, incident_report):
    submitted_by = _format_user_contact(getattr(incident_report, "updated_by", None) or getattr(incident_report, "created_by", None))
    severity_label = incident_report.get_severity_choice_display() or incident_report.severity_level or "-"
    service_label = incident_report.get_service_affected_display() if getattr(incident_report, "service_affected", "") else "-"
    notified_chain = incident_report.display_notified_person
    return (
        f"Dear Team,\n\n"
        f"The incident response template for ticket {ticket.ticket_id} has been submitted from BestSupport.\n\n"
        f"Incident Ticket ID: {ticket.ticket_id}\n"
        f"Incident Reference: {incident_report.display_incident_reference}\n"
        f"Subject: {incident_report.display_title}\n"
        f"Severity: {severity_label}\n"
        f"Service Affected: {service_label}\n"
        f"Current Status: {incident_report.current_status or '-'}\n"
        f"Submitted By: {submitted_by}\n"
        f"Notified Users: {notified_chain}\n\n"
        f"Open Incident Report:\n{_incident_report_signoff_url(request, ticket)}\n\n"
        "The latest incident response template is attached as image file(s) for review.\n"
    )


def _build_incident_report_submission_attachments(incident_report):
    docx_source = _incident_response_template_source_from_report(incident_report)
    image_payloads = _incident_response_template_image_payloads(docx_source, image_format="png")
    if not image_payloads:
        raise ValueError("Incident report image export is unavailable.")
    base_name = (
        (docx_source.get("incident_title") or "").strip()
        or (docx_source.get("incident_id") or "").strip()
        or "incident-response-template"
    )
    safe_name = get_valid_filename(base_name).replace(" ", "_")
    return [
        (f"{safe_name}-page-{index}.png", image_payload, "image/png")
        for index, image_payload in enumerate(image_payloads, start=1)
    ]


def _build_incident_report_submission_attachment(incident_report):
    return _build_incident_report_submission_attachments(incident_report)[0]


def _send_incident_report_submission_email(request, ticket, incident_report, cc_users=None):
    notified_emails = []
    cc_emails = []
    warnings = []
    seen_to = set()
    seen_cc = set()

    for signoff in _ordered_notified_signoffs(incident_report):
        signer = signoff.user
        if signer is None:
            continue
        signer_name = incident_report_person_display(signer) or "Selected user"
        signer_email = (getattr(signer, "email", "") or "").strip()
        if not signer_email:
            warnings.append(f"{signer_name} does not have an email address, so no submission email was sent to that user.")
            continue
        signer_key = signer_email.casefold()
        if signer_key in seen_to:
            continue
        seen_to.add(signer_key)
        notified_emails.append(signer_email)

    if not notified_emails:
        raise ValueError("Add at least one reviewer or approver with an email address before submitting the incident report.")

    for user in cc_users or []:
        email = (getattr(user, "email", "") or "").strip()
        if not email:
            display_name = incident_report_person_display(user) or getattr(user, "username", "") or "Selected CC user"
            warnings.append(f"{display_name} does not have an email address, so that CC recipient was skipped.")
            continue
        email_key = email.casefold()
        if email_key in seen_to or email_key in seen_cc:
            continue
        seen_cc.add(email_key)
        cc_emails.append(email)

    subject = f"Incident Response Submitted: {incident_report.display_incident_reference}"
    body = _build_incident_report_submission_email_body(request, ticket, incident_report)
    attachments = _build_incident_report_submission_attachments(incident_report)
    _send_email_message(subject, body, notified_emails, cc_list=cc_emails, email_attachments=attachments)
    return warnings


def _is_incident_report_locked(ticket, incident_report):
    return bool(
        incident_report is not None
        and not getattr(incident_report, "correction_requested_at", None)
        and (getattr(incident_report, "submitted_at", None) or getattr(ticket, "status", "") == "closed")
    )


def _mark_incident_ticket_resolved_after_submission(request, ticket, incident_report, actor):
    if ticket.status in {"resolved", "closed"}:
        return False

    incident_ref = incident_report.display_incident_reference
    ticket.status = "resolved"
    ticket.resolved_by = actor
    ticket.resolved_note = (
        f"Resolved after incident report submission ({incident_ref})."
        if incident_ref
        else "Resolved after incident report submission."
    )
    ticket.save()

    if ticket.created_by_id and ticket.created_by_id != getattr(actor, "id", None):
        _notify_user(
            ticket.created_by_id,
            {
                "kind": "ticket_status",
                "level": "success",
                "title": "Ticket resolved",
                "message": f"{ticket.ticket_id} was resolved after incident report submission",
                "url": reverse("ticket_detail", args=[ticket.id]),
                "ticket_id": ticket.id,
                "ticket_code": ticket.ticket_id,
                "status": ticket.status,
                "updated_by": actor.get_username(),
            },
        )
    return True


def _build_new_ticket_email_body(request, ticket):
    assigned_to = getattr(ticket.assigned_to, "username", "") or "Unassigned"
    description = (ticket.description or "").strip() or "-"
    return (
        f"Dear Support Team,\n\n"
        f"I hope you are doing well.\n\n"
        f"{_format_user_contact(ticket.created_by)} has raised the following ticket for service.\n\n"
        f"Ticket ID: {ticket.ticket_id}\n"
        f"Subject: {ticket.subject}\n"
        f"Department: {ticket.department or '-'}\n"
        f"Request Type: {ticket.get_request_type_display()}\n"
        f"Impact: {ticket.get_impact_display()}\n"
        f"Urgency: {ticket.get_urgency_display()}\n"
        f"Priority: {ticket.get_priority_display()}\n"
        f"Requester: {_format_user_contact(ticket.created_by)}\n"
        f"Assigned To: {assigned_to}\n\n"
        f"User Message:\n{description}\n\n"
        f"Open Ticket:\n{_ticket_detail_url(request, ticket)}\n"
    )


def _remote_access_pending_stage(remote_access_approval):
    stage = getattr(remote_access_approval, "current_stage", "")
    if stage == "recommendation":
        return {
            "stage": "recommendation",
            "stage_label": "Recommendation",
            "stage_label_lower": "recommendation",
            "reviewer": getattr(remote_access_approval, "recommender", None),
            "notification_title": "Remote access recommendation needed",
            "notification_message": "requested recommendation",
            "email_subject": "Remote Access Recommendation Needed",
        }
    if stage == "second_recommendation":
        return {
            "stage": "second_recommendation",
            "stage_label": "Second Recommendation",
            "stage_label_lower": "second recommendation",
            "reviewer": getattr(remote_access_approval, "second_recommender", None),
            "notification_title": "Remote access second recommendation needed",
            "notification_message": "requested second recommendation",
            "email_subject": "Remote Access Second Recommendation Needed",
        }
    return {
        "stage": "approval",
        "stage_label": "Approval",
        "stage_label_lower": "approval",
        "reviewer": getattr(remote_access_approval, "approver", None),
        "notification_title": "Remote access approval needed",
        "notification_message": "requested approval",
        "email_subject": "Remote Access Approval Needed",
    }


def _approval_request_kind(ticket):
    request_type = (getattr(ticket, "request_type", "") or "").strip()
    if request_type in {"cbs_access_ho", "cbs_access_branch"}:
        return "CBS Access"
    if (getattr(ticket, "subject", "") or "").strip().casefold() == "cbs access request":
        return "CBS Access"
    return "Remote Access"


def _cbs_access_office_type_from_request_type(request_type):
    return "branch" if (request_type or "").strip() == "cbs_access_branch" else "head_office"


def _cbs_access_request_type_for_office(office_type):
    return "cbs_access_branch" if (office_type or "").strip() == "branch" else "cbs_access_ho"


def _cbs_access_template_path(request_type=None):
    if _cbs_access_office_type_from_request_type(request_type) == "branch":
        return CBS_ACCESS_BRANCH_TEMPLATE_DOCX
    return CBS_ACCESS_TEMPLATE_DOCX


def _cbs_access_group_choices(request_type=None):
    if _cbs_access_office_type_from_request_type(request_type) == "branch":
        return CBS_BRANCH_USER_GROUP_CHOICES
    return CBS_USER_GROUP_CHOICES


def _cbs_access_office_label(request_type=None):
    return "Branch Office" if _cbs_access_office_type_from_request_type(request_type) == "branch" else "Head Office"


def _notify_remote_access_reviewer(request, ticket, remote_access_approval):
    stage_meta = _remote_access_pending_stage(remote_access_approval)
    reviewer = stage_meta["reviewer"]
    if reviewer is None:
        return None, None
    request_kind = _approval_request_kind(ticket)
    stage_meta = {
        **stage_meta,
        "notification_title": f"{request_kind} {stage_meta['stage_label_lower']} needed",
        "notification_message": f"requested {stage_meta['stage_label_lower']}",
        "email_subject": f"{request_kind} {stage_meta['stage_label']} Needed",
        "request_kind": request_kind,
    }

    if reviewer.id != request.user.id:
        requester_name = request.user.get_full_name().strip() or request.user.username
        _notify_user(
            reviewer.id,
            {
                "kind": "remote_access_approval",
                "level": "warning",
                "title": stage_meta["notification_title"],
                "message": f"{requester_name} {stage_meta['notification_message']} for {ticket.ticket_id}: {ticket.subject}",
                "url": reverse("ticket_detail", args=[ticket.id]),
                "ticket_id": ticket.id,
                "ticket_code": ticket.ticket_id,
                "delay": 20000,
            },
        )
    return reviewer, stage_meta


def _build_remote_access_request_email_body(request, ticket, remote_access_approval):
    stage_meta = _remote_access_pending_stage(remote_access_approval)
    request_kind = _approval_request_kind(ticket)
    reviewer = stage_meta["reviewer"]
    reviewer_name = getattr(reviewer, "get_full_name", lambda: "")().strip() or getattr(reviewer, "username", "") or "there"
    description = (ticket.description or "").strip() or "-"
    body = (
        f"Dear {reviewer_name},\n\n"
        f"I hope you are doing well.\n\n"
        f"{_format_user_contact(ticket.created_by)} has submitted a {request_kind.lower()} request.\n"
    )
    if stage_meta["stage"] == "recommendation":
        body += "When convenient, please review the details below and let us know whether this request should move forward for approval.\n\n"
    elif stage_meta["stage"] == "second_recommendation":
        body += "This request has already received the first recommendation and is now waiting for your second recommendation.\n\n"
    else:
        if remote_access_approval.recommender_id and remote_access_approval.recommended_by_id:
            body += "This request has already been recommended and is now waiting for your final approval.\n\n"
        else:
            body += "When convenient, please review the details below and let us know whether this request may be approved.\n\n"
    body += (
        f"Request ID: {ticket.ticket_id}\n"
        f"Subject: {ticket.subject}\n"
        f"Status: {remote_access_approval.get_status_display()}\n"
        f"Requested By: {_format_user_contact(ticket.created_by)}\n"
        f"Recommended By: {_format_user_contact(getattr(remote_access_approval, 'recommender', None)) if remote_access_approval.recommender_id else 'Not Required'}\n"
        f"Second Recommended By: {_format_user_contact(getattr(remote_access_approval, 'second_recommender', None)) if remote_access_approval.second_recommender_id else 'Not Required'}\n"
        f"Approved By: {_format_user_contact(getattr(remote_access_approval, 'approver', None))}\n"
        f"{stage_meta['stage_label']}: {_format_user_contact(reviewer)}\n"
    )
    if remote_access_approval.recommended_at:
        body += f"Recommended At: {_format_ws_datetime(remote_access_approval.recommended_at)}\n"
    if remote_access_approval.recommendation_note:
        body += f"Recommendation Note:\n{remote_access_approval.recommendation_note.strip()}\n"
    if remote_access_approval.second_recommended_at:
        body += f"Second Recommended At: {_format_ws_datetime(remote_access_approval.second_recommended_at)}\n"
    if remote_access_approval.second_recommendation_note:
        body += f"Second Recommendation Note:\n{remote_access_approval.second_recommendation_note.strip()}\n"
    body += (
        "\n"
        f"Request Details:\n{description}\n\n"
        f"Open Request:\n{_ticket_detail_url(request, ticket)}\n\n"
        f"Thank you for your time and support.\n"
    )
    return body


def _build_remote_access_decision_email_body(request, ticket, remote_access_approval):
    request_kind = _approval_request_kind(ticket)
    requester_name = ticket.created_by.get_full_name().strip() or ticket.created_by.username or "there"
    decision_stage = "Approval"
    decided_by = getattr(remote_access_approval, "decided_by", None) or getattr(remote_access_approval, "approver", None)
    decision_note = (remote_access_approval.decision_note or "").strip()
    decided_at = remote_access_approval.decided_at
    if remote_access_approval.status == RemoteAccessApproval.STATUS_REJECTED and not remote_access_approval.decided_by_id:
        if remote_access_approval.second_recommended_by_id:
            decision_stage = "Second Recommendation"
            decided_by = getattr(remote_access_approval, "second_recommended_by", None) or getattr(remote_access_approval, "second_recommender", None)
            decision_note = (remote_access_approval.second_recommendation_note or "").strip()
            decided_at = remote_access_approval.second_recommended_at
        else:
            decision_stage = "Recommendation"
            decided_by = getattr(remote_access_approval, "recommended_by", None) or getattr(remote_access_approval, "recommender", None)
            decision_note = (remote_access_approval.recommendation_note or "").strip()
            decided_at = remote_access_approval.recommended_at
    body = (
        f"Dear {requester_name},\n\n"
        f"I hope you are doing well.\n\n"
        f"This is a courtesy update regarding your {request_kind.lower()} request.\n"
        f"The request has been {remote_access_approval.get_status_display().lower()}.\n\n"
        f"Request ID: {ticket.ticket_id}\n"
        f"Subject: {ticket.subject}\n"
        f"Status: {remote_access_approval.get_status_display()}\n"
        f"Requested By: {_format_user_contact(ticket.created_by)}\n"
        f"Recommended By: {_format_user_contact(getattr(remote_access_approval, 'recommender', None)) if remote_access_approval.recommender_id else 'Not Required'}\n"
        f"Second Recommended By: {_format_user_contact(getattr(remote_access_approval, 'second_recommender', None)) if remote_access_approval.second_recommender_id else 'Not Required'}\n"
        f"Approved By: {_format_user_contact(getattr(remote_access_approval, 'approver', None))}\n"
        f"Decision Stage: {decision_stage}\n"
        f"Decision By: {_format_user_contact(decided_by)}\n"
    )
    if decided_at:
        body += f"Decided At: {_format_ws_datetime(decided_at)}\n"
    body += f"\nRequest Details:\n{(ticket.description or '').strip() or '-'}\n"
    if decision_note:
        if request_kind == "CBS Access" and remote_access_approval.status == RemoteAccessApproval.STATUS_APPROVED:
            body += f"\nMessage / CBS User ID:\n{decision_note}\n"
        else:
            body += f"\nMessage:\n{decision_note}\n"
    if request_kind == "CBS Access" and remote_access_approval.status == RemoteAccessApproval.STATUS_REJECTED:
        body += (
            "\nYou can correct the CBS access request document from the ticket page and submit it again for approval.\n"
        )
    body += (
        f"\nOpen Request:\n{_ticket_detail_url(request, ticket)}\n\n"
        f"Thank you.\n"
    )
    return body


def _build_email_attachments(uploads):
    email_attachments = []
    for upload in uploads or []:
        try:
            if hasattr(upload, "seek"):
                upload.seek(0)
            email_attachments.append(
                (
                    upload.name,
                    upload.read(),
                    getattr(upload, "content_type", "") or "application/octet-stream",
                )
            )
            if hasattr(upload, "seek"):
                upload.seek(0)
        except Exception:
            continue
    return email_attachments


def _clean_email_recipients(recipients):
    cleaned = []
    seen = set()
    for value in recipients or []:
        email = (value or "").strip()
        normalized = email.lower()
        if not email or normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(email)
    return cleaned


def _send_email_message(subject, body, recipient_list, cc_list=None, email_attachments=None):
    to_recipients = _clean_email_recipients(recipient_list)
    to_keys = {email.lower() for email in to_recipients}
    cc_recipients = [
        email
        for email in _clean_email_recipients(cc_list)
        if email.lower() not in to_keys
    ]
    email = EmailMessage(
        subject=subject,
        body=body,
        from_email=get_outgoing_from_email(),
        to=to_recipients,
        cc=cc_recipients,
    )
    for attachment in email_attachments or []:
        email.attach(*attachment)
    email.send(fail_silently=False)


def _approved_cbs_access_assignment_attachments(ticket):
    remote_access_approval = _get_remote_access_approval(ticket)
    if (
        _approval_request_kind(ticket) != "CBS Access"
        or remote_access_approval is None
        or remote_access_approval.status != RemoteAccessApproval.STATUS_APPROVED
    ):
        return []
    return _build_cbs_access_email_attachments(ticket, remote_access_approval)


def _cbs_assignment_user_options():
    return CustomUser.objects.filter(is_active=True).order_by("first_name", "last_name", "username")


def _cbs_assignment_department_options():
    department_names = {
        (name or "").strip()
        for name in Department.objects.values_list("name", flat=True)
        if (name or "").strip()
    }
    return sorted(department_names, key=str.casefold)


def _send_assignment_email(request, ticket, assigned_by, action_label, email_attachments=None, cc_list=None):
    assignee_email = (getattr(ticket.assigned_to, "email", "") or "").strip()
    if not assignee_email:
        messages.warning(request, "Ticket assigned, but the assignee has no email set.")
        return

    mail_subject = f"Ticket Assigned: {ticket.ticket_id}"
    mail_body = _build_assignment_email_body(request, ticket, assigned_by)
    email_attachments = list(email_attachments or [])
    try:
        email_attachments.extend(_approved_cbs_access_assignment_attachments(ticket))
    except Exception:
        messages.warning(request, "Ticket assigned, but the approved CBS document could not be attached.")
    try:
        _send_email_message(mail_subject, mail_body, [assignee_email], cc_list=cc_list, email_attachments=email_attachments)
    except Exception:
        messages.warning(request, f"{action_label}, but assignment email could not be sent.")


def _status_chip_class(status_value):
    normalized = (status_value or "").strip().lower()
    if normalized in {"approved", "resolved"}:
        return "chip-success"
    if normalized in {"rejected", "closed", "cancelled_duplicate"}:
        return "chip-danger"
    if normalized in {"pending", "pending_recommendation", "pending_approval", "in_progress", "waiting_on_user", "waiting_on_third_party"}:
        return "chip-warning"
    if normalized in {"new", "open", "acknowledged"}:
        return "chip-primary"
    return "chip-muted"


def _ticket_close_signer():
    return TimestampSigner(salt="tickets.close")


def _make_ticket_close_token(ticket):
    return _ticket_close_signer().sign(f"{ticket.id}:{ticket.created_by_id}")


def _validate_ticket_close_token(ticket, token, max_age_seconds):
    expected = f"{ticket.id}:{ticket.created_by_id}"
    value = _ticket_close_signer().unsign(token, max_age=max_age_seconds)
    return value == expected


def _is_support_user(user):
    return user.is_authenticated and (user.is_staff or user.is_superuser or user.is_itsupport)


def _is_admin_user(user):
    return user.is_authenticated and (user.is_staff or user.is_superuser)


def _session_last_seen_at(session_data):
    raw_value = session_data.get("active_seen_ts")
    try:
        timestamp = int(raw_value)
    except (TypeError, ValueError):
        return None

    try:
        return datetime.fromtimestamp(timestamp, tz=dt_timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _get_currently_logged_in_users(request):
    now = timezone.now()
    online_window_minutes = max(int(getattr(settings, "USER_ACTIVITY_ONLINE_MINUTES", 15)), 1)
    cutoff = now - timedelta(minutes=online_window_minutes)
    last_seen_by_user_id = {}

    for session in Session.objects.filter(expire_date__gte=now):
        try:
            session_data = session.get_decoded()
        except Exception:
            continue

        user_id = session_data.get("_auth_user_id")
        if not user_id:
            continue

        seen_at = _session_last_seen_at(session_data)
        if not seen_at or seen_at < cutoff:
            continue

        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            continue

        previous_seen_at = last_seen_by_user_id.get(user_id)
        if previous_seen_at is None or seen_at > previous_seen_at:
            last_seen_by_user_id[user_id] = seen_at

    current_user = getattr(request, "user", None)
    if getattr(current_user, "is_authenticated", False):
        current_seen_at = last_seen_by_user_id.get(current_user.id)
        if current_seen_at is None or now > current_seen_at:
            last_seen_by_user_id[current_user.id] = now

    users_by_id = CustomUser.objects.in_bulk(last_seen_by_user_id.keys())
    active_users = [
        {
            "user": user,
            "last_seen_at": last_seen_by_user_id[user_id],
        }
        for user_id, user in users_by_id.items()
    ]
    active_users.sort(
        key=lambda row: (
            -row["last_seen_at"].timestamp(),
            (row["user"].get_full_name() or row["user"].get_username()).casefold(),
        )
    )
    return active_users, online_window_minutes


def _can_view_tech_doc(user, document: TechnicalDocument) -> bool:
    if _is_support_user(user):
        return True

    visibility = getattr(document, "visibility", TechnicalDocument.VISIBILITY_PUBLIC)
    if visibility == TechnicalDocument.VISIBILITY_PUBLIC:
        return True
    if visibility == TechnicalDocument.VISIBILITY_BRANCH:
        if not document.allowed_branches.exists():
            return True
        user_branch = _normalize_branch(_user_branch_name(user))
        return bool(
            user_branch and document.allowed_branches.filter(name__iexact=_user_branch_name(user)).exists()
        )
    if visibility == TechnicalDocument.VISIBILITY_DEPARTMENT:
        user_department = _normalize_department(_user_department_name(user))
        if not user_department or not document.allowed_departments.filter(
            name__iexact=_user_department_name(user)
        ).exists():
            return False
        if not document.allowed_branches.exists():
            return True
        user_branch = _normalize_branch(_user_branch_name(user))
        return bool(
            user_branch and document.allowed_branches.filter(name__iexact=_user_branch_name(user)).exists()
        )
    if visibility == TechnicalDocument.VISIBILITY_SUPPORT_ONLY:
        return False
    if visibility == TechnicalDocument.VISIBILITY_RESTRICTED:
        return document.allowed_users.filter(id=user.id).exists()

    return False


def _tech_doc_visibility_q(user):
    user_branch = _user_branch_name(user)
    user_department = _user_department_name(user)

    visibility_q = Q(visibility=TechnicalDocument.VISIBILITY_PUBLIC)
    visibility_q |= Q(
        visibility=TechnicalDocument.VISIBILITY_BRANCH,
        allowed_branches__isnull=True,
    )
    visibility_q |= Q(
        visibility=TechnicalDocument.VISIBILITY_RESTRICTED,
        allowed_users=user,
    )

    if user_branch:
        visibility_q |= Q(
            visibility=TechnicalDocument.VISIBILITY_BRANCH,
            allowed_branches__name__iexact=user_branch,
        )

    if user_department:
        visibility_q |= Q(
            visibility=TechnicalDocument.VISIBILITY_DEPARTMENT,
            allowed_departments__name__iexact=user_department,
            allowed_branches__isnull=True,
        )
        if user_branch:
            visibility_q |= Q(
                visibility=TechnicalDocument.VISIBILITY_DEPARTMENT,
                allowed_departments__name__iexact=user_department,
                allowed_branches__name__iexact=user_branch,
            )

    return visibility_q


def _tech_docs_upload_context(form_values=None):
    values = {
        "visibility": TechnicalDocument.VISIBILITY_PUBLIC,
        "allowed_users": "",
        "branches": [],
        "departments": [],
    }
    if form_values:
        values.update(form_values)

    branches = Branch.objects.order_by("name").only("branch_id", "name")
    departments = Department.objects.order_by("name").only("id", "name")
    return {
        "doc_visibility_choices": TechnicalDocument.VISIBILITY_CHOICES,
        "tech_doc_form": values,
        "tech_doc_branch_options": [(branch.branch_id, branch.name) for branch in branches],
        "tech_doc_department_options": [(str(department.id), department.name) for department in departments],
    }


def _selected_tech_doc_branches(selected_branch_ids):
    branch_ids = list(dict.fromkeys(value for value in selected_branch_ids if value))
    branches = list(Branch.objects.filter(branch_id__in=branch_ids).only("branch_id", "name"))
    found_ids = {branch.branch_id for branch in branches}
    missing_ids = sorted(set(branch_ids) - found_ids)
    return branches, missing_ids


def _selected_tech_doc_departments(selected_department_ids):
    department_ids = list(dict.fromkeys(value for value in selected_department_ids if value))
    departments = list(Department.objects.filter(id__in=department_ids).only("id", "name"))
    found_ids = {str(department.id) for department in departments}
    missing_ids = sorted(set(department_ids) - found_ids)
    return departments, missing_ids


def _tech_doc_extension(document: TechnicalDocument) -> str:
    return os.path.splitext(document.filename or "")[1].lower()


def _stream_s3_body(body):
    try:
        while True:
            chunk = body.read(1024 * 256)
            if not chunk:
                break
            yield chunk
    finally:
        body.close()


def _read_s3_body_bytes(body) -> bytes:
    try:
        return body.read()
    finally:
        body.close()


def _excel_column_index(cell_reference: str) -> int:
    letters = []
    for character in (cell_reference or "").upper():
        if "A" <= character <= "Z":
            letters.append(character)
            continue
        break
    column_index = 0
    for letter in letters:
        column_index = (column_index * 26) + (ord(letter) - ord("A") + 1)
    return column_index


def _excel_column_label(column_index: int) -> str:
    label = []
    while column_index > 0:
        column_index, remainder = divmod(column_index - 1, 26)
        label.append(chr(ord("A") + remainder))
    return "".join(reversed(label)) or "A"


def _excel_text_content(node) -> str:
    if node is None:
        return ""
    parts = []
    for text_node in node.findall(".//main:t", TECH_DOC_EXCEL_NS):
        parts.append(text_node.text or "")
    return "".join(parts)


def _excel_shared_strings(archive: zipfile.ZipFile):
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    except ET.ParseError as exc:
        raise ValueError("This Excel workbook could not be previewed.") from exc
    return [_excel_text_content(item) for item in root.findall("main:si", TECH_DOC_EXCEL_NS)]


def _excel_relationship_target(base_path: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_path), target))


def _excel_cell_value(cell, shared_strings) -> str:
    cell_type = cell.get("t")
    formula = cell.find("main:f", TECH_DOC_EXCEL_NS)
    value_node = cell.find("main:v", TECH_DOC_EXCEL_NS)
    raw_value = value_node.text if value_node is not None else ""

    if cell_type == "inlineStr":
        return _excel_text_content(cell.find("main:is", TECH_DOC_EXCEL_NS))
    if cell_type == "s":
        try:
            return shared_strings[int(raw_value)]
        except (IndexError, TypeError, ValueError):
            return raw_value or ""
    if cell_type == "b":
        return "TRUE" if raw_value == "1" else "FALSE"
    if raw_value not in {"", None}:
        return raw_value
    if formula is not None and formula.text:
        return f"={formula.text}"
    return ""


def _excel_preview_sheet(sheet_name: str, sheet_bytes: bytes, anchor: str, shared_strings):
    try:
        root = ET.fromstring(sheet_bytes)
    except ET.ParseError as exc:
        raise ValueError("This Excel workbook could not be previewed.") from exc

    sheet_data = root.find("main:sheetData", TECH_DOC_EXCEL_NS)
    if sheet_data is None:
        return {
            "anchor": anchor,
            "name": sheet_name,
            "columns": [],
            "rows": [],
        }

    parsed_rows = []
    max_column_index = 0
    for fallback_row_number, row in enumerate(sheet_data.findall("main:row", TECH_DOC_EXCEL_NS), start=1):
        row_number = int(row.get("r") or fallback_row_number)
        cells = {}
        next_column_index = 1
        for cell in row.findall("main:c", TECH_DOC_EXCEL_NS):
            cell_reference = cell.get("r") or ""
            column_index = _excel_column_index(cell_reference) if cell_reference else next_column_index
            if column_index <= 0:
                column_index = next_column_index
            next_column_index = column_index + 1
            cells[column_index] = _excel_cell_value(cell, shared_strings)
        if not cells:
            continue
        max_column_index = max(max_column_index, max(cells))
        parsed_rows.append({"number": row_number, "cells": cells})

    columns = [_excel_column_label(index) for index in range(1, max_column_index + 1)]
    rows = [
        {
            "number": row["number"],
            "values": [row["cells"].get(index, "") for index in range(1, max_column_index + 1)],
        }
        for row in parsed_rows
    ]
    return {
        "anchor": anchor,
        "name": sheet_name,
        "columns": columns,
        "rows": rows,
    }


def _excel_preview_sheets(workbook_bytes: bytes):
    try:
        archive = zipfile.ZipFile(BytesIO(workbook_bytes))
    except zipfile.BadZipFile as exc:
        raise ValueError("This Excel workbook could not be previewed.") from exc

    with archive:
        try:
            workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
            workbook_rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        except KeyError as exc:
            raise ValueError("This Excel workbook could not be previewed.") from exc
        except ET.ParseError as exc:
            raise ValueError("This Excel workbook could not be previewed.") from exc

        relationship_targets = {
            relationship.get("Id"): _excel_relationship_target(
                "xl/workbook.xml",
                relationship.get("Target") or "",
            )
            for relationship in workbook_rels_root.findall("rel:Relationship", TECH_DOC_EXCEL_NS)
            if relationship.get("Id") and relationship.get("Target")
        }
        shared_strings = _excel_shared_strings(archive)
        sheets = []
        for index, sheet in enumerate(
            workbook_root.findall("main:sheets/main:sheet", TECH_DOC_EXCEL_NS),
            start=1,
        ):
            sheet_name = sheet.get("name") or f"Sheet {index}"
            relationship_id = sheet.get(f"{{{TECH_DOC_EXCEL_REL_NS}}}id")
            target_path = relationship_targets.get(relationship_id or "")
            if not target_path:
                sheets.append(
                    {
                        "anchor": f"sheet-{index}",
                        "name": sheet_name,
                        "columns": [],
                        "rows": [],
                    }
                )
                continue
            try:
                sheet_bytes = archive.read(target_path)
            except KeyError:
                sheets.append(
                    {
                        "anchor": f"sheet-{index}",
                        "name": sheet_name,
                        "columns": [],
                        "rows": [],
                    }
                )
                continue
            sheets.append(
                _excel_preview_sheet(
                    sheet_name=sheet_name,
                    sheet_bytes=sheet_bytes,
                    anchor=f"sheet-{index}",
                    shared_strings=shared_strings,
                )
            )
    if not sheets:
        raise ValueError("This Excel workbook does not contain any sheets.")
    return sheets


def _is_ticket_participant(user, ticket):
    remote_access_approval = _get_remote_access_approval(ticket)
    if not getattr(user, "is_authenticated", False):
        return False
    has_ticket_access = (
        user.is_staff
        or user.is_superuser
        or user.is_itsupport
        or ticket.created_by_id == user.id
        or ticket.assigned_to_id == user.id
        or _is_department_ticket_member(user, ticket)
    )
    if remote_access_approval is not None:
        return (
            has_ticket_access
            or remote_access_approval.recommender_id == user.id
            or remote_access_approval.recommended_by_id == user.id
            or remote_access_approval.second_recommender_id == user.id
            or remote_access_approval.second_recommended_by_id == user.id
            or remote_access_approval.approver_id == user.id
            or remote_access_approval.decided_by_id == user.id
        )
    return (
        has_ticket_access
        or (
            ticket.request_type == "incident"
            and _is_incident_report_signer(user, _get_incident_report(ticket))
        )
    )


def _get_remote_access_approval(ticket):
    try:
        return ticket.remote_access_approval
    except RemoteAccessApproval.DoesNotExist:
        return None


def _get_incident_report(ticket):
    try:
        return ticket.incident_report
    except IncidentReport.DoesNotExist:
        return None


def _is_incident_report_signer(user, incident_report):
    if not getattr(user, "is_authenticated", False) or incident_report is None:
        return False
    if incident_report.incident_commander_user_id == user.id:
        return True
    if incident_report.registered_user_id == user.id or incident_report.notified_user_id == user.id:
        return True
    return incident_report.signoffs.filter(role=IncidentReportSignoff.ROLE_NOTIFIED, user_id=user.id).exists()


def _can_access_incident_report(user, ticket, incident_report=None):
    if _is_ticket_participant(user, ticket):
        return True
    if incident_report is None:
        incident_report = _get_incident_report(ticket)
    return _is_incident_report_signer(user, incident_report)


def _can_manage_incident_report(user, ticket):
    if not getattr(user, "is_authenticated", False):
        return False
    incident_report = _get_incident_report(ticket)
    if incident_report is not None and incident_report.incident_commander_user_id == user.id:
        return True
    return _is_support_user(user) or ticket.created_by_id == user.id


def _ordered_notified_signoffs(incident_report):
    if incident_report is None:
        return IncidentReportSignoff.objects.none()
    return incident_report.signoffs.filter(role=IncidentReportSignoff.ROLE_NOTIFIED).select_related("user").order_by("level", "id")


def _ordered_incident_report_attachments(incident_report):
    if incident_report is None:
        return []
    try:
        return list(incident_report.attachments.select_related("uploaded_by").order_by("created_at", "id"))
    except (OperationalError, ProgrammingError):
        return []


def _incident_report_attachment_notes_for_pdf(incident_report):
    attachment_names = [attachment.filename for attachment in _ordered_incident_report_attachments(incident_report)]
    if not attachment_names:
        return ""
    return "Uploaded files:\n" + "\n".join(f"- {name}" for name in attachment_names)


def _incident_report_signature_status(incident_report):
    missing = []
    if incident_report is None:
        return {"complete": False, "missing": ["Create and save the incident report first."]}

    if not getattr(incident_report, "registered_signature", None):
        missing.append("Incident registered by signature")

    for signoff in _apply_incident_report_formal_signoff_labels(list(_ordered_notified_signoffs(incident_report))):
        if signoff.user_id and not signoff.snapshot_signature:
            missing.append(f"{signoff.formal_label} signature")

    return {"complete": not missing, "missing": missing}


def _incident_report_signoff_level_count_from_request(request, default=2):
    try:
        return max(1, min(int(request.POST.get("incident_signoff_level_count") or default), 6))
    except (TypeError, ValueError):
        return default


def _clear_incident_signoffs_from_level(incident_report, level):
    if incident_report is None or not level:
        return 0
    cleared = 0
    signoffs = incident_report.signoffs.filter(
        role=IncidentReportSignoff.ROLE_NOTIFIED,
        level__gte=level,
    )
    for signoff in signoffs:
        if signoff.snapshot_signature:
            try:
                signoff.snapshot_signature.delete(save=False)
            except Exception:
                pass
        if signoff.snapshot_signature or signoff.signed_at or signoff.signed_display_name:
            signoff.snapshot_signature = None
            signoff.signed_at = None
            signoff.signed_display_name = ""
            signoff.save(update_fields=["snapshot_signature", "signed_at", "signed_display_name", "updated_at"])
            cleared += 1
    return cleared


def _notify_incident_report_correction_requested(request, ticket, incident_report, signoff, note):
    recipients = []
    for user in (
        getattr(ticket, "created_by", None),
        getattr(incident_report, "created_by", None),
        getattr(incident_report, "incident_commander_user", None),
    ):
        if user and user.id not in {recipient.id for recipient in recipients}:
            recipients.append(user)

    actor_name = request.user.get_full_name().strip() or request.user.username
    for user in recipients:
        if user.id != request.user.id:
            _notify_user(
                user.id,
                {
                    "kind": "incident_report_correction",
                    "level": "warning",
                    "title": "Incident report correction requested",
                    "message": f"{ticket.ticket_id}: correction requested by {actor_name}",
                    "url": reverse("ticket_incident_report", args=[ticket.id]),
                    "ticket_id": ticket.id,
                    "ticket_code": ticket.ticket_id,
                    "delay": 12000,
                },
            )

    recipient_emails = [
        (getattr(user, "email", "") or "").strip()
        for user in recipients
        if user.id != request.user.id and (getattr(user, "email", "") or "").strip()
    ]
    if not recipient_emails:
        return

    formal_label = getattr(signoff, "formal_label", "Reviewer")
    body = (
        f"Dear Team,\n\n"
        f"Correction has been requested for an incident report in BestSupport.\n\n"
        f"Ticket ID: {ticket.ticket_id}\n"
        f"Subject: {ticket.subject}\n"
        f"Requested By: {_format_user_contact(request.user)}\n"
        f"Review Stage: {formal_label}\n\n"
        f"Correction Note:\n{note or '-'}\n\n"
        f"Open Incident Report:\n{_incident_report_signoff_url(request, ticket)}\n\n"
        f"Please update the report and save it again to resend the sign-off request.\n"
    )
    try:
        _send_email_message(
            f"Incident Report Correction Requested: {ticket.ticket_id}",
            body,
            recipient_emails,
        )
    except Exception:
        messages.warning(request, "Correction was recorded, but notification email could not be sent.")


def _incident_report_resolution_blockers(ticket):
    incident_report = _get_incident_report(ticket)
    if incident_report is None:
        return ["Create and submit the incident report before resolving this incident ticket."]
    signature_status = _incident_report_signature_status(incident_report)
    blockers = []
    if not signature_status["complete"]:
        blockers.append("Complete all required incident report signatures: " + ", ".join(signature_status["missing"]))
    if not getattr(incident_report, "submitted_at", None):
        blockers.append("Submit & Send the incident report before resolving this incident ticket.")
    return blockers


def _can_decide_remote_access_approval(user, remote_access_approval):
    if remote_access_approval is None:
        return False
    return remote_access_approval.can_decide(user)


def _build_same_host_webrtc_ice_servers(request):
    if not getattr(settings, "WEBRTC_USE_HOST_TURN_FALLBACK", True):
        return []

    host = urlsplit(f"//{request.get_host()}").hostname or request.get_host()
    if not host:
        return []

    ice_servers = [{"urls": f"stun:{host}:{settings.WEBRTC_STUN_PORT}"}]
    if settings.WEBRTC_TURN_AUTH_SECRET or (settings.WEBRTC_TURN_USERNAME and settings.WEBRTC_TURN_PASSWORD):
        turn_server = {
            "urls": [
                f"turn:{host}:{settings.WEBRTC_TURN_PORT}?transport=udp",
                f"turn:{host}:{settings.WEBRTC_TURN_PORT}?transport=tcp",
                f"turns:{host}:{settings.WEBRTC_TURNS_PORT}?transport=tcp",
            ],
        }
        if settings.WEBRTC_TURN_USERNAME and settings.WEBRTC_TURN_PASSWORD:
            turn_server["username"] = settings.WEBRTC_TURN_USERNAME
            turn_server["credential"] = settings.WEBRTC_TURN_PASSWORD
        if settings.WEBRTC_TURN_CREDENTIAL_TYPE:
            turn_server["credentialType"] = settings.WEBRTC_TURN_CREDENTIAL_TYPE
        ice_servers.append(turn_server)
    return ice_servers


def _build_temporary_turn_credentials(user):
    auth_secret = getattr(settings, "WEBRTC_TURN_AUTH_SECRET", "")
    if not auth_secret:
        return None

    ttl_seconds = max(int(getattr(settings, "WEBRTC_TURN_CREDENTIAL_TTL_SECONDS", 3600)), 60)
    expires_at = int(time.time()) + ttl_seconds
    user_token = getattr(user, "username", "") or str(getattr(user, "pk", "user"))
    username = f"{expires_at}:{user_token}"
    digest = hmac.new(
        auth_secret.encode("utf-8"),
        username.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    credential = base64.b64encode(digest).decode("ascii")
    return {
        "username": username,
        "credential": credential,
    }


def _with_runtime_turn_credentials(ice_servers, user):
    if not ice_servers:
        return []

    temporary_turn_credentials = _build_temporary_turn_credentials(user)
    resolved_servers = []
    for server in ice_servers:
        urls = server.get("urls")
        urls_list = urls if isinstance(urls, list) else [urls]
        has_turn_url = any(isinstance(url, str) and url.startswith(("turn:", "turns:")) for url in urls_list if url)
        resolved = dict(server)
        if has_turn_url and temporary_turn_credentials:
            resolved["username"] = temporary_turn_credentials["username"]
            resolved["credential"] = temporary_turn_credentials["credential"]
            if not resolved.get("credentialType") and settings.WEBRTC_TURN_CREDENTIAL_TYPE:
                resolved["credentialType"] = settings.WEBRTC_TURN_CREDENTIAL_TYPE
        resolved_servers.append(resolved)
    return resolved_servers


def _build_ticket_detail_context(request, ticket, chat_privacy_form=None, remote_access_approval_form=None):
    remote_access_approval = _get_remote_access_approval(ticket)
    incident_report = _get_incident_report(ticket)
    approval_request_kind = _approval_request_kind(ticket)
    cbs_access_support_workflow = (
        approval_request_kind == "CBS Access"
        and remote_access_approval is not None
        and remote_access_approval.status == RemoteAccessApproval.STATUS_APPROVED
    )
    cbs_access_context = (
        _cbs_access_detail_context(ticket, remote_access_approval)
        if approval_request_kind == "CBS Access"
        else {}
    )
    display_description = ticket.description
    if approval_request_kind == "CBS Access" and cbs_access_context.get("cbs_access_data"):
        display_description = _build_cbs_access_request_description(cbs_access_context["cbs_access_data"])
    is_remote_access_request = remote_access_approval is not None and not cbs_access_support_workflow
    if is_remote_access_request:
        _apply_ticket_display_status(ticket, remote_access_approval)
        ticket.is_remote_access_request = True
    else:
        _apply_ticket_display_status(ticket, remote_access_approval)
        ticket.is_remote_access_request = False
    can_update_ticket = (
        not is_remote_access_request
        and (_is_support_user(request.user) or ticket.assigned_to_id == request.user.id)
    )
    show_incident_report = not is_remote_access_request and ticket.request_type == "incident"
    can_manage_incident_report = show_incident_report and _can_manage_incident_report(request.user, ticket)
    can_view_chat = can_access_ticket_chat(request.user, ticket)
    webrtc_ice_servers = getattr(settings, "WEBRTC_ICE_SERVERS", []) or _build_same_host_webrtc_ice_servers(request)
    webrtc_ice_servers = _with_runtime_turn_credentials(webrtc_ice_servers, request.user)
    if is_remote_access_request:
        can_view_requester_info = bool(
            _is_ticket_participant(request.user, ticket)
        )
        chat_unavailable_message = "Remote access approval requests do not use ticket chat or audio calls."
        ticket_back_url = reverse("ticket_list")
    else:
        can_view_requester_info = can_update_ticket or _is_department_ticket_member(request.user, ticket)
        chat_unavailable_message = (
            "This ticket chat is private. Only the requester and the assigned user can view messages, "
            "attachments, and audio call controls."
        )
        ticket_back_url = reverse("support_queue") if _is_support_user(request.user) else reverse("ticket_list")

    if can_view_chat:
        _mark_ticket_chat_seen(ticket, request.user)
        chat_messages = TicketMessage.objects.filter(ticket=ticket).select_related("author", "attachment")
    else:
        chat_messages = TicketMessage.objects.none()

    if chat_privacy_form is None and can_manage_ticket_chat_privacy(request.user, ticket):
        chat_privacy_form = TicketChatPrivacyForm(ticket=ticket, user=request.user)

    if remote_access_approval_form is None and _can_decide_remote_access_approval(request.user, remote_access_approval):
        remote_access_approval_form = RemoteAccessApprovalDecisionForm()

    assignment_logs = list(
        ticket.assignment_logs.all().select_related("assigned_to", "assigned_by", "ticket")
    )
    if not ticket.assigned_to_id and ticket.status in {"resolved", "closed"}:
        ticket._display_assignee = next(
            (log.assigned_to for log in assignment_logs if log.assigned_to_id),
            None,
        )

    context = {
        'ticket': ticket,
        'display_description': display_description,
        'chat_messages': chat_messages,
        'assignment_logs': assignment_logs,
        'can_claim_ticket': False if is_remote_access_request else _can_claim_department_ticket(request.user, ticket),
        'can_update_ticket': can_update_ticket,
        'can_view_requester_info': can_view_requester_info,
        'chat_locked': is_ticket_chat_locked(ticket),
        'chat_locked_message': ticket_chat_locked_message(ticket),
        'can_view_chat': can_view_chat,
        'can_manage_chat_privacy': can_manage_ticket_chat_privacy(request.user, ticket),
        'chat_privacy_form': chat_privacy_form,
        'chat_attachment_batch_limit': TICKET_CHAT_ATTACHMENT_MAX_FILES,
        'webrtc_ice_servers_json': json.dumps(webrtc_ice_servers),
        'remote_access_approval': remote_access_approval,
        'approval_request_kind': approval_request_kind,
        'approval_request_kind_lower': approval_request_kind.lower(),
        'can_decide_remote_access_approval': _can_decide_remote_access_approval(request.user, remote_access_approval),
        'remote_access_approval_form': remote_access_approval_form,
        'incident_report': incident_report,
        'show_incident_report': show_incident_report,
        'can_manage_incident_report': can_manage_incident_report,
        'is_remote_access_request': is_remote_access_request,
        'chat_unavailable_message': chat_unavailable_message,
        'show_assignment_history': not is_remote_access_request,
        'cbs_access_support_workflow': cbs_access_support_workflow,
        'can_requester_assign_cbs_access': bool(
            cbs_access_support_workflow
            and ticket.created_by_id == request.user.id
            and ticket.status not in {"resolved", "closed", "cancelled_duplicate"}
        ),
        'cbs_assignment_users': _cbs_assignment_user_options() if (
            cbs_access_support_workflow
            and ticket.created_by_id == request.user.id
            and ticket.status not in {"resolved", "closed", "cancelled_duplicate"}
        ) else [],
        'cbs_assignment_cc_users': _cbs_assignment_user_options() if (
            cbs_access_support_workflow
            and ticket.created_by_id == request.user.id
            and ticket.status not in {"resolved", "closed", "cancelled_duplicate"}
        ) else [],
        'cbs_assignment_departments': _cbs_assignment_department_options() if (
            cbs_access_support_workflow
            and ticket.created_by_id == request.user.id
            and ticket.status not in {"resolved", "closed", "cancelled_duplicate"}
        ) else [],
        'ticket_back_url': ticket_back_url,
    }
    context.update(cbs_access_context)
    return context


def _mark_ticket_chat_seen(ticket, user, seen_at=None):
    if not getattr(user, "is_authenticated", False):
        return
    TicketChatReadState.objects.update_or_create(
        ticket=ticket,
        user=user,
        defaults={"last_seen_at": seen_at or timezone.now()},
    )


def _attach_ticket_chat_flags(tickets, user):
    tickets = list(tickets)
    for ticket in tickets:
        ticket.has_unread_messages = False

    if not tickets or not getattr(user, "is_authenticated", False):
        return tickets

    ticket_ids = [ticket.id for ticket in tickets]
    latest_other_message_by_ticket = {
        row["ticket_id"]: row["latest_other_message_at"]
        for row in (
            TicketMessage.objects.filter(ticket_id__in=ticket_ids)
            .exclude(author_id=user.id)
            .values("ticket_id")
            .annotate(latest_other_message_at=Max("created_at"))
        )
    }
    last_seen_by_ticket = dict(
        TicketChatReadState.objects.filter(ticket_id__in=ticket_ids, user=user).values_list(
            "ticket_id",
            "last_seen_at",
        )
    )

    for ticket in tickets:
        if not can_access_ticket_chat(user, ticket):
            ticket.has_unread_messages = False
            continue
        latest_other_message_at = latest_other_message_by_ticket.get(ticket.id)
        last_seen_at = last_seen_by_ticket.get(ticket.id)
        ticket.has_unread_messages = bool(
            latest_other_message_at and (last_seen_at is None or latest_other_message_at > last_seen_at)
        )

    return tickets


def _attach_ticket_display_assignees(tickets):
    tickets = list(tickets)
    solved_unassigned_tickets = [
        ticket
        for ticket in tickets
        if not ticket.assigned_to_id and ticket.status in {"resolved", "closed"}
    ]
    if not solved_unassigned_tickets:
        return tickets

    latest_assignee_by_ticket_id = {}
    assignment_logs = (
        TicketAssignmentLog.objects.filter(
            ticket_id__in=[ticket.id for ticket in solved_unassigned_tickets],
            assigned_to__isnull=False,
        )
        .select_related("assigned_to")
        .order_by("ticket_id", "-assigned_at", "-id")
    )
    for log in assignment_logs:
        latest_assignee_by_ticket_id.setdefault(log.ticket_id, log.assigned_to)

    for ticket in solved_unassigned_tickets:
        ticket._display_assignee = latest_assignee_by_ticket_id.get(ticket.id)
    return tickets


def _attach_support_ticket_display_flags(tickets, user):
    tickets = _attach_ticket_chat_flags(tickets, user)
    tickets = _attach_ticket_display_assignees(tickets)
    for ticket in tickets:
        remote_access_approval = _get_remote_access_approval(ticket)
        cbs_approved = _apply_ticket_display_status(ticket, remote_access_approval)
        if remote_access_approval is not None and not cbs_approved:
            ticket.can_support_manage = False
        else:
            ticket.can_support_manage = _is_support_user(user) or ticket.assigned_to_id == getattr(user, "id", None)
    return tickets


def _latest_assignment_user_ids_by_ticket_id(ticket_ids):
    latest_assignee_by_ticket_id = {}
    if not ticket_ids:
        return latest_assignee_by_ticket_id

    assignment_logs = (
        TicketAssignmentLog.objects.filter(
            ticket_id__in=ticket_ids,
            assigned_to__isnull=False,
        )
        .order_by("ticket_id", "-assigned_at", "-id")
        .values("ticket_id", "assigned_to_id")
    )
    for log in assignment_logs:
        latest_assignee_by_ticket_id.setdefault(log["ticket_id"], log["assigned_to_id"])
    return latest_assignee_by_ticket_id


def _resolved_owner_ids_by_ticket_id(tickets):
    resolved_tickets = list(tickets)
    owner_by_ticket_id = {}
    fallback_ticket_ids = []

    for ticket in resolved_tickets:
        if ticket.resolved_by_id:
            owner_by_ticket_id[ticket.id] = ticket.resolved_by_id
        else:
            fallback_ticket_ids.append(ticket.id)

    if fallback_ticket_ids:
        owner_by_ticket_id.update(_latest_assignment_user_ids_by_ticket_id(fallback_ticket_ids))

    return owner_by_ticket_id


def _build_ticket_attachment_event(ticket, message, attachment, author):
    return {
        "id": message.id,
        "body": message.body,
        "author": author,
        "author_id": message.author_id,
        "created_at": _format_ws_datetime(message.created_at),
        "attachment": {
            "id": attachment.id,
            "filename": attachment.filename,
            "size": attachment.size,
            "content_type": attachment.content_type,
            "view_url": reverse("ticket_attachment_view", args=[ticket.id, attachment.id]),
            "download_url": reverse("ticket_attachment_download", args=[ticket.id, attachment.id]),
        },
    }


def _can_delete_ticket_message(user, message):
    if not getattr(user, "is_authenticated", False):
        return False
    if message.author_id == user.id:
        return True
    attachment = message.attachment if hasattr(message, "attachment") else None
    return bool(attachment and attachment.uploaded_by_id == user.id)


def _new_submission_token():
    return secrets.token_hex(16)


def _clean_submission_token(value):
    return _clean_query_value(value)[:64]


def _ticket_for_submission_token(submission_token):
    if not submission_token:
        return None
    return Ticket.objects.filter(submission_token=submission_token).first()


def _parse_incident_report_datetime_value(value):
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        raw_value = str(value).strip()
        parsed = parse_datetime(raw_value)
        if parsed is None:
            for date_format in ("%b %d, %Y %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
                try:
                    parsed = datetime.strptime(raw_value, date_format)
                    break
                except ValueError:
                    continue
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _incident_report_template_defaults_for_ticket(ticket, user=None, incident_data=None):
    incident_data = incident_data or {}
    reporter = incident_report_person_display(getattr(ticket, "created_by", None))
    commander = incident_report_person_display(getattr(ticket, "assigned_to", None))
    detected_at = ""
    detected_at_value = _parse_incident_report_datetime_value(incident_data.get("incident_detected_at"))
    if getattr(ticket, "created_at", None):
        detected_at_value = detected_at_value or ticket.created_at
        detected_at = timezone.localtime(ticket.created_at).strftime("%b %d, %Y %H:%M")

    actor = user if getattr(user, "is_authenticated", False) else getattr(ticket, "created_by", None)
    requester = getattr(ticket, "created_by", None)
    responsible_departments = []
    primary_department = (getattr(ticket, "department", "") or "").strip()
    if primary_department:
        responsible_departments.append(primary_department)
    responsible_departments.extend(
        item
        for item in parse_department_list(getattr(ticket, "additional_departments", ""))
        if item.casefold() not in {department.casefold() for department in responsible_departments}
    )
    impact_branch_department = " / ".join(
        value
        for value in [
            (getattr(ticket, "branch", "") or "").strip(),
            ", ".join(responsible_departments),
        ]
        if value
    )
    return {
        "incident_title": (getattr(ticket, "subject", "") or "").strip(),
        "incident_id": (getattr(ticket, "ticket_id", "") or "").strip(),
        "detected_at": (incident_data.get("incident_detected_at") or "").strip() or detected_at,
        "date_time_of_detection": detected_at_value,
        "date_of_report": getattr(ticket, "created_at", None),
        "reporting_employee_name": reporter,
        "reporting_employee_designation": (getattr(requester, "position", "") or "").strip(),
        "reporting_employee_email": (getattr(requester, "email", "") or "").strip(),
        "reporting_employee_contact": (getattr(requester, "phone_number", "") or "").strip(),
        "reported_by": reporter,
        "incident_commander": commander,
        "incident_commander_user": getattr(ticket, "assigned_to", None),
        "current_status": (incident_data.get("incident_current_status") or "").strip() or (ticket.get_status_display() if getattr(ticket, "status", "") else ""),
        "service_affected": (incident_data.get("incident_service_affected") or "").strip(),
        "branch_impacted": (getattr(ticket, "branch", "") or "").strip(),
        "impact_branch_department": impact_branch_department,
        "unit_or_department_impacted": impact_branch_department,
        "summary_what_happened": (getattr(ticket, "description", "") or "").strip(),
        "summary_detected": (incident_data.get("incident_detected_how") or "").strip(),
        "summary_affected": (incident_data.get("incident_affected") or "").strip(),
        "impact_operational": (incident_data.get("incident_business_impact") or "").strip(),
        "containment_actions": (incident_data.get("incident_initial_action") or "").strip(),
        "evidence_logs": (incident_data.get("incident_evidence") or "").strip(),
        "evidence_ticket_case": (getattr(ticket, "ticket_id", "") or "").strip(),
        "created_by": actor,
        "updated_by": actor,
    }


def _ensure_incident_report_template_for_ticket(ticket, user=None, incident_data=None):
    if getattr(ticket, "request_type", "") != "incident":
        return None
    incident_report, _created = IncidentReport.objects.get_or_create(
        ticket=ticket,
        defaults=_incident_report_template_defaults_for_ticket(ticket, user=user, incident_data=incident_data),
    )
    return incident_report


@login_required
def create_ticket(request):
    submission_token = _new_submission_token()
    if request.method == 'POST':
        submission_token = _clean_submission_token(request.POST.get("submission_token")) or _new_submission_token()
        existing_ticket = _ticket_for_submission_token(submission_token)
        if existing_ticket is not None:
            messages.info(request, "This ticket was already submitted. Opening the existing ticket instead.")
            return redirect("ticket_detail", ticket_id=existing_ticket.id)

        form = TicketForm(request.POST, request.FILES, user=request.user)
        if form.is_valid():
            attachments = form.cleaned_data.get("attachments") or []
            email_attachments = _build_email_attachments(attachments)
            minio_cfg = None
            s3 = None
            if attachments:
                try:
                    minio_cfg = get_minio_config()
                    s3 = get_s3_client()
                except Exception:
                    form.add_error("attachments", "Attachment storage is not configured.")
                    return render(
                        request,
                        "tickets/create_ticket.html",
                        {"form": form, "submission_token": submission_token},
                    )

            ticket = form.save(commit=False)
            ticket.created_by = request.user
            ticket.submission_token = submission_token or None
            assign_user_id = getattr(form, "_assign_user_id", None)
            if assign_user_id:
                ticket.assigned_to_id = assign_user_id
                ticket._assignment_actor_id = request.user.id
            try:
                with transaction.atomic():
                    ticket.save()
                    _ensure_incident_report_template_for_ticket(
                        ticket,
                        user=request.user,
                        incident_data=form.cleaned_data,
                    )
            except IntegrityError:
                existing_ticket = _ticket_for_submission_token(submission_token)
                if existing_ticket is not None:
                    messages.info(request, "This ticket was already submitted. Opening the existing ticket instead.")
                    return redirect("ticket_detail", ticket_id=existing_ticket.id)
                raise

            if attachments and minio_cfg and s3:
                for upload in attachments:
                    object_key = TicketMessageAttachment.build_object_key(ticket.id, upload.name)
                    content_type = getattr(upload, "content_type", "") or "application/octet-stream"
                    try:
                        if hasattr(upload, "seek"):
                            upload.seek(0)
                        s3.upload_fileobj(
                            upload,
                            minio_cfg.bucket,
                            object_key,
                            ExtraArgs={"ContentType": content_type},
                        )
                        message = TicketMessage.objects.create(
                            ticket=ticket,
                            author=request.user,
                            body=f"Attachment uploaded: {upload.name}",
                        )
                        TicketMessageAttachment.objects.create(
                            ticket=ticket,
                            message=message,
                            uploaded_by=request.user,
                            object_key=object_key,
                            filename=upload.name,
                            content_type=content_type,
                            size=upload.size or 0,
                        )
                    except Exception:
                        messages.warning(request, f"Ticket created, but attachment upload failed: {upload.name}")
            if ticket.assigned_to_id and ticket.assigned_to_id != request.user.id:
                _notify_user(
                    ticket.assigned_to_id,
                    {
                        "kind": "ticket_assigned",
                        "level": "info",
                        "title": "New ticket assigned",
                        "message": f"{ticket.ticket_id}: {ticket.subject}",
                        "url": reverse("ticket_detail", args=[ticket.id]),
                        "ticket_id": ticket.id,
                        "ticket_code": ticket.ticket_id,
                    },
                )
                if (ticket.notify_email or "").strip().lower() != (getattr(ticket.assigned_to, "email", "") or "").strip().lower():
                    _send_assignment_email(
                        request,
                        ticket,
                        request.user,
                        "Ticket created",
                        email_attachments=email_attachments,
                    )
            mail_subject = f"New Helpdesk Ticket: {ticket.ticket_id}"
            mail_body = _build_new_ticket_email_body(request, ticket)
            try:
                _send_email_message(
                    mail_subject,
                    mail_body,
                    [ticket.notify_email or settings.IT_SUPPORT_EMAIL],
                    cc_list=ticket.cc_email_list,
                    email_attachments=email_attachments,
                )
            except Exception:
                messages.warning(request, "Ticket created, but notification email could not be sent.")
            messages.success(request, 'Ticket created successfully!')
            if ticket.request_type == "incident":
                return redirect("ticket_incident_report", ticket_id=ticket.id)
            return redirect('ticket_list')
    else:
        request_type = (request.GET.get("request_type") or "service").strip()
        allowed_request_types = {value for value, _label in Ticket.REQUEST_TYPE_CHOICES}
        if request_type not in allowed_request_types:
            request_type = "service"
        form = TicketForm(initial={"request_type": request_type}, user=request.user)

    return render(
        request,
        'tickets/create_ticket.html',
        {
            'form': form,
            'submission_token': submission_token,
        },
    )


def _format_cbs_access_date(value):
    if not value:
        return "-"
    if isinstance(value, str):
        return value or "-"
    if isinstance(value, datetime):
        value = timezone.localtime(value)
    return value.strftime("%m/%d/%Y")


def _cbs_display_user(user):
    if not user:
        return ""
    full_name = (user.get_full_name() or "").strip()
    return full_name or (getattr(user, "username", "") or "").strip()


def _cbs_user_position(user):
    return (getattr(user, "position", "") or "").strip() if user else ""


def _cbs_description_user_value(value, fallback="-"):
    if value in (None, ""):
        return fallback
    if hasattr(value, "get_full_name"):
        return _cbs_display_user(value) or fallback
    return str(value)


def _build_cbs_access_request_description(cleaned_data):
    request_type = cleaned_data.get("request_type") or "cbs_access_ho"
    group_labels = dict(_cbs_access_group_choices(request_type))
    selected_groups = cleaned_data.get("user_groups") or []
    selected_group_lines = [
        f"{code} - {group_labels.get(code, code)}: Yes"
        for code in selected_groups
    ]
    if not selected_group_lines:
        selected_group_lines = ["-"]

    user_type_label = "New User" if cleaned_data.get("user_type") == "new" else "Amendment for Old User"
    access_user = cleaned_data.get("access_user")
    access_user_id = getattr(access_user, "id", "") or cleaned_data.get("access_user_id") or "-"
    access_user_name = (
        _cbs_display_user(access_user)
        if hasattr(access_user, "get_full_name")
        else cleaned_data.get("access_user_signature_name")
    ) or "-"
    access_user_signed_at = (
        cleaned_data.get("access_user_signed_at")
        or timezone.localtime(timezone.now()).strftime("%m/%d/%Y")
    )
    requested_signature_user = cleaned_data.get("requested_signature_user")
    requested_signature_user_id = (
        getattr(requested_signature_user, "id", "")
        or cleaned_data.get("requested_signature_user_id")
        or "-"
    )
    requested_signature_name = (
        _cbs_display_user(requested_signature_user)
        if hasattr(requested_signature_user, "get_full_name")
        else cleaned_data.get("requested_signature_name")
    ) or "-"
    requested_signature_signed_at = (
        cleaned_data.get("requested_signature_signed_at")
        or timezone.localtime(timezone.now()).strftime("%m/%d/%Y")
    )
    return "\n".join(
        [
            "BEST FINANCE COMPANY LIMITED",
            f"USER ID REQUEST FORM FOR CBS (For {_cbs_access_office_label(request_type)} Only)",
            f"Request Type: {request_type}",
            "",
            "USER INFORMATION:",
            f"Name: {cleaned_data.get('name') or '-'}",
            f"Designation: {cleaned_data.get('designation') or '-'}",
            f"Department: {cleaned_data.get('department') or '-'}",
            f"Employee ID: {cleaned_data.get('employee_id') or '-'}",
            f"Access User Signature User ID: {access_user_id}",
            f"Access User Signature Name: {access_user_name}",
            f"Access User Signed At: {access_user_signed_at}",
            f"Requested Signature User ID: {requested_signature_user_id}",
            f"Requested Signature Name: {requested_signature_name}",
            f"Requested Signature Signed At: {requested_signature_signed_at}",
            f"Type of User: {user_type_label}",
            f"Old User ID: {cleaned_data.get('old_user_id') or '-'}",
            "",
            "USER REQUIREMENTS AND APPROVALS:",
            *selected_group_lines,
            "",
            f"Reason for Amendment for Old User: {cleaned_data.get('amendment_reason') or '-'}",
            "",
            "DIGITAL SIGN-OFF CHAIN:",
            f"Recommended By User: {_cbs_description_user_value(cleaned_data.get('recommender'), 'Not Required')}",
            f"Second Recommended By User: {_cbs_description_user_value(cleaned_data.get('second_recommender'), 'Not Required')}",
            f"Approved By User: {_cbs_description_user_value(cleaned_data.get('approver'), '-')}",
            "",
            "User Requested By",
            f"Name: {cleaned_data.get('requested_by_name') or '-'}",
            f"Designation: {cleaned_data.get('requested_by_designation') or '-'}",
            f"Date: {_format_cbs_access_date(cleaned_data.get('requested_by_date'))}",
            "",
            "Recommended By",
            f"Name: {cleaned_data.get('recommended_by_name') or '-'}",
            f"Designation: {cleaned_data.get('recommended_by_designation') or '-'}",
            f"Date: {_format_cbs_access_date(cleaned_data.get('recommended_by_date'))}",
            "",
            "Second Recommended By",
            f"Name: {cleaned_data.get('branch_second_recommended_by_name') or '-'}",
            f"Designation: {cleaned_data.get('branch_second_recommended_by_designation') or '-'}",
            f"Date: {_format_cbs_access_date(cleaned_data.get('branch_second_recommended_by_date'))}",
            "",
            "Approved By",
            f"Name: {cleaned_data.get('approved_by_name') or '-'}",
            f"Designation: {cleaned_data.get('approved_by_designation') or '-'}",
            f"Date: {_format_cbs_access_date(cleaned_data.get('approved_by_date'))}",
            "",
            "ENDORSEMENT BY USER:",
            "By signing below I acknowledge that I am the authorized user of the Pumori System and possess the appropriate power to login with aforementioned user id. I will keep my user id and password as equal to my signature. The information provided herein is correct and true in my knowledge.",
            "User Signature with Date:",
        ]
    )


def _cbs_access_template_response(
    cleaned_data=None,
    filename="cbs-access-request-template.doc",
    remote_access_approval=None,
    output_format="docx",
):
    output_format = (output_format or "docx").strip().lower()
    request_type = (cleaned_data or {}).get("request_type") or "cbs_access_ho"
    if output_format == "pdf":
        pdf_response = _cbs_access_docx_to_pdf_response(
            cleaned_data or {},
            filename=filename.rsplit(".", 1)[0] + ".pdf",
            remote_access_approval=remote_access_approval,
        )
        if pdf_response is not None:
            return pdf_response
        return _cbs_access_pdf_response(
            cleaned_data or {},
            filename=filename.rsplit(".", 1)[0] + ".pdf",
            remote_access_approval=remote_access_approval,
        )
    if output_format in {"png", "jpg", "jpeg"}:
        image_response = _cbs_access_docx_to_image_response(
            cleaned_data or {},
            filename=filename,
            remote_access_approval=remote_access_approval,
            image_format=output_format,
        )
        if image_response is not None:
            return image_response
        return HttpResponse(
            "CBS access image export is unavailable. Please contact support.",
            status=503,
            content_type="text/plain",
        )

    if os.path.exists(_cbs_access_template_path(request_type)):
        docx_payload = _build_cbs_access_docx(cleaned_data or {}, remote_access_approval=remote_access_approval)
        resolved_filename = filename.rsplit(".", 1)[0] + ".docx"
        response = HttpResponse(
            docx_payload,
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        response["Content-Disposition"] = f'attachment; filename="{resolved_filename}"'
        return response

    data = cleaned_data or {}
    selected_groups = set(data.get("user_groups") or [])
    group_labels = dict(_cbs_access_group_choices(request_type))

    def value(name):
        item = data.get(name)
        if item is None:
            return ""
        if hasattr(item, "strftime"):
            return item.strftime("%m/%d/%Y")
        return str(item)

    def checked_when(name, expected):
        return "X" if value(name) == expected else ""

    def display_user(user):
        if not user:
            return ""
        full_name = (user.get_full_name() or "").strip()
        return full_name or (getattr(user, "username", "") or "").strip()

    def signature_data_uri(user):
        upload = getattr(user, "signature_image", None)
        return signature_field_data_uri(upload)

    def signature_field_data_uri(upload):
        if not upload:
            return ""
        try:
            if hasattr(upload, "open"):
                upload.open("rb")
            if hasattr(upload, "seek"):
                upload.seek(0)
            payload = upload.read()
            if hasattr(upload, "seek"):
                upload.seek(0)
        except Exception:
            return ""
        filename_hint = (getattr(upload, "name", "") or "").lower()
        mime_type = "image/png"
        if filename_hint.endswith((".jpg", ".jpeg")):
            mime_type = "image/jpeg"
        elif filename_hint.endswith(".gif"):
            mime_type = "image/gif"
        return f"data:{mime_type};base64,{base64.b64encode(payload).decode('ascii')}"

    if remote_access_approval is not None:
        if getattr(remote_access_approval, "recommended_by_id", None):
            data["recommended_by_name"] = display_user(remote_access_approval.recommended_by) or value("recommended_by_name")
            data["recommended_by_designation"] = (
                getattr(remote_access_approval.recommended_by, "position", "") or value("recommended_by_designation")
            )
            data["recommended_by_date"] = timezone.localtime(remote_access_approval.recommended_at).strftime("%m/%d/%Y") if remote_access_approval.recommended_at else value("recommended_by_date")
        if getattr(remote_access_approval, "decided_by_id", None):
            data["approved_by_name"] = display_user(remote_access_approval.decided_by) or value("approved_by_name")
            data["approved_by_designation"] = (
                getattr(remote_access_approval.decided_by, "position", "") or value("approved_by_designation")
            )
            data["approved_by_date"] = timezone.localtime(remote_access_approval.decided_at).strftime("%m/%d/%Y") if remote_access_approval.decided_at else value("approved_by_date")

    recommender_signature_uri = (
        signature_field_data_uri(_cbs_access_snapshot_field(remote_access_approval, "recommended_signature_snapshot"))
        or signature_data_uri(remote_access_approval.recommended_by)
        if remote_access_approval is not None and getattr(remote_access_approval, "recommended_by_id", None)
        else ""
    )
    approver_signature_uri = (
        signature_field_data_uri(_cbs_access_snapshot_field(remote_access_approval, "approved_signature_snapshot"))
        or signature_data_uri(remote_access_approval.decided_by)
        if remote_access_approval is not None and getattr(remote_access_approval, "decided_by_id", None)
        else ""
    )
    access_user = _cbs_access_acknowledgement_user(data)
    access_user_signature_uri = (
        signature_field_data_uri(_cbs_access_snapshot_field(remote_access_approval, "access_user_signature_snapshot"))
        or signature_data_uri(access_user)
    )
    access_user_signed_at = value("access_user_signed_at") or timezone.localtime(timezone.now()).strftime("%m/%d/%Y")
    requested_signature_user = _cbs_requested_signature_user(data)
    requested_signature_uri = (
        signature_field_data_uri(_cbs_access_snapshot_field(remote_access_approval, "requested_signature_snapshot"))
        or signature_data_uri(requested_signature_user)
    )

    group_rows = []
    midpoint = (len(CBS_USER_GROUP_CHOICES) + 1) // 2
    left_groups = CBS_USER_GROUP_CHOICES[:midpoint]
    right_groups = CBS_USER_GROUP_CHOICES[midpoint:]
    for index in range(midpoint):
        left_code, left_label = left_groups[index]
        right_code, right_label = right_groups[index] if index < len(right_groups) else ("", "")
        group_rows.append(
            "<tr>"
            f"<td class='code'>{escape(left_code)}</td><td>{escape(group_labels.get(left_code, left_label))}</td><td class='please'>{'X' if left_code in selected_groups else ''}</td>"
            f"<td class='code'>{escape(right_code)}</td><td>{escape(group_labels.get(right_code, right_label))}</td><td class='please'>{'X' if right_code in selected_groups else ''}</td>"
            "</tr>"
        )

    recommender_signature_html = (
        f"<img class='signature-img' src='{recommender_signature_uri}' alt='Recommended signature'>"
        if recommender_signature_uri
        else "<div class='signature-line'>Signature pending</div>"
    )
    approver_signature_html = (
        f"<img class='signature-img' src='{approver_signature_uri}' alt='Approved signature'>"
        if approver_signature_uri
        else "<div class='signature-line'>Signature pending</div>"
    )
    access_user_signature_html = (
        f"<img class='signature-img' src='{access_user_signature_uri}' alt='User acknowledgement signature'>"
        if access_user_signature_uri
        else "<span>______________________________</span>"
    )
    requested_signature_html = (
        f"<img class='signature-img' src='{requested_signature_uri}' alt='Requested by signature'>"
        if requested_signature_uri
        else "<div class='signature-line'>Signature pending</div>"
    )

    body = f"""<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        @page {{ size: A4; margin: 0.45in; }}
        body {{ font-family: Arial, sans-serif; font-size: 15pt; color: #000; }}
        h1, h2 {{ text-align: center; margin: 0; }}
        h1 {{ font-size: 20pt; font-weight: 800; }}
        h2 {{ font-size: 17pt; font-weight: 800; margin-bottom: 16px; }}
        table {{ border-collapse: collapse; width: 100%; margin-bottom: 16px; table-layout: fixed; }}
        th, td {{ border: 2px solid #000; padding: 8px; vertical-align: top; font-size: 14pt; line-height: 1.25; }}
        th {{ background: #f2f2f2; font-weight: 800; text-align: center; }}
        .section {{ font-weight: 800; font-size: 15pt; margin: 16px 0 8px; }}
        .label {{ width: 26%; font-weight: 800; }}
        .code {{ width: 9%; text-align: center; font-weight: 800; }}
        .please {{ width: 11%; text-align: center; font-weight: 800; font-size: 16pt; }}
        .center {{ text-align: center; }}
        .signature-box {{ height: 95px; vertical-align: middle; }}
        .signature-img {{ max-width: 220px; max-height: 80px; display: block; margin: 0 auto; }}
        .signature-line {{ margin-top: 38px; border-top: 2px solid #000; color: #000; font-size: 12pt; padding-top: 4px; text-align: center; }}
        .endorsement {{ font-size: 14pt; line-height: 1.35; }}
    </style>
</head>
<body>
    <h1>BEST FINANCE COMPANY LIMITED</h1>
    <h2>USER ID REQUEST FORM FOR CBS (For Head Office Only)</h2>

    <div class="section">USER INFORMATION:</div>
    <table>
        <tr><td class="label">Name</td><td>{escape(value('name'))}</td></tr>
        <tr><td class="label">Designation</td><td>{escape(value('designation'))}</td></tr>
        <tr><td class="label">Department</td><td>{escape(value('department'))}</td></tr>
        <tr><td class="label">Employee ID</td><td>{escape(value('employee_id'))}</td></tr>
        <tr><td class="label">Type of User</td><td>New User [{checked_when('user_type', 'new')}] &nbsp;&nbsp; Amendment for Old User [{checked_when('user_type', 'amendment')}]</td></tr>
        <tr><td class="label">Old User ID</td><td>{escape(value('old_user_id'))}</td></tr>
    </table>

    <div class="section">USER REQUIREMENTS AND APPROVALS:</div>
    <table>
        <tr>
            <th>User Group</th><th>Description</th><th>Please (X)</th>
            <th>User Group</th><th>Description</th><th>Please (X)</th>
        </tr>
        {''.join(group_rows)}
    </table>

    <table>
        <tr><td>Reason for Amendment for Old User:</td><td>{escape(value('amendment_reason'))}</td></tr>
    </table>

    <table>
        <tr><th>User Requested By</th><th>Recommended By</th><th>Approved By</th></tr>
        <tr>
            <td>Name: {escape(value('requested_by_name'))}</td>
            <td>Name: {escape(value('recommended_by_name'))}</td>
            <td>Name: {escape(value('approved_by_name'))}</td>
        </tr>
        <tr>
            <td>Designation: {escape(value('requested_by_designation'))}</td>
            <td>Designation: {escape(value('recommended_by_designation'))}</td>
            <td>Designation: {escape(value('approved_by_designation'))}</td>
        </tr>
        <tr>
            <td>Date: {escape(value('requested_by_date'))}</td>
            <td>Date: {escape(value('recommended_by_date'))}</td>
            <td>Date: {escape(value('approved_by_date'))}</td>
        </tr>
        <tr>
            <td class="signature-box">Digital Signature:{requested_signature_html}</td>
            <td class="signature-box">Digital Signature:{recommender_signature_html}</td>
            <td class="signature-box">Digital Signature:{approver_signature_html}</td>
        </tr>
    </table>

    <div class="section">3. ENDORSEMENT BY USER:</div>
    <p class="endorsement">By signing below I acknowledge that I am the authorized user of the Pumori System and possess the appropriate power to login with aforementioned user id. I will keep my user id and password as equal to my signature. The information provided herein is correct and true in my knowledge.</p>
    <p>User Acknowledgement Signature:<br>{access_user_signature_html}<br>Date: {escape(access_user_signed_at)}</p>
</body>
</html>"""
    response = HttpResponse(body, content_type="application/msword")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _build_cbs_access_email_attachments(ticket, remote_access_approval):
    pdf_payload = _cbs_access_docx_to_pdf_payload(
        _cbs_access_data_from_ticket(ticket),
        remote_access_approval=remote_access_approval,
    )
    if pdf_payload is None:
        pdf_response = _cbs_access_pdf_response(
            _cbs_access_data_from_ticket(ticket),
            filename=f"cbs-access-request-{ticket.ticket_id}.pdf",
            remote_access_approval=remote_access_approval,
        )
        pdf_payload = pdf_response.content
    return [
        (
            f"cbs-access-request-{ticket.ticket_id}.pdf",
            pdf_payload,
            "application/pdf",
        )
    ]


def _build_cbs_access_email_attachment(ticket, remote_access_approval):
    return _build_cbs_access_email_attachments(ticket, remote_access_approval)[0]


def _cbs_access_docx_to_pdf_payload(cleaned_data=None, remote_access_approval=None):
    converter = shutil.which("libreoffice") or shutil.which("soffice")
    request_type = (cleaned_data or {}).get("request_type") or "cbs_access_ho"
    if not converter or not os.path.exists(_cbs_access_template_path(request_type)):
        return None

    docx_payload = _build_cbs_access_docx(cleaned_data or {}, remote_access_approval=remote_access_approval)
    with tempfile.TemporaryDirectory() as tmpdir:
        docx_path = os.path.join(tmpdir, "cbs-access-request.docx")
        with open(docx_path, "wb") as docx_file:
            docx_file.write(docx_payload)

        try:
            subprocess.run(
                [
                    converter,
                    "--headless",
                    "--nologo",
                    "--nofirststartwizard",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    tmpdir,
                    docx_path,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except Exception:
            return None

        pdf_path = os.path.join(tmpdir, "cbs-access-request.pdf")
        if not os.path.exists(pdf_path):
            return None
        with open(pdf_path, "rb") as pdf_file:
            return pdf_file.read()


def _cbs_access_docx_to_pdf_response(cleaned_data=None, filename="cbs-access-request.pdf", remote_access_approval=None):
    payload = _cbs_access_docx_to_pdf_payload(cleaned_data or {}, remote_access_approval=remote_access_approval)
    if payload is None:
        return None

    response = HttpResponse(payload, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _cbs_access_docx_to_image_response(
    cleaned_data=None,
    filename="cbs-access-request.png",
    remote_access_approval=None,
    image_format="png",
):
    normalized_format = "jpeg" if image_format in {"jpg", "jpeg"} else "png"
    extension = "jpg" if normalized_format == "jpeg" else "png"
    content_type = "image/jpeg" if normalized_format == "jpeg" else "image/png"
    base_filename = get_valid_filename(os.path.basename(filename).rsplit(".", 1)[0] or "cbs-access-request")
    payloads = _cbs_access_docx_to_image_payloads(
        cleaned_data or {},
        remote_access_approval=remote_access_approval,
        image_format=normalized_format,
    )
    if not payloads:
        return None

    image_payloads = [
        (f"{base_filename}-page-{page_index}.{extension}", image_payload)
        for page_index, image_payload in enumerate(payloads, start=1)
    ]

    if len(image_payloads) == 1:
        image_filename, image_payload = image_payloads[0]
        response = HttpResponse(image_payload, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{image_filename}"'
        return response

    archive = BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for image_filename, image_payload in image_payloads:
            zip_file.writestr(image_filename, image_payload)
    archive.seek(0)
    response = HttpResponse(archive.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{base_filename}-{extension}-pages.zip"'
    return response


def _cbs_access_docx_to_image_payloads(cleaned_data=None, remote_access_approval=None, image_format="png"):
    converter = shutil.which("libreoffice") or shutil.which("soffice")
    request_type = (cleaned_data or {}).get("request_type") or "cbs_access_ho"
    if not converter or not os.path.exists(_cbs_access_template_path(request_type)):
        return []

    normalized_format = "jpeg" if image_format in {"jpg", "jpeg"} else "png"
    docx_payload = _build_cbs_access_docx(cleaned_data or {}, remote_access_approval=remote_access_approval)

    with tempfile.TemporaryDirectory() as tmpdir:
        docx_path = os.path.join(tmpdir, "cbs-access-request.docx")
        with open(docx_path, "wb") as docx_file:
            docx_file.write(docx_payload)

        try:
            subprocess.run(
                [
                    converter,
                    "--headless",
                    "--nologo",
                    "--nofirststartwizard",
                    "--convert-to",
                    "png",
                    "--outdir",
                    tmpdir,
                    docx_path,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except Exception:
            return []

        png_paths = sorted(
            os.path.join(tmpdir, item)
            for item in os.listdir(tmpdir)
            if item.lower().endswith(".png")
        )
        image_payloads = []
        for png_path in png_paths:
            with open(png_path, "rb") as image_file:
                image_payload = image_file.read()
            if normalized_format == "jpeg":
                try:
                    with Image.open(BytesIO(image_payload)) as image:
                        converted = BytesIO()
                        image.convert("RGB").save(converted, format="JPEG", quality=92)
                        image_payload = converted.getvalue()
                except Exception:
                    return []
            image_payloads.append(image_payload)
        return image_payloads or _cbs_access_pillow_image_payloads(
            cleaned_data or {},
            remote_access_approval=remote_access_approval,
            image_format=normalized_format,
        )


def _cbs_access_pillow_image_payloads(cleaned_data=None, remote_access_approval=None, image_format="png"):
    data = dict(cleaned_data or {})
    request_type = data.get("request_type") or "cbs_access_ho"
    office_type = _cbs_access_office_type_from_request_type(request_type)
    is_branch_request = office_type == "branch"

    def display_user(user):
        if not user:
            return ""
        return (user.get_full_name() or "").strip() or (getattr(user, "username", "") or "").strip()

    if remote_access_approval is not None:
        if getattr(remote_access_approval, "recommended_by_id", None):
            data["recommended_by_name"] = display_user(remote_access_approval.recommended_by) or data.get("recommended_by_name", "")
            data["recommended_by_designation"] = _cbs_user_position(remote_access_approval.recommended_by) or data.get("recommended_by_designation", "")
            data["recommended_by_date"] = timezone.localtime(remote_access_approval.recommended_at).strftime("%m/%d/%Y") if remote_access_approval.recommended_at else data.get("recommended_by_date", "")
        if getattr(remote_access_approval, "second_recommended_by_id", None):
            data["branch_second_recommended_by_name"] = display_user(remote_access_approval.second_recommended_by) or data.get("branch_second_recommended_by_name", "")
            data["branch_second_recommended_by_designation"] = _cbs_user_position(remote_access_approval.second_recommended_by) or data.get("branch_second_recommended_by_designation", "")
            data["branch_second_recommended_by_date"] = timezone.localtime(remote_access_approval.second_recommended_at).strftime("%m/%d/%Y") if remote_access_approval.second_recommended_at else data.get("branch_second_recommended_by_date", "")
        if getattr(remote_access_approval, "decided_by_id", None):
            data["approved_by_name"] = display_user(remote_access_approval.decided_by) or data.get("approved_by_name", "")
            data["approved_by_designation"] = _cbs_user_position(remote_access_approval.decided_by) or data.get("approved_by_designation", "")
            data["approved_by_date"] = timezone.localtime(remote_access_approval.decided_at).strftime("%m/%d/%Y") if remote_access_approval.decided_at else data.get("approved_by_date", "")

    width, height = 1650, 2200
    margin = 60
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    ink = (0, 0, 0)
    light = (242, 245, 248)

    def font(size, bold=False):
        candidates = ["DejaVuSans-Bold.ttf"] if bold else ["DejaVuSans.ttf"]
        for candidate in candidates:
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
        return ImageFont.load_default()

    title_font = font(34, True)
    section_font = font(26, True)
    body_font = font(22)
    bold_font = font(22, True)
    small_font = font(19)

    def value(name):
        item = data.get(name)
        if item is None:
            return ""
        if hasattr(item, "strftime"):
            return item.strftime("%m/%d/%Y")
        return str(item)

    def text_w(text, used_font):
        box = draw.textbbox((0, 0), str(text), font=used_font)
        return box[2] - box[0]

    def wrap_lines(text, used_font, max_width):
        words = str(text or "").split()
        lines = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if text_w(candidate, used_font) <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines or [""]

    def draw_text(text, x, y, max_width, used_font, max_lines=2):
        line_height = used_font.size + 7
        for index, line in enumerate(wrap_lines(text, used_font, max_width)[:max_lines]):
            draw.text((x, y + index * line_height), line, fill=ink, font=used_font)

    def cell(x, y, w, h, text="", used_font=None, fill=None, align="left", max_lines=2):
        draw.rectangle((x, y, x + w, y + h), outline=ink, fill=fill or "white", width=2)
        if text:
            used_font = used_font or body_font
            tx = x + 8
            if align == "center":
                tx = x + max(8, int((w - text_w(text, used_font)) / 2))
            draw_text(text, tx, y + 8, w - 16, used_font, max_lines=max_lines)

    def row(y, widths, h, texts, fonts=None, fills=None, aligns=None, max_lines=2):
        x = margin
        for index, w in enumerate(widths):
            cell(
                x,
                y,
                w,
                h,
                texts[index] if index < len(texts) else "",
                used_font=fonts[index] if fonts else body_font,
                fill=fills[index] if fills else None,
                align=aligns[index] if aligns else "left",
                max_lines=max_lines,
            )
            x += w
        return y + h

    y = 46
    title = f"USER ID REQUEST FORM FOR CBS (For {_cbs_access_office_label(request_type)} Only)"
    draw.text(((width - text_w("BEST FINANCE COMPANY LIMITED", title_font)) // 2, y), "BEST FINANCE COMPANY LIMITED", fill=ink, font=title_font)
    y += 52
    draw.text(((width - text_w(title, section_font)) // 2, y), title, fill=ink, font=section_font)
    y += 62

    full_w = width - margin * 2
    label_w = 250
    y = row(y, [label_w, full_w - label_w], 54, ["Name", value("name")], fonts=[bold_font, body_font])
    y = row(y, [label_w, full_w - label_w], 54, ["Designation", value("designation")], fonts=[bold_font, body_font])
    y = row(y, [label_w, full_w - label_w], 54, ["Branch / Department" if is_branch_request else "Department", value("department")], fonts=[bold_font, body_font])
    y = row(y, [label_w, full_w - label_w], 54, ["Employee ID", value("employee_id")], fonts=[bold_font, body_font])
    user_type = f"New User [{'X' if value('user_type') == 'new' else ' '}]     Amendment for Old User [{'X' if value('user_type') == 'amendment' else ' '}]     Old User ID: {value('old_user_id') or '-'}"
    y = row(y, [label_w, full_w - label_w], 58, ["Type of User", user_type], fonts=[bold_font, body_font])
    y += 26

    cell(margin, y, full_w, 46, "USER REQUIREMENTS AND APPROVALS:", section_font, fill=light)
    y += 46
    choices = _cbs_access_group_choices(request_type)
    selected_groups = set(data.get("user_groups") or [])
    mid = (len(choices) + 1) // 2
    widths = [90, 560, 100, 90, 560, 100]
    y = row(y, widths, 48, ["Group", "Description", "X", "Group", "Description", "X"], fonts=[bold_font] * 6, fills=[light] * 6, aligns=["center"] * 6)
    for index in range(mid):
        left_code, left_label = choices[index]
        right_code, right_label = choices[index + mid] if index + mid < len(choices) else ("", "")
        y = row(
            y,
            widths,
            56,
            [left_code, left_label, "X" if left_code in selected_groups else "", right_code, right_label, "X" if right_code in selected_groups else ""],
            fonts=[bold_font, small_font, body_font, bold_font, small_font, body_font],
            aligns=["center", "left", "center", "center", "left", "center"],
        )
    y = row(y + 16, [420, full_w - 420], 82, ["Reason for Amendment for Old User", value("amendment_reason") or "-"], fonts=[bold_font, body_font], max_lines=3)
    y += 24

    if is_branch_request:
        sign_widths = [full_w // 4, full_w // 4, full_w // 4, full_w - (full_w // 4) * 3]
        sign_labels = ["User Requested By", "Recommended By", "Second Recommended By", "Approved By"]
        name_values = [value("requested_by_name"), value("recommended_by_name"), value("branch_second_recommended_by_name"), value("approved_by_name")]
        designation_values = [value("requested_by_designation"), value("recommended_by_designation"), value("branch_second_recommended_by_designation"), value("approved_by_designation")]
        date_values = [value("requested_by_date"), value("recommended_by_date"), value("branch_second_recommended_by_date"), value("approved_by_date")]
    else:
        sign_widths = [full_w // 3, full_w // 3, full_w - (full_w // 3) * 2]
        sign_labels = ["User Requested By", "Recommended By", "Approved By"]
        name_values = [value("requested_by_name"), value("recommended_by_name"), value("approved_by_name")]
        designation_values = [value("requested_by_designation"), value("recommended_by_designation"), value("approved_by_designation")]
        date_values = [value("requested_by_date"), value("recommended_by_date"), value("approved_by_date")]
    y = row(y, sign_widths, 52, sign_labels, fonts=[bold_font] * len(sign_widths), fills=[light] * len(sign_widths), aligns=["center"] * len(sign_widths))
    y = row(y, sign_widths, 92, ["Digital Signature"] * len(sign_widths), fonts=[small_font] * len(sign_widths), aligns=["center"] * len(sign_widths))
    y = row(y, sign_widths, 58, [f"Name: {item or '-'}" for item in name_values], fonts=[body_font] * len(sign_widths))
    y = row(y, sign_widths, 58, [f"Designation: {item or '-'}" for item in designation_values], fonts=[body_font] * len(sign_widths))
    y = row(y, sign_widths, 58, [f"Date: {item or '-'}" for item in date_values], fonts=[body_font] * len(sign_widths))
    y += 28

    endorsement = (
        "3. ENDORSEMENT BY USER: By signing below I acknowledge that I am the authorized user of the Pumori System "
        "and possess the appropriate power to login with aforementioned user id. I will keep my user id and password "
        "as equal to my signature. The information provided herein is correct and true in my knowledge."
    )
    cell(margin, y, full_w, 150, endorsement, small_font, max_lines=5)

    output = BytesIO()
    if image_format == "jpeg":
        image.save(output, format="JPEG", quality=92)
    else:
        image.save(output, format="PNG")
    return [output.getvalue()]


def _docx_cell_text(cell):
    return "".join(node.text or "" for node in cell.findall(".//w:t", WORD_NS))


def _set_docx_cell_text(cell, value):
    text_nodes = cell.findall(".//w:t", WORD_NS)
    value = str(value or "")
    if text_nodes:
        text_nodes[0].text = value
        for node in text_nodes[1:]:
            node.text = ""
        return
    paragraph = cell.find("./w:p", WORD_NS)
    if paragraph is None:
        paragraph = ET.SubElement(cell, f"{{{WORD_NS['w']}}}p")
    run = ET.SubElement(paragraph, f"{{{WORD_NS['w']}}}r")
    text = ET.SubElement(run, f"{{{WORD_NS['w']}}}t")
    text.text = value


def _clear_docx_paragraph_text(paragraph):
    for node in paragraph.findall(".//w:t", WORD_NS):
        node.text = ""


def _set_docx_paragraph_text(paragraph, value):
    _clear_docx_paragraph_text(paragraph)
    runs = paragraph.findall("./w:r", WORD_NS)
    if runs:
        run = runs[0]
    else:
        run = ET.SubElement(paragraph, f"{{{WORD_NS['w']}}}r")
    text = run.find("w:t", WORD_NS)
    if text is None:
        text = ET.SubElement(run, f"{{{WORD_NS['w']}}}t")
    text.text = str(value or "")


def _clear_docx_paragraph_runs(paragraph):
    for child in list(paragraph):
        if child.tag == f"{{{WORD_NS['w']}}}r":
            paragraph.remove(child)


def _append_docx_text_run(paragraph, value="", *, bold=False, font_name="Times New Roman", font_size="24"):
    run = ET.SubElement(paragraph, f"{{{WORD_NS['w']}}}r")
    _set_docx_run_font(run, font_name=font_name, font_size=font_size, bold=bold)
    text_node = ET.SubElement(run, f"{{{WORD_NS['w']}}}t")
    if str(value or "").startswith(" ") or str(value or "").endswith(" "):
        text_node.set(f"{{http://www.w3.org/XML/1998/namespace}}space", "preserve")
    text_node.text = str(value or "")
    return run


def _append_docx_line_break(paragraph):
    run = ET.SubElement(paragraph, f"{{{WORD_NS['w']}}}r")
    ET.SubElement(run, f"{{{WORD_NS['w']}}}br")
    return run


def _set_docx_run_font(run, *, font_name="Times New Roman", font_size="24", bold=False):
    run_properties = run.find("w:rPr", WORD_NS)
    if run_properties is None:
        run_properties = ET.Element(f"{{{WORD_NS['w']}}}rPr")
        run.insert(0, run_properties)

    for bold_node in list(run_properties.findall("w:b", WORD_NS)):
        run_properties.remove(bold_node)
    for bold_node in list(run_properties.findall("w:bCs", WORD_NS)):
        run_properties.remove(bold_node)
    if bold:
        ET.SubElement(run_properties, f"{{{WORD_NS['w']}}}b")
        ET.SubElement(run_properties, f"{{{WORD_NS['w']}}}bCs")

    fonts = run_properties.find("w:rFonts", WORD_NS)
    if fonts is None:
        fonts = ET.SubElement(run_properties, f"{{{WORD_NS['w']}}}rFonts")
    for attr in ("ascii", "hAnsi", "cs", "eastAsia"):
        fonts.set(f"{{{WORD_NS['w']}}}{attr}", font_name)

    for size_tag in ("sz", "szCs"):
        size_node = run_properties.find(f"w:{size_tag}", WORD_NS)
        if size_node is None:
            size_node = ET.SubElement(run_properties, f"{{{WORD_NS['w']}}}{size_tag}")
        size_node.set(f"{{{WORD_NS['w']}}}val", font_size)


def _normalize_docx_text_font(root, *, font_name="Times New Roman", font_size="24", bold=False):
    for run_properties in root.findall(".//w:rPr", WORD_NS):
        for bold_node in list(run_properties.findall("w:b", WORD_NS)):
            run_properties.remove(bold_node)
        for bold_node in list(run_properties.findall("w:bCs", WORD_NS)):
            run_properties.remove(bold_node)
        if bold:
            ET.SubElement(run_properties, f"{{{WORD_NS['w']}}}b")
            ET.SubElement(run_properties, f"{{{WORD_NS['w']}}}bCs")

        fonts = run_properties.find("w:rFonts", WORD_NS)
        if fonts is None:
            fonts = ET.SubElement(run_properties, f"{{{WORD_NS['w']}}}rFonts")
        for attr in ("ascii", "hAnsi", "cs", "eastAsia"):
            fonts.set(f"{{{WORD_NS['w']}}}{attr}", font_name)

        for size_tag in ("sz", "szCs"):
            size_node = run_properties.find(f"w:{size_tag}", WORD_NS)
            if size_node is None:
                size_node = ET.SubElement(run_properties, f"{{{WORD_NS['w']}}}{size_tag}")
            size_node.set(f"{{{WORD_NS['w']}}}val", font_size)

    for run in root.findall(".//w:r", WORD_NS):
        if run.find("w:t", WORD_NS) is not None and run.find("w:rPr", WORD_NS) is None:
            _set_docx_run_font(run, font_name=font_name, font_size=font_size, bold=bold)


def _apply_incident_docx_header_bold(root, *, font_name="Times New Roman", font_size="24"):
    exact_headers = {
        "Incident Report Template",
        "Best Finance Company Ltd.",
        "INCIDENT REPORT INFORMATION",
        "BREACH REPORTING EMPLOYEE’S INFORMATION",
        "BREACH REPORTING EMPLOYEE'S INFORMATION",
        "INCIDENT DETAILS",
        "INCIDENT IMPACT",
        "INCIDENT RESPONSE DETAILS",
        "INCIDENT INFORMATION SHARING",
        "ATTACHMENTS (IF APPLICABLE)",
    }
    for paragraph in root.findall(".//w:p", WORD_NS):
        paragraph_text = "".join(node.text or "" for node in paragraph.findall(".//w:t", WORD_NS)).strip()
        if not paragraph_text:
            continue
        is_upper_header = paragraph_text == paragraph_text.upper() and any(char.isalpha() for char in paragraph_text)
        if paragraph_text not in exact_headers and not is_upper_header:
            continue
        for run in paragraph.findall(".//w:r", WORD_NS):
            if run.find("w:t", WORD_NS) is not None:
                _set_docx_run_font(run, font_name=font_name, font_size=font_size, bold=True)


def _apply_incident_docx_label_bold(root, *, font_name="Times New Roman", font_size="24"):
    for paragraph in root.findall(".//w:p", WORD_NS):
        paragraph_text = "".join(node.text or "" for node in paragraph.findall(".//w:t", WORD_NS)).strip()
        if not paragraph_text or ":" not in paragraph_text:
            continue
        label_text = paragraph_text.split(":", 1)[0].strip()
        if not label_text or len(label_text) > 90:
            continue
        for run in paragraph.findall(".//w:r", WORD_NS):
            text_node = run.find("w:t", WORD_NS)
            run_text = (text_node.text or "") if text_node is not None else ""
            if not run_text:
                continue
            _set_docx_run_font(run, font_name=font_name, font_size=font_size, bold=True)
            if ":" in run_text:
                break


def _clear_docx_checkbox_glyphs(root):
    for text_node in root.findall(".//w:t", WORD_NS):
        if (text_node.text or "").strip() in {"☐", "☑"}:
            text_node.text = ""


def _clear_incident_docx_placeholders(root):
    placeholders = {"[title]", "title"}
    parent_map = {child: parent for parent in root.iter() for child in parent}
    for paragraph in root.findall(".//w:p", WORD_NS):
        text_nodes = paragraph.findall(".//w:t", WORD_NS)
        paragraph_text = "".join(node.text or "" for node in text_nodes).strip()
        normalized_text = paragraph_text.replace("\u00a0", " ").replace("\ufeff", "").strip().casefold()
        is_title_placeholder = normalized_text in placeholders or (
            "title" in normalized_text and "[" in normalized_text and "]" in normalized_text
        )
        if is_title_placeholder:
            parent = parent_map.get(paragraph)
            if parent is not None:
                parent.remove(paragraph)
            else:
                for node in text_nodes:
                    node.text = ""


def _remove_incident_docx_severity_option_table(root):
    parent_map = {child: parent for parent in root.iter() for child in parent}
    for table in list(root.findall(".//w:tbl", WORD_NS)):
        table_text = "".join(node.text or "" for node in table.findall(".//w:t", WORD_NS))
        normalized = "".join(table_text.split()).casefold()
        if normalized == "criticalhighmediumlow" or (
            normalized.startswith("criticalhighmediumlow")
            and ("☐" in table_text or "☑" in table_text)
        ):
            parent = parent_map.get(table)
            if parent is not None:
                parent.remove(table)
            continue

    parent_map = {child: parent for parent in root.iter() for child in parent}
    for table in list(root.findall(".//w:tbl", WORD_NS)):
        table_text = "".join(node.text or "" for node in table.findall(".//w:t", WORD_NS)).strip()
        if table_text:
            continue
        parent = parent_map.get(table)
        if parent is None:
            continue
        siblings = list(parent)
        try:
            table_index = siblings.index(table)
        except ValueError:
            continue
        preceding_text = ""
        for sibling in reversed(siblings[max(0, table_index - 3):table_index]):
            preceding_text = "".join(node.text or "" for node in sibling.findall(".//w:t", WORD_NS)) + preceding_text
        if "incident severity" in preceding_text.casefold():
            parent.remove(table)


def _clear_incident_docx_header(root):
    for child in list(root):
        root.remove(child)


def _set_docx_paragraph_alignment(paragraph, alignment="center"):
    paragraph_properties = paragraph.find("w:pPr", WORD_NS)
    if paragraph_properties is None:
        paragraph_properties = ET.Element(f"{{{WORD_NS['w']}}}pPr")
        paragraph.insert(0, paragraph_properties)
    justification = paragraph_properties.find("w:jc", WORD_NS)
    if justification is None:
        justification = ET.SubElement(paragraph_properties, f"{{{WORD_NS['w']}}}jc")
    justification.set(f"{{{WORD_NS['w']}}}val", alignment)


def _set_docx_paragraph_spacing(paragraph, *, before=None, after=None, line=None):
    paragraph_properties = paragraph.find("w:pPr", WORD_NS)
    if paragraph_properties is None:
        paragraph_properties = ET.Element(f"{{{WORD_NS['w']}}}pPr")
        paragraph.insert(0, paragraph_properties)
    spacing = paragraph_properties.find("w:spacing", WORD_NS)
    if spacing is None:
        spacing = ET.SubElement(paragraph_properties, f"{{{WORD_NS['w']}}}spacing")
    if before is not None:
        spacing.set(f"{{{WORD_NS['w']}}}before", str(before))
    if after is not None:
        spacing.set(f"{{{WORD_NS['w']}}}after", str(after))
    if line is not None:
        spacing.set(f"{{{WORD_NS['w']}}}line", str(line))
        spacing.set(f"{{{WORD_NS['w']}}}lineRule", "auto")


def _set_docx_row_height(row, height):
    row_properties = row.find("w:trPr", WORD_NS)
    if row_properties is None:
        row_properties = ET.Element(f"{{{WORD_NS['w']}}}trPr")
        row.insert(0, row_properties)
    row_height = row_properties.find("w:trHeight", WORD_NS)
    if row_height is None:
        row_height = ET.SubElement(row_properties, f"{{{WORD_NS['w']}}}trHeight")
    row_height.set(f"{{{WORD_NS['w']}}}val", str(height))
    row_height.set(f"{{{WORD_NS['w']}}}hRule", "atLeast")


def _normalize_incident_docx_cover_page(root):
    tables = root.findall(".//w:tbl", WORD_NS)
    if not tables:
        return
    cover_table = tables[0]
    table_properties = cover_table.find("w:tblPr", WORD_NS)
    if table_properties is None:
        table_properties = ET.Element(f"{{{WORD_NS['w']}}}tblPr")
        cover_table.insert(0, table_properties)
    table_width = table_properties.find("w:tblW", WORD_NS)
    if table_width is None:
        table_width = ET.SubElement(table_properties, f"{{{WORD_NS['w']}}}tblW")
    table_width.set(f"{{{WORD_NS['w']}}}w", "9355")
    table_width.set(f"{{{WORD_NS['w']}}}type", "dxa")
    table_layout = table_properties.find("w:tblLayout", WORD_NS)
    if table_layout is None:
        table_layout = ET.SubElement(table_properties, f"{{{WORD_NS['w']}}}tblLayout")
    table_layout.set(f"{{{WORD_NS['w']}}}type", "fixed")

    existing_rows = cover_table.findall("./w:tr", WORD_NS)
    existing_texts = [
        "".join(node.text or "" for node in row.findall(".//w:t", WORD_NS)).strip()
        for row in existing_rows
    ]
    logo_paragraph = None
    for paragraph in cover_table.findall(".//w:p", WORD_NS):
        if paragraph.find(".//w:drawing", WORD_NS) is not None:
            logo_paragraph = ET.fromstring(ET.tostring(paragraph, encoding="utf-8"))
            break

    title_text = next((text for text in existing_texts if "Incident Report Template" in text), "Incident Report Template")
    company_text = next((text for text in existing_texts if "Best Finance Company" in text), "Best Finance Company Ltd.")
    version_text = next((text for text in existing_texts if text.startswith("Version")), "Version: 1.0")
    date_text = next(
        (
            text
            for text in reversed(existing_texts)
            if text and "Incident Report Template" not in text and "Best Finance Company" not in text and not text.startswith("Version")
        ),
        "",
    )

    for child in list(cover_table):
        if child.tag not in {f"{{{WORD_NS['w']}}}tblPr", f"{{{WORD_NS['w']}}}tblGrid"}:
            cover_table.remove(child)

    table_grid = cover_table.find("w:tblGrid", WORD_NS)
    if table_grid is None:
        table_grid = ET.SubElement(cover_table, f"{{{WORD_NS['w']}}}tblGrid")
    for grid_column in list(table_grid):
        table_grid.remove(grid_column)
    ET.SubElement(table_grid, f"{{{WORD_NS['w']}}}gridCol", {f"{{{WORD_NS['w']}}}w": "9355"})

    def _cover_row(height, text="", *, paragraph=None):
        row = ET.Element(f"{{{WORD_NS['w']}}}tr")
        _set_docx_row_height(row, height)
        cell = ET.SubElement(row, f"{{{WORD_NS['w']}}}tc")
        cell_properties = ET.SubElement(cell, f"{{{WORD_NS['w']}}}tcPr")
        ET.SubElement(cell_properties, f"{{{WORD_NS['w']}}}tcW", {f"{{{WORD_NS['w']}}}w": "9355", f"{{{WORD_NS['w']}}}type": "dxa"})
        ET.SubElement(cell_properties, f"{{{WORD_NS['w']}}}vAlign", {f"{{{WORD_NS['w']}}}val": "center"})
        if paragraph is not None:
            cell.append(paragraph)
        else:
            paragraph = ET.SubElement(cell, f"{{{WORD_NS['w']}}}p")
            if text:
                run = ET.SubElement(paragraph, f"{{{WORD_NS['w']}}}r")
                text_node = ET.SubElement(run, f"{{{WORD_NS['w']}}}t")
                text_node.text = str(text)
        _set_docx_paragraph_alignment(paragraph, "center")
        _set_docx_paragraph_spacing(paragraph, before=0, after=0, line=300)
        return row

    if logo_paragraph is not None:
        cover_table.append(_cover_row(1400, paragraph=logo_paragraph))
    else:
        cover_table.append(_cover_row(900))
    cover_table.append(_cover_row(520, title_text))
    cover_table.append(_cover_row(680, company_text))
    cover_table.append(_cover_row(560, version_text))
    if date_text:
        cover_table.append(_cover_row(560, date_text))


def _format_cbs_docx_value(value):
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%m/%d/%Y")
    return str(value)


def _cbs_signature_png_payload(user):
    upload = getattr(user, "signature_image", None)
    return _cbs_signature_field_png_payload(upload)


def _cbs_signature_field_png_payload(upload):
    if not upload:
        return None
    try:
        if hasattr(upload, "open"):
            upload.open("rb")
        if hasattr(upload, "seek"):
            upload.seek(0)
        payload = upload.read() if hasattr(upload, "read") else None
        if not payload:
            return None
        image = Image.open(BytesIO(payload)).convert("RGBA")
        image.thumbnail((520, 180))
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    except Exception:
        return None
    finally:
        try:
            if hasattr(upload, "close"):
                upload.close()
        except Exception:
            pass


def _incident_signature_png_payload(upload):
    if not upload:
        return None
    try:
        if hasattr(upload, "open"):
            upload.open("rb")
        if hasattr(upload, "seek"):
            upload.seek(0)
        payload = upload.read() if hasattr(upload, "read") else None
        if not payload:
            return None
        image = Image.open(BytesIO(payload)).convert("RGBA")
        image.thumbnail((520, 180))
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    except Exception:
        return None
    finally:
        try:
            if hasattr(upload, "close"):
                upload.close()
        except Exception:
            pass


def _cbs_access_acknowledgement_user(data):
    access_user = data.get("access_user")
    if access_user is not None and hasattr(access_user, "signature_image"):
        return access_user
    access_user_id = (data.get("access_user_id") or "").strip()
    if access_user_id.isdigit():
        return CustomUser.objects.filter(id=int(access_user_id), is_active=True).first()
    return None


def _cbs_requested_signature_user(data):
    requested_user = data.get("requested_signature_user")
    if requested_user is not None and hasattr(requested_user, "signature_image"):
        return requested_user
    requested_user_id = (data.get("requested_signature_user_id") or "").strip()
    if requested_user_id.isdigit():
        user = CustomUser.objects.filter(id=int(requested_user_id), is_active=True).first()
        if user is not None:
            return user
    requested_name = (data.get("requested_by_name") or data.get("requested_signature_name") or "").strip()
    if requested_name:
        normalized = requested_name.casefold()
        for user in CustomUser.objects.filter(is_active=True).order_by("first_name", "last_name", "username"):
            candidates = {
                (getattr(user, "username", "") or "").strip().casefold(),
                ((user.get_full_name() or "").strip()).casefold(),
            }
            if normalized in {candidate for candidate in candidates if candidate}:
                return user
    return None


def _cbs_access_snapshot_field(remote_access_approval, field_name):
    if remote_access_approval is None:
        return None
    snapshot = getattr(remote_access_approval, field_name, None)
    return snapshot if snapshot else None


def _cbs_recommendation_signature_allowed(remote_access_approval):
    return bool(
        remote_access_approval
        and getattr(remote_access_approval, "recommended_by_id", None)
        and remote_access_approval.status != RemoteAccessApproval.STATUS_REJECTED
    )


def _cbs_second_recommendation_signature_allowed(remote_access_approval):
    return bool(
        remote_access_approval
        and getattr(remote_access_approval, "second_recommended_by_id", None)
        and remote_access_approval.status != RemoteAccessApproval.STATUS_REJECTED
    )


def _cbs_approval_signature_allowed(remote_access_approval):
    return bool(
        remote_access_approval
        and getattr(remote_access_approval, "decided_by_id", None)
        and remote_access_approval.status == RemoteAccessApproval.STATUS_APPROVED
    )


def _next_docx_relationship_id(rels_root):
    max_id = 0
    for relationship in rels_root.findall(f"{{{DOCX_REL_NS}}}Relationship"):
        rel_id = relationship.attrib.get("Id", "")
        if rel_id.startswith("rId") and rel_id[3:].isdigit():
            max_id = max(max_id, int(rel_id[3:]))
    return f"rId{max_id + 1}"


def _ensure_docx_png_content_type(content_types_root):
    for default in content_types_root.findall(f"{{{DOCX_CONTENT_TYPES_NS}}}Default"):
        if (default.attrib.get("Extension") or "").lower() == "png":
            return
    ET.SubElement(
        content_types_root,
        f"{{{DOCX_CONTENT_TYPES_NS}}}Default",
        {"Extension": "png", "ContentType": "image/png"},
    )


def _docx_logo_png_payload():
    for logo_path in DOCX_LOGO_IMAGE_PATHS:
        if not os.path.exists(logo_path):
            continue
        try:
            image = Image.open(logo_path).convert("RGBA")
            image.thumbnail((430, 105))
            output = BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()
        except Exception:
            continue
    return None


def _add_docx_png_relationship(rels_root, content_types_root, media_items, image_name, payload):
    if not payload:
        return ""
    media_path = f"word/media/{image_name}.png"
    rel_id = _next_docx_relationship_id(rels_root)
    ET.SubElement(
        rels_root,
        f"{{{DOCX_REL_NS}}}Relationship",
        {
            "Id": rel_id,
            "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
            "Target": f"media/{image_name}.png",
        },
    )
    _ensure_docx_png_content_type(content_types_root)
    media_items[media_path] = payload
    return rel_id


def _prepend_docx_logo_to_cell(cell, relationship_id):
    _append_docx_image_to_existing_cell(
        cell,
        relationship_id,
        name="Best Finance Company Logo",
        width_emu="2095500",
        height_emu="499110",
        alignment="center",
        prepend=True,
    )


def _remove_docx_leading_empty_paragraphs(body):
    if body is None:
        return
    for child in list(body):
        local_name = child.tag.rsplit("}", 1)[-1]
        if local_name != "p":
            break
        has_text = any((node.text or "").strip() for node in child.findall(".//w:t", WORD_NS))
        has_drawing = child.find(".//w:drawing", WORD_NS) is not None
        has_section = child.find(".//w:sectPr", WORD_NS) is not None
        if has_text or has_drawing or has_section:
            break
        body.remove(child)


def _append_docx_image_to_cell(cell, relationship_id, *, name="Digital signature"):
    _set_docx_cell_text(cell, "")
    _append_docx_image_to_existing_cell(cell, relationship_id, name=name)


def _append_docx_image_to_existing_cell(
    cell,
    relationship_id,
    *,
    name="Digital signature",
    width_emu="1714500",
    height_emu="594360",
    alignment=None,
    prepend=False,
):
    if prepend:
        paragraph = ET.Element(f"{{{WORD_NS['w']}}}p")
        insert_index = 1 if len(cell) and cell[0].tag == f"{{{WORD_NS['w']}}}tcPr" else 0
        cell.insert(insert_index, paragraph)
    else:
        paragraph = ET.SubElement(cell, f"{{{WORD_NS['w']}}}p")
    if alignment:
        paragraph_properties = ET.SubElement(paragraph, f"{{{WORD_NS['w']}}}pPr")
        ET.SubElement(paragraph_properties, f"{{{WORD_NS['w']}}}jc", {f"{{{WORD_NS['w']}}}val": alignment})
    run = ET.SubElement(paragraph, f"{{{WORD_NS['w']}}}r")
    drawing = ET.SubElement(run, f"{{{WORD_NS['w']}}}drawing")

    wp_ns = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
    a_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    pic_ns = "http://schemas.openxmlformats.org/drawingml/2006/picture"
    r_ns = DOCX_WORD_REL_NS

    inline = ET.SubElement(drawing, f"{{{wp_ns}}}inline", {"distT": "0", "distB": "0", "distL": "0", "distR": "0"})
    ET.SubElement(inline, f"{{{wp_ns}}}extent", {"cx": width_emu, "cy": height_emu})
    ET.SubElement(inline, f"{{{wp_ns}}}effectExtent", {"l": "0", "t": "0", "r": "0", "b": "0"})
    ET.SubElement(inline, f"{{{wp_ns}}}docPr", {"id": str(secrets.randbelow(900000) + 100000), "name": name})
    ET.SubElement(inline, f"{{{wp_ns}}}cNvGraphicFramePr")
    graphic = ET.SubElement(inline, f"{{{a_ns}}}graphic")
    graphic_data = ET.SubElement(graphic, f"{{{a_ns}}}graphicData", {"uri": pic_ns})
    pic = ET.SubElement(graphic_data, f"{{{pic_ns}}}pic")
    nv_pic_pr = ET.SubElement(pic, f"{{{pic_ns}}}nvPicPr")
    ET.SubElement(nv_pic_pr, f"{{{pic_ns}}}cNvPr", {"id": "0", "name": f"{name}.png"})
    ET.SubElement(nv_pic_pr, f"{{{pic_ns}}}cNvPicPr")
    blip_fill = ET.SubElement(pic, f"{{{pic_ns}}}blipFill")
    ET.SubElement(blip_fill, f"{{{a_ns}}}blip", {f"{{{r_ns}}}embed": relationship_id})
    stretch = ET.SubElement(blip_fill, f"{{{a_ns}}}stretch")
    ET.SubElement(stretch, f"{{{a_ns}}}fillRect")
    sp_pr = ET.SubElement(pic, f"{{{pic_ns}}}spPr")
    xfrm = ET.SubElement(sp_pr, f"{{{a_ns}}}xfrm")
    ET.SubElement(xfrm, f"{{{a_ns}}}off", {"x": "0", "y": "0"})
    ET.SubElement(xfrm, f"{{{a_ns}}}ext", {"cx": width_emu, "cy": height_emu})
    prst_geom = ET.SubElement(sp_pr, f"{{{a_ns}}}prstGeom", {"prst": "rect"})
    ET.SubElement(prst_geom, f"{{{a_ns}}}avLst")


def _build_cbs_access_docx(cleaned_data=None, remote_access_approval=None):
    data = dict(cleaned_data or {})
    request_type = data.get("request_type") or "cbs_access_ho"
    template_path = _cbs_access_template_path(request_type)
    request_id = (data.get("request_id") or data.get("ticket_id") or "").strip()

    def display_user(user):
        if not user:
            return ""
        return (user.get_full_name() or "").strip() or (getattr(user, "username", "") or "").strip()

    if remote_access_approval is not None:
        if getattr(remote_access_approval, "recommended_by_id", None):
            data["recommended_by_name"] = display_user(remote_access_approval.recommended_by) or data.get("recommended_by_name", "")
            data["recommended_by_designation"] = getattr(remote_access_approval.recommended_by, "position", "") or data.get("recommended_by_designation", "")
            data["recommended_by_date"] = timezone.localtime(remote_access_approval.recommended_at).strftime("%m/%d/%Y") if remote_access_approval.recommended_at else data.get("recommended_by_date", "")
        if getattr(remote_access_approval, "second_recommended_by_id", None):
            data["branch_second_recommended_by_name"] = display_user(remote_access_approval.second_recommended_by) or data.get("branch_second_recommended_by_name", "")
            data["branch_second_recommended_by_designation"] = getattr(remote_access_approval.second_recommended_by, "position", "") or data.get("branch_second_recommended_by_designation", "")
            data["branch_second_recommended_by_date"] = timezone.localtime(remote_access_approval.second_recommended_at).strftime("%m/%d/%Y") if remote_access_approval.second_recommended_at else data.get("branch_second_recommended_by_date", "")
        if getattr(remote_access_approval, "decided_by_id", None):
            data["approved_by_name"] = display_user(remote_access_approval.decided_by) or data.get("approved_by_name", "")
            data["approved_by_designation"] = getattr(remote_access_approval.decided_by, "position", "") or data.get("approved_by_designation", "")
            data["approved_by_date"] = timezone.localtime(remote_access_approval.decided_at).strftime("%m/%d/%Y") if remote_access_approval.decided_at else data.get("approved_by_date", "")

    selected_groups = set(data.get("user_groups") or [])
    with zipfile.ZipFile(template_path, "r") as source_docx:
        document_xml = source_docx.read("word/document.xml")
        rels_xml = source_docx.read("word/_rels/document.xml.rels")
        content_types_xml = source_docx.read("[Content_Types].xml")
        root = ET.fromstring(document_xml)
        rels_root = ET.fromstring(rels_xml)
        content_types_root = ET.fromstring(content_types_xml)
        body = root.find("w:body", WORD_NS)
        tables = root.findall(".//w:tbl", WORD_NS)
        if body is None or not tables:
            raise ValueError("CBS access DOCX template does not contain a table.")

        first_table = tables[0]
        for child in list(body):
            tag_name = child.tag.rsplit("}", 1)[-1]
            if child is first_table or tag_name == "sectPr":
                continue
            body.remove(child)

        rows = first_table.findall("./w:tr", WORD_NS)

        def set_cell(row_index, cell_index, value):
            cells = rows[row_index].findall("./w:tc", WORD_NS)
            if cell_index < len(cells):
                _set_docx_cell_text(cells[cell_index], value)

        is_branch_request = request_type == "cbs_access_branch"
        if rows:
            title_cells = rows[0].findall("./w:tc", WORD_NS)
            if title_cells:
                title_text = _docx_cell_text(title_cells[0]).strip()
                if "Request ID:" not in title_text:
                    title_text = f"{title_text}        Request ID: {request_id}".rstrip()
                elif request_id:
                    title_text = f"{title_text} {request_id}".rstrip()
                _set_docx_cell_text(title_cells[0], title_text)
        set_cell(2, 1, _format_cbs_docx_value(data.get("name")))
        set_cell(3, 1, _format_cbs_docx_value(data.get("designation")))
        set_cell(4, 1, _format_cbs_docx_value(data.get("department")))
        set_cell(5, 1, _format_cbs_docx_value(data.get("employee_id")))
        set_cell(6, 1, "√" if data.get("user_type") == "new" else "")
        set_cell(7, 1, "√" if data.get("user_type") == "amendment" else "")
        set_cell(7, 4, _format_cbs_docx_value(data.get("old_user_id")))

        group_rows = rows[10:15] if is_branch_request else rows[10:25]
        for row in group_rows:
            cells = row.findall("./w:tc", WORD_NS)
            if len(cells) >= 6:
                left_code = _docx_cell_text(cells[0]).strip()
                right_code = _docx_cell_text(cells[3]).strip()
                _set_docx_cell_text(cells[2], "√" if left_code in selected_groups else "")
                _set_docx_cell_text(cells[5], "√" if right_code in selected_groups else "")
            elif len(cells) >= 3:
                code = _docx_cell_text(cells[0]).strip()
                _set_docx_cell_text(cells[2], "√" if code in selected_groups else "")

        reason_row_index = 15 if is_branch_request else 25
        signature_row_index = 17 if is_branch_request else 27
        name_row_index = 18 if is_branch_request else 28
        designation_row_index = 19 if is_branch_request else 29
        date_row_index = 20 if is_branch_request else 30
        endorsement_row_index = 22 if is_branch_request else 32
        set_cell(reason_row_index, 0, f"Reason for Amendment for Old User:  {_format_cbs_docx_value(data.get('amendment_reason'))}")
        for cell_index in range(4 if is_branch_request else 3):
            set_cell(signature_row_index, cell_index, "")

        media_items = {}
        logo_payload = _docx_logo_png_payload()
        if logo_payload and rows:
            logo_rel_id = _add_docx_png_relationship(
                rels_root,
                content_types_root,
                media_items,
                "bfc_cbs_access_logo",
                logo_payload,
            )
            cells = rows[0].findall("./w:tc", WORD_NS)
            if logo_rel_id and cells:
                _prepend_docx_logo_to_cell(cells[0], logo_rel_id)

        def add_signature_to_cell(row_index, cell_index, user, image_name, *, append=False):
            payload = _cbs_signature_png_payload(user)
            add_signature_payload_to_cell(row_index, cell_index, payload, image_name, append=append)

        def add_signature_snapshot_to_cell(row_index, cell_index, snapshot, image_name, *, append=False):
            payload = _cbs_signature_field_png_payload(snapshot)
            add_signature_payload_to_cell(row_index, cell_index, payload, image_name, append=append)

        def add_signature_payload_to_cell(row_index, cell_index, payload, image_name, *, append=False):
            if not payload:
                return
            media_path = f"word/media/{image_name}.png"
            rel_id = _next_docx_relationship_id(rels_root)
            ET.SubElement(
                rels_root,
                f"{{{DOCX_REL_NS}}}Relationship",
                {
                    "Id": rel_id,
                    "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
                    "Target": f"media/{image_name}.png",
                },
            )
            _ensure_docx_png_content_type(content_types_root)
            media_items[media_path] = payload
            cells = rows[row_index].findall("./w:tc", WORD_NS)
            if cell_index < len(cells):
                if append:
                    _append_docx_image_to_existing_cell(cells[cell_index], rel_id, name=image_name.replace("_", " ").title())
                else:
                    _append_docx_image_to_cell(cells[cell_index], rel_id, name=image_name.replace("_", " ").title())

        requested_signature_snapshot = _cbs_access_snapshot_field(remote_access_approval, "requested_signature_snapshot")
        if requested_signature_snapshot:
            add_signature_snapshot_to_cell(signature_row_index, 0, requested_signature_snapshot, "cbs_requested_signature")
        else:
            requested_signature_user = _cbs_requested_signature_user(data)
            add_signature_to_cell(signature_row_index, 0, requested_signature_user, "cbs_requested_signature")

        acknowledgement_snapshot = _cbs_access_snapshot_field(remote_access_approval, "access_user_signature_snapshot")
        acknowledgement_user = _cbs_access_acknowledgement_user(data) if not acknowledgement_snapshot else None
        if (acknowledgement_snapshot or acknowledgement_user is not None) and len(rows) > endorsement_row_index:
            acknowledgement_cells = rows[endorsement_row_index].findall("./w:tc", WORD_NS)
            if acknowledgement_cells:
                signed_at = _format_cbs_docx_value(data.get("access_user_signed_at") or timezone.localdate())
                _set_docx_cell_text(
                    acknowledgement_cells[0],
                    "By signing below I acknowledge that I am the authorized user of the Pumori System and possess the appropriate power to login with aforementioned user id. I will keep my user id and password as equal to my signature. The information provided herein is correct and true in my knowledge.\n\n"
                    f"User Acknowledgement Signature:\n\nDate: {signed_at}",
                )
                if acknowledgement_snapshot:
                    add_signature_snapshot_to_cell(endorsement_row_index, 0, acknowledgement_snapshot, "cbs_access_user_acknowledgement_signature", append=True)
                else:
                    add_signature_to_cell(endorsement_row_index, 0, acknowledgement_user, "cbs_access_user_acknowledgement_signature", append=True)

        if remote_access_approval is not None:
            recommended_snapshot = _cbs_access_snapshot_field(remote_access_approval, "recommended_signature_snapshot")
            second_recommended_snapshot = _cbs_access_snapshot_field(remote_access_approval, "second_recommended_signature_snapshot")
            approved_snapshot = _cbs_access_snapshot_field(remote_access_approval, "approved_signature_snapshot")
            recommended_cell_index = 1
            second_recommended_cell_index = 2
            approved_cell_index = 3 if is_branch_request else 2
            if _cbs_recommendation_signature_allowed(remote_access_approval) and recommended_snapshot:
                add_signature_snapshot_to_cell(signature_row_index, recommended_cell_index, recommended_snapshot, "cbs_recommended_signature")
            elif _cbs_recommendation_signature_allowed(remote_access_approval):
                add_signature_to_cell(signature_row_index, recommended_cell_index, remote_access_approval.recommended_by, "cbs_recommended_signature")
            if is_branch_request and _cbs_second_recommendation_signature_allowed(remote_access_approval) and second_recommended_snapshot:
                add_signature_snapshot_to_cell(signature_row_index, second_recommended_cell_index, second_recommended_snapshot, "cbs_second_recommended_signature")
            elif is_branch_request and _cbs_second_recommendation_signature_allowed(remote_access_approval):
                add_signature_to_cell(signature_row_index, second_recommended_cell_index, remote_access_approval.second_recommended_by, "cbs_second_recommended_signature")
            if _cbs_approval_signature_allowed(remote_access_approval) and approved_snapshot:
                add_signature_snapshot_to_cell(signature_row_index, approved_cell_index, approved_snapshot, "cbs_approved_signature")
            elif _cbs_approval_signature_allowed(remote_access_approval):
                add_signature_to_cell(signature_row_index, approved_cell_index, remote_access_approval.decided_by, "cbs_approved_signature")

        set_cell(name_row_index, 0, f"Name: {_format_cbs_docx_value(data.get('requested_by_name'))}")
        set_cell(name_row_index, 1, f"Name: {_format_cbs_docx_value(data.get('recommended_by_name'))}")
        if is_branch_request:
            set_cell(name_row_index, 2, f"Name: {_format_cbs_docx_value(data.get('branch_second_recommended_by_name'))}")
            set_cell(name_row_index, 3, f"Name: {_format_cbs_docx_value(data.get('approved_by_name'))}")
        else:
            set_cell(name_row_index, 2, f"Name: {_format_cbs_docx_value(data.get('approved_by_name'))}")
        set_cell(designation_row_index, 0, f"Designation: {_format_cbs_docx_value(data.get('requested_by_designation'))}")
        set_cell(designation_row_index, 1, f"Designation: {_format_cbs_docx_value(data.get('recommended_by_designation'))}")
        if is_branch_request:
            set_cell(designation_row_index, 2, f"Designation: {_format_cbs_docx_value(data.get('branch_second_recommended_by_designation'))}")
            set_cell(designation_row_index, 3, f"Designation: {_format_cbs_docx_value(data.get('approved_by_designation'))}")
        else:
            set_cell(designation_row_index, 2, f"Designation: {_format_cbs_docx_value(data.get('approved_by_designation'))}")
        set_cell(date_row_index, 0, f"Date: {_format_cbs_docx_value(data.get('requested_by_date'))}")
        set_cell(date_row_index, 1, f"Date: {_format_cbs_docx_value(data.get('recommended_by_date'))}")
        if is_branch_request:
            set_cell(date_row_index, 2, f"Date: {_format_cbs_docx_value(data.get('branch_second_recommended_by_date'))}")
            set_cell(date_row_index, 3, f"Date: {_format_cbs_docx_value(data.get('approved_by_date'))}")
        else:
            set_cell(date_row_index, 2, f"Date: {_format_cbs_docx_value(data.get('approved_by_date'))}")

        output = BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target_docx:
            for item in source_docx.infolist():
                if item.filename == "word/document.xml":
                    target_docx.writestr(item, ET.tostring(root, encoding="utf-8", xml_declaration=True))
                elif item.filename.startswith("word/header") and item.filename.endswith(".xml"):
                    header_root = ET.fromstring(source_docx.read(item.filename))
                    _clear_incident_docx_header(header_root)
                    target_docx.writestr(item, ET.tostring(header_root, encoding="utf-8", xml_declaration=True))
                elif item.filename == "word/glossary/document.xml":
                    glossary_root = ET.fromstring(source_docx.read(item.filename))
                    _clear_incident_docx_placeholders(glossary_root)
                    target_docx.writestr(item, ET.tostring(glossary_root, encoding="utf-8", xml_declaration=True))
                elif item.filename == "word/_rels/document.xml.rels":
                    target_docx.writestr(item, _serialize_docx_package_xml(rels_root, DOCX_REL_NS, "rel"))
                elif item.filename == "[Content_Types].xml":
                    target_docx.writestr(item, _serialize_docx_package_xml(content_types_root, DOCX_CONTENT_TYPES_NS, "ct"))
                else:
                    target_docx.writestr(item, source_docx.read(item.filename))
            for media_path, payload in media_items.items():
                target_docx.writestr(media_path, payload)
        return output.getvalue()


def _cbs_access_pdf_response(cleaned_data=None, filename="cbs-access-request.pdf", remote_access_approval=None):
    data = cleaned_data or {}
    selected_groups = set(data.get("user_groups") or [])
    page_size = (2480, 3508)
    margin_x = 150
    top_margin = 120
    bottom_margin = 130
    y = top_margin
    ink = (0, 0, 0)
    white = (255, 255, 255)
    light = (244, 244, 244)

    pages = []
    image = None
    draw = None

    def new_page():
        nonlocal image, draw, y
        image = Image.new("RGB", page_size, white)
        draw = ImageDraw.Draw(image)
        pages.append(image)
        y = top_margin

    def ensure_space(height):
        if y + height > page_size[1] - bottom_margin:
            new_page()

    new_page()

    def font(size, bold=False):
        candidates = ["arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold else ["arial.ttf", "DejaVuSans.ttf"]
        for candidate in candidates:
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
        return ImageFont.load_default()

    title_font = font(82, True)
    subtitle_font = font(66, True)
    section_font = font(62, True)
    body_font = font(58)
    bold_font = font(58, True)
    small_font = font(50)

    def value(name):
        item = data.get(name)
        if item is None:
            return ""
        if hasattr(item, "strftime"):
            return item.strftime("%m/%d/%Y")
        return str(item)

    def display_user(user):
        if not user:
            return ""
        full_name = (user.get_full_name() or "").strip()
        return full_name or (getattr(user, "username", "") or "").strip()

    if remote_access_approval is not None:
        if getattr(remote_access_approval, "recommended_by_id", None):
            data["recommended_by_name"] = display_user(remote_access_approval.recommended_by) or value("recommended_by_name")
            data["recommended_by_designation"] = (
                getattr(remote_access_approval.recommended_by, "position", "") or value("recommended_by_designation")
            )
            data["recommended_by_date"] = timezone.localtime(remote_access_approval.recommended_at).strftime("%m/%d/%Y") if remote_access_approval.recommended_at else value("recommended_by_date")
        if getattr(remote_access_approval, "decided_by_id", None):
            data["approved_by_name"] = display_user(remote_access_approval.decided_by) or value("approved_by_name")
            data["approved_by_designation"] = (
                getattr(remote_access_approval.decided_by, "position", "") or value("approved_by_designation")
            )
            data["approved_by_date"] = timezone.localtime(remote_access_approval.decided_at).strftime("%m/%d/%Y") if remote_access_approval.decided_at else value("approved_by_date")

    def text_w(text, used_font):
        box = draw.textbbox((0, 0), str(text), font=used_font)
        return box[2] - box[0]

    def center(text, top, used_font):
        x = (page_size[0] - text_w(text, used_font)) / 2
        draw.text((x, top), text, fill=ink, font=used_font)
        draw.text((x + 1, top), text, fill=ink, font=used_font)

    def wrap_lines(text, used_font, width):
        words = str(text or "").split()
        lines = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if text_w(candidate, used_font) <= width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines or [""]

    def draw_text(text, x, top, width, used_font, max_lines=3, line_gap=3):
        line_h = used_font.size + 6
        for index, line_text in enumerate(wrap_lines(text, used_font, width)[:max_lines]):
            text_y = top + index * (line_h + line_gap)
            draw.text((x, text_y), line_text, fill=ink, font=used_font)
            draw.text((x + 1, text_y), line_text, fill=ink, font=used_font)

    def cell(x, top, width, height, text="", used_font=None, fill=None, align="left", max_lines=3):
        draw.rectangle((x, top, x + width, top + height), outline=ink, fill=fill or white, width=4)
        if text:
            used_font = used_font or body_font
            tx = x + 10
            if align == "center":
                tx = x + max(10, (width - text_w(text, used_font)) / 2)
            draw_text(text, tx, top + 8, width - 20, used_font, max_lines=max_lines)

    def table_row(top, widths, height, texts, fills=None, fonts=None, aligns=None, max_lines=2):
        left = margin_x
        for index, width in enumerate(widths):
            cell(
                left,
                top,
                width,
                height,
                texts[index] if index < len(texts) else "",
                used_font=(fonts[index] if fonts else None),
                fill=(fills[index] if fills else None),
                align=(aligns[index] if aligns else "left"),
                max_lines=max_lines,
            )
            left += width
        return top + height

    def add_table_row(widths, height, texts, fills=None, fonts=None, aligns=None, max_lines=2):
        nonlocal y
        ensure_space(height)
        y = table_row(y, widths, height, texts, fills=fills, fonts=fonts, aligns=aligns, max_lines=max_lines)

    def checkbox(x, top, checked=False):
        size = 34
        draw.rectangle((x, top, x + size, top + size), outline=ink, width=4)
        if checked:
            draw.line((x + 6, top + 18, x + 15, top + 29, x + 31, top + 6), fill=ink, width=7)

    def load_signature(user, max_size=(330, 108)):
        upload = getattr(user, "signature_image", None)
        if not upload:
            return None
        try:
            if hasattr(upload, "open"):
                upload.open("rb")
            if hasattr(upload, "seek"):
                upload.seek(0)
            signature = Image.open(upload).convert("RGBA")
            signature.thumbnail(max_size)
            return signature
        except Exception:
            return None

    def paste_signature(user, box):
        signature = load_signature(user)
        if signature is None:
            x1, y1, x2, y2 = box
            draw.line((x1 + 45, y1 + 48, x2 - 45, y1 + 48), fill=ink, width=3)
            draw.text((x1 + 45, y1 + 57), "Signature pending", fill=ink, font=small_font)
            return
        x1, y1, x2, y2 = box
        px = x1 + int((x2 - x1 - signature.width) / 2)
        py = y1 + int((y2 - y1 - signature.height) / 2)
        image.paste(signature, (px, py), signature)

    full_w = page_size[0] - margin_x * 2
    center("BEST FINANCE COMPANY LIMITED", y, title_font)
    y += 118
    center("USER ID REQUEST FORM FOR CBS (For Head Office Only)", y, subtitle_font)
    y += 140

    draw.text((margin_x, y), "USER INFORMATION:", fill=ink, font=section_font)
    draw.text((margin_x + 1, y), "USER INFORMATION:", fill=ink, font=section_font)
    y += 92
    label_w = 380
    value_w = full_w - label_w
    add_table_row([label_w, value_w], 126, ["Name", value("name")], fonts=[bold_font, body_font])
    add_table_row([label_w, value_w], 126, ["Designation", value("designation")], fonts=[bold_font, body_font])
    add_table_row([label_w, value_w], 126, ["Department", value("department")], fonts=[bold_font, body_font])
    add_table_row([label_w, value_w], 126, ["Employee ID", value("employee_id")], fonts=[bold_font, body_font])
    user_type = f"New User [{'X' if value('user_type') == 'new' else ' '}]     Amendment for Old User [{'X' if value('user_type') == 'amendment' else ' '}]"
    add_table_row([label_w, value_w], 126, ["Type of User", user_type], fonts=[bold_font, body_font])
    add_table_row([label_w, value_w], 126, ["Old User ID", value("old_user_id")], fonts=[bold_font, body_font])
    y += 72

    ensure_space(180)
    draw.text((margin_x, y), "USER REQUIREMENTS AND APPROVALS:", fill=ink, font=section_font)
    draw.text((margin_x + 1, y), "USER REQUIREMENTS AND APPROVALS:", fill=ink, font=section_font)
    y += 92
    code_w = 120
    please_w = 150
    desc_w = int((full_w - (code_w + please_w) * 2) / 2)
    widths = [code_w, desc_w, please_w, code_w, desc_w, please_w]
    add_table_row(
        widths,
        134,
        ["User Group", "Description", "Please (X)", "User Group", "Description", "Please (X)"],
        fills=[light] * 6,
        fonts=[bold_font] * 6,
        aligns=["center"] * 6,
    )
    midpoint = (len(CBS_USER_GROUP_CHOICES) + 1) // 2
    for index in range(midpoint):
        left_code, left_label = CBS_USER_GROUP_CHOICES[index]
        right_code, right_label = CBS_USER_GROUP_CHOICES[index + midpoint] if index + midpoint < len(CBS_USER_GROUP_CHOICES) else ("", "")
        ensure_space(118)
        row_top = y
        y = table_row(
            y,
            widths,
            118,
            [left_code, left_label, "", right_code, right_label, ""],
            fonts=[bold_font, small_font, body_font, bold_font, small_font, body_font],
            aligns=["center", "left", "center", "center", "left", "center"],
            max_lines=2,
        )
        checkbox(margin_x + code_w + desc_w + 58, row_top + 42, left_code in selected_groups)
        if right_code:
            checkbox(margin_x + code_w + desc_w + please_w + code_w + desc_w + 58, row_top + 42, right_code in selected_groups)

    reason_h = 190
    y += 26
    ensure_space(reason_h + 40)
    cell(margin_x, y, label_w + 120, reason_h, "Reason for Amendment for Old User:", bold_font, max_lines=3)
    cell(margin_x + label_w + 120, y, full_w - label_w - 120, reason_h, value("amendment_reason"), body_font, max_lines=3)
    y += reason_h + 42

    ensure_space(142 * 5)
    approval_col = int(full_w / 3)
    approval_widths = [approval_col, approval_col, full_w - approval_col * 2]
    add_table_row(approval_widths, 132, ["User Requested By", "Recommended By", "Approved By"], fills=[light] * 3, fonts=[bold_font] * 3, aligns=["center"] * 3)
    add_table_row(approval_widths, 142, [f"Name: {value('requested_by_name')}", f"Name: {value('recommended_by_name')}", f"Name: {value('approved_by_name')}"], fonts=[body_font] * 3)
    add_table_row(approval_widths, 142, [f"Designation: {value('requested_by_designation')}", f"Designation: {value('recommended_by_designation')}", f"Designation: {value('approved_by_designation')}"], fonts=[body_font] * 3)
    add_table_row(approval_widths, 142, [f"Date: {value('requested_by_date')}", f"Date: {value('recommended_by_date')}", f"Date: {value('approved_by_date')}"], fonts=[body_font] * 3)

    sig_top = y
    sig_h = 260
    ensure_space(sig_h + 48)
    sig_top = y
    left = margin_x
    for width in approval_widths:
        cell(left, sig_top, width, sig_h, "Digital Signature:", bold_font)
        left += width
    if _cbs_recommendation_signature_allowed(remote_access_approval):
        rec_left = margin_x + approval_widths[0]
        paste_signature(remote_access_approval.recommended_by, (rec_left + 12, sig_top + 28, rec_left + approval_widths[1] - 12, sig_top + sig_h - 8))
    if _cbs_approval_signature_allowed(remote_access_approval):
        app_left = margin_x + approval_widths[0] + approval_widths[1]
        paste_signature(remote_access_approval.decided_by, (app_left + 12, sig_top + 28, app_left + approval_widths[2] - 12, sig_top + sig_h - 8))
    y += sig_h + 36

    ensure_space(340)
    draw.text((margin_x, y), "3. ENDORSEMENT BY USER:", fill=ink, font=section_font)
    draw.text((margin_x + 1, y), "3. ENDORSEMENT BY USER:", fill=ink, font=section_font)
    y += 74
    endorsement = (
        "By signing below I acknowledge that I am the authorized user of the Pumori System and possess the appropriate "
        "power to login with aforementioned user id. I will keep my user id and password as equal to my signature. "
        "The information provided herein is correct and true in my knowledge."
    )
    for line_text in wrap_lines(endorsement, small_font, full_w):
        draw.text((margin_x, y), line_text, fill=ink, font=small_font)
        draw.text((margin_x + 1, y), line_text, fill=ink, font=small_font)
        y += 66
    y += 58
    draw.text((margin_x, y), "User Signature with Date: .........................................", fill=ink, font=body_font)
    draw.text((margin_x + 1, y), "User Signature with Date: .........................................", fill=ink, font=body_font)

    output = BytesIO()
    pages[0].save(output, format="PDF", resolution=300.0, save_all=True, append_images=pages[1:])
    response = HttpResponse(output.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _cbs_user_group_rows(selected_groups=None, request_type=None):
    selected_groups = set(selected_groups or [])
    choices = _cbs_access_group_choices(request_type)
    midpoint = (len(choices) + 1) // 2
    left_groups = choices[:midpoint]
    right_groups = choices[midpoint:]
    rows = []
    for index in range(midpoint):
        left_code, left_label = left_groups[index]
        right_code, right_label = right_groups[index] if index < len(right_groups) else ("", "")
        rows.append(
            {
                "left_code": left_code,
                "left_label": left_label,
                "left_checked": left_code in selected_groups,
                "right_code": right_code,
                "right_label": right_label,
                "right_checked": right_code in selected_groups,
            }
        )
    return rows


@login_required
def cbs_access_request_template_download(request):
    office_type = "branch" if _clean_query_value(request.GET.get("office")) == "branch" else "head_office"
    return _cbs_access_template_response({"request_type": _cbs_access_request_type_for_office(office_type)})


def _cbs_access_data_from_ticket(ticket):
    request_type = getattr(ticket, "request_type", "") or "cbs_access_ho"
    data = {
        "user_groups": [],
        "request_type": request_type,
        "request_id": getattr(ticket, "ticket_id", "") or "",
    }
    group_codes = {code for code, _label in _cbs_access_group_choices(request_type)}
    approval_section = ""
    for raw_line in (ticket.description or "").splitlines():
        line_text = raw_line.strip()
        if line_text in {"User Requested By", "Recommended By", "Second Recommended By", "Approved By"}:
            approval_section = line_text
            continue
        if not line_text or ":" not in line_text:
            continue
        raw_key, value = line_text.split(":", 1)
        key = raw_key.strip().casefold()
        value = value.strip()
        field_map = {
            "name": "name",
            "designation": "designation",
            "department": "department",
            "employee id": "employee_id",
            "access user signature user id": "access_user_id",
            "access user signature name": "access_user_signature_name",
            "access user signed at": "access_user_signed_at",
            "requested signature user id": "requested_signature_user_id",
            "requested signature name": "requested_signature_name",
            "requested signature signed at": "requested_signature_signed_at",
            "old user id": "old_user_id",
            "reason for amendment for old user": "amendment_reason",
            "request type": "request_type",
            "request id": "request_id",
            "recommended by user": "recommender",
            "second recommended by user": "second_recommender",
            "approved by user": "approver",
        }
        if key == "type of user":
            data["user_type"] = "new" if value.casefold() == "new user" else "amendment"
        elif approval_section and key in {"name", "designation", "date"}:
            section_prefix = {
                "User Requested By": "requested_by",
                "Recommended By": "recommended_by",
                "Second Recommended By": "branch_second_recommended_by",
                "Approved By": "approved_by",
            }[approval_section]
            data[f"{section_prefix}_{key}"] = "" if value == "-" else value
        elif key in field_map:
            data[field_map[key]] = "" if value == "-" else value
        elif " - " in raw_key and value.casefold() == "yes":
            code = raw_key.split(" - ", 1)[0].strip()
            if code in group_codes:
                data["user_groups"].append(code)
    if not data.get("requested_signature_user_id") and getattr(ticket, "created_by_id", None):
        data["requested_signature_user_id"] = str(ticket.created_by_id)
    if not data.get("requested_signature_signed_at"):
        requested_date = data.get("requested_by_date") or ""
        if requested_date:
            data["requested_signature_signed_at"] = requested_date
        elif getattr(ticket, "created_at", None):
            data["requested_signature_signed_at"] = timezone.localtime(ticket.created_at).strftime("%m/%d/%Y")
    if not data.get("access_user_signed_at"):
        data["access_user_signed_at"] = data.get("requested_signature_signed_at", "")
    return data


def _parse_cbs_form_date(value):
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except (TypeError, ValueError):
            continue
    return None


def _cbs_access_form_initial_from_ticket(ticket):
    data = _cbs_access_data_from_ticket(ticket)
    initial = {
        "subject": "CBS Access Request",
        "name": data.get("name", ""),
        "designation": data.get("designation", ""),
        "department": data.get("department", ""),
        "employee_id": data.get("employee_id", ""),
        "access_user": data.get("access_user_id") or "",
        "user_type": data.get("user_type") or "new",
        "old_user_id": data.get("old_user_id", ""),
        "user_groups": data.get("user_groups") or [],
        "amendment_reason": data.get("amendment_reason", ""),
        "requested_by_name": data.get("requested_by_name", ""),
        "requested_by_designation": data.get("requested_by_designation", ""),
        "requested_by_date": _parse_cbs_form_date(data.get("requested_by_date")),
        "recommended_by_name": "",
        "recommended_by_designation": "",
        "recommended_by_date": None,
        "branch_second_recommended_by_name": data.get("branch_second_recommended_by_name", ""),
        "branch_second_recommended_by_designation": data.get("branch_second_recommended_by_designation", ""),
        "branch_second_recommended_by_date": _parse_cbs_form_date(data.get("branch_second_recommended_by_date")),
        "approved_by_name": "",
        "approved_by_designation": "",
        "approved_by_date": None,
        "endorsement": True,
    }
    remote_access_approval = _get_remote_access_approval(ticket)
    if remote_access_approval is not None:
        if getattr(remote_access_approval, "recommender_id", None):
            initial["recommender"] = remote_access_approval.recommender_id
        if getattr(remote_access_approval, "second_recommender_id", None):
            initial["second_recommender"] = remote_access_approval.second_recommender_id
        if getattr(remote_access_approval, "approver_id", None):
            initial["approver"] = remote_access_approval.approver_id
    return initial


def _reset_cbs_access_approval_for_resubmission(remote_access_approval, recommender, second_recommender, approver):
    for field_name in (
        "recommended_signature_snapshot",
        "second_recommended_signature_snapshot",
        "approved_signature_snapshot",
    ):
        snapshot = getattr(remote_access_approval, field_name, None)
        if snapshot:
            try:
                snapshot.delete(save=False)
            except Exception:
                pass
            setattr(remote_access_approval, field_name, None)

    remote_access_approval.recommender = recommender
    remote_access_approval.second_recommender = second_recommender
    remote_access_approval.approver = approver
    remote_access_approval.status = RemoteAccessApproval.initial_status_for(recommender, second_recommender)
    remote_access_approval.recommendation_note = ""
    remote_access_approval.recommended_by = None
    remote_access_approval.recommended_at = None
    remote_access_approval.second_recommendation_note = ""
    remote_access_approval.second_recommended_by = None
    remote_access_approval.second_recommended_at = None
    remote_access_approval.decision_note = ""
    remote_access_approval.decided_by = None
    remote_access_approval.decided_at = None


def _cbs_signature_view_url(ticket_id, role, user):
    if not getattr(user, "signature_image", None):
        return ""
    return reverse("cbs_access_request_signature_view", args=[ticket_id, role])


def _cbs_snapshot_signature_view_url(ticket_id, role, snapshot):
    if not snapshot:
        return ""
    return reverse("cbs_access_request_signature_view", args=[ticket_id, role])


def _cbs_access_data_with_approval(ticket, remote_access_approval):
    data = _cbs_access_data_from_ticket(ticket)
    if remote_access_approval is not None:
        if getattr(remote_access_approval, "recommender_id", None):
            data["recommender"] = _cbs_display_user(remote_access_approval.recommender) or data.get("recommender", "")
        if getattr(remote_access_approval, "second_recommender_id", None):
            data["second_recommender"] = _cbs_display_user(remote_access_approval.second_recommender) or data.get("second_recommender", "")
        if getattr(remote_access_approval, "approver_id", None):
            data["approver"] = _cbs_display_user(remote_access_approval.approver) or data.get("approver", "")
        if getattr(remote_access_approval, "recommended_by_id", None):
            data["recommended_by_name"] = _cbs_display_user(remote_access_approval.recommended_by) or data.get("recommended_by_name", "")
            data["recommended_by_designation"] = _cbs_user_position(remote_access_approval.recommended_by) or data.get("recommended_by_designation", "")
            data["recommended_by_date"] = (
                timezone.localtime(remote_access_approval.recommended_at).strftime("%m/%d/%Y")
                if remote_access_approval.recommended_at
                else data.get("recommended_by_date", "")
            )
        if getattr(remote_access_approval, "second_recommended_by_id", None):
            data["branch_second_recommended_by_name"] = _cbs_display_user(remote_access_approval.second_recommended_by) or data.get("branch_second_recommended_by_name", "")
            data["branch_second_recommended_by_designation"] = _cbs_user_position(remote_access_approval.second_recommended_by) or data.get("branch_second_recommended_by_designation", "")
            data["branch_second_recommended_by_date"] = (
                timezone.localtime(remote_access_approval.second_recommended_at).strftime("%m/%d/%Y")
                if remote_access_approval.second_recommended_at
                else data.get("branch_second_recommended_by_date", "")
            )
        if getattr(remote_access_approval, "decided_by_id", None):
            data["approved_by_name"] = _cbs_display_user(remote_access_approval.decided_by) or data.get("approved_by_name", "")
            data["approved_by_designation"] = _cbs_user_position(remote_access_approval.decided_by) or data.get("approved_by_designation", "")
            data["approved_by_date"] = (
                timezone.localtime(remote_access_approval.decided_at).strftime("%m/%d/%Y")
                if remote_access_approval.decided_at
                else data.get("approved_by_date", "")
            )
    return data


def _refresh_cbs_access_ticket_description(ticket, remote_access_approval):
    if _approval_request_kind(ticket) != "CBS Access":
        return
    refreshed_description = _build_cbs_access_request_description(
        _cbs_access_data_with_approval(ticket, remote_access_approval)
    )
    if ticket.description != refreshed_description:
        ticket.description = refreshed_description
        ticket.save(update_fields=["description", "updated_at"])


def _cbs_access_detail_context(ticket, remote_access_approval):
    data = _cbs_access_data_with_approval(ticket, remote_access_approval)

    recommendation_signature_allowed = _cbs_recommendation_signature_allowed(remote_access_approval)
    second_recommendation_signature_allowed = _cbs_second_recommendation_signature_allowed(remote_access_approval)
    approval_signature_allowed = _cbs_approval_signature_allowed(remote_access_approval)
    recommender_user = getattr(remote_access_approval, "recommended_by", None) if recommendation_signature_allowed else None
    second_recommender_user = getattr(remote_access_approval, "second_recommended_by", None) if second_recommendation_signature_allowed else None
    approver_user = getattr(remote_access_approval, "decided_by", None) if approval_signature_allowed else None
    acknowledgement_user = _cbs_access_acknowledgement_user(data)
    requested_signature_user = _cbs_requested_signature_user(data)
    return {
        "cbs_access_data": data,
        "cbs_user_group_rows": _cbs_user_group_rows(data.get("user_groups"), request_type=ticket.request_type),
        "cbs_office_type": _cbs_access_office_type_from_request_type(ticket.request_type),
        "cbs_office_label": _cbs_access_office_label(ticket.request_type),
        "cbs_recommended_signed": recommendation_signature_allowed,
        "cbs_second_recommended_signed": second_recommendation_signature_allowed,
        "cbs_approved_signed": approval_signature_allowed,
        "cbs_requested_signature_url": (
            _cbs_snapshot_signature_view_url(ticket.id, "requested", _cbs_access_snapshot_field(remote_access_approval, "requested_signature_snapshot"))
            or _cbs_signature_view_url(ticket.id, "requested", requested_signature_user)
        ),
        "cbs_recommender_signature_url": (
            _cbs_snapshot_signature_view_url(ticket.id, "recommended", _cbs_access_snapshot_field(remote_access_approval, "recommended_signature_snapshot"))
            if recommendation_signature_allowed
            else ""
        ) or (
            _cbs_signature_view_url(ticket.id, "recommended", recommender_user)
            if recommendation_signature_allowed
            else ""
        ),
        "cbs_second_recommender_signature_url": (
            _cbs_snapshot_signature_view_url(ticket.id, "second-recommended", _cbs_access_snapshot_field(remote_access_approval, "second_recommended_signature_snapshot"))
            if second_recommendation_signature_allowed
            else ""
        ) or (
            _cbs_signature_view_url(ticket.id, "second-recommended", second_recommender_user)
            if second_recommendation_signature_allowed
            else ""
        ),
        "cbs_approver_signature_url": (
            _cbs_snapshot_signature_view_url(ticket.id, "approved", _cbs_access_snapshot_field(remote_access_approval, "approved_signature_snapshot"))
            if approval_signature_allowed
            else ""
        ) or (
            _cbs_signature_view_url(ticket.id, "approved", approver_user)
            if approval_signature_allowed
            else ""
        ),
        "cbs_access_user_signature_url": (
            _cbs_snapshot_signature_view_url(ticket.id, "access-user", _cbs_access_snapshot_field(remote_access_approval, "access_user_signature_snapshot"))
            or _cbs_signature_view_url(ticket.id, "access-user", acknowledgement_user)
        ),
    }


@login_required
def cbs_access_request_download(request, ticket_id):
    ticket = get_object_or_404(
        Ticket.objects.select_related(
            "created_by",
            "remote_access_approval",
            "remote_access_approval__recommended_by",
            "remote_access_approval__second_recommended_by",
            "remote_access_approval__decided_by",
        ),
        id=ticket_id,
    )
    if not _is_ticket_participant(request.user, ticket):
        messages.error(request, "You do not have access to this request.")
        return redirect("ticket_list")
    if _approval_request_kind(ticket) != "CBS Access":
        messages.error(request, "This ticket is not a CBS access request.")
        return redirect("ticket_detail", ticket_id=ticket.id)
    remote_access_approval = _get_remote_access_approval(ticket)
    output_format = _clean_query_value(request.GET.get("format")) or "pdf"
    if output_format not in {"pdf", "docx", "doc"}:
        output_format = "pdf"
    if (
        remote_access_approval is not None
        and remote_access_approval.status == RemoteAccessApproval.STATUS_APPROVED
    ):
        output_format = "pdf"
    return _cbs_access_template_response(
        _cbs_access_data_from_ticket(ticket),
        filename=f"cbs-access-request-{ticket.ticket_id}.doc",
        remote_access_approval=remote_access_approval,
        output_format="docx" if output_format == "doc" else output_format,
    )


@login_required
def cbs_access_request_signature_view(request, ticket_id, role):
    ticket = get_object_or_404(
        Ticket.objects.select_related(
            "remote_access_approval",
            "remote_access_approval__recommended_by",
            "remote_access_approval__second_recommended_by",
            "remote_access_approval__decided_by",
        ),
        id=ticket_id,
    )
    if not _is_ticket_participant(request.user, ticket):
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)
    if _approval_request_kind(ticket) != "CBS Access":
        return JsonResponse({"ok": False, "error": "File not found"}, status=404)

    remote_access_approval = _get_remote_access_approval(ticket)
    signature_user = None
    signature_field = None
    if role == "requested":
        signature_field = _cbs_access_snapshot_field(remote_access_approval, "requested_signature_snapshot")
    elif role == "recommended" and _cbs_recommendation_signature_allowed(remote_access_approval):
        signature_field = _cbs_access_snapshot_field(remote_access_approval, "recommended_signature_snapshot")
    elif role == "second-recommended" and _cbs_second_recommendation_signature_allowed(remote_access_approval):
        signature_field = _cbs_access_snapshot_field(remote_access_approval, "second_recommended_signature_snapshot")
    elif role == "approved" and _cbs_approval_signature_allowed(remote_access_approval):
        signature_field = _cbs_access_snapshot_field(remote_access_approval, "approved_signature_snapshot")
    elif role == "access-user":
        signature_field = _cbs_access_snapshot_field(remote_access_approval, "access_user_signature_snapshot")

    if role == "recommended" and _cbs_recommendation_signature_allowed(remote_access_approval):
        signature_user = remote_access_approval.recommended_by
    elif role == "second-recommended" and _cbs_second_recommendation_signature_allowed(remote_access_approval):
        signature_user = remote_access_approval.second_recommended_by
    elif role == "approved" and _cbs_approval_signature_allowed(remote_access_approval):
        signature_user = remote_access_approval.decided_by
    elif role == "requested":
        signature_user = _cbs_requested_signature_user(_cbs_access_data_from_ticket(ticket))
    elif role == "access-user":
        signature_user = _cbs_access_acknowledgement_user(_cbs_access_data_from_ticket(ticket))

    if not signature_field:
        signature_field = getattr(signature_user, "signature_image", None)
    if not signature_field:
        return JsonResponse({"ok": False, "error": "File not found"}, status=404)

    try:
        image_file = signature_field.open("rb")
    except Exception:
        return JsonResponse({"ok": False, "error": "File not found"}, status=404)

    filename = os.path.basename(signature_field.name or "signature").replace('"', "")
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    response = FileResponse(image_file, content_type=content_type)
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


@login_required
@require_POST
def cbs_access_request_send_document(request, ticket_id):
    ticket = get_object_or_404(
        Ticket.objects.select_related(
            "created_by",
            "remote_access_approval",
            "remote_access_approval__recommended_by",
            "remote_access_approval__decided_by",
        ),
        id=ticket_id,
    )
    if not _is_ticket_participant(request.user, ticket):
        messages.error(request, "You do not have access to this request.")
        return redirect("ticket_list")
    remote_access_approval = _get_remote_access_approval(ticket)
    if _approval_request_kind(ticket) != "CBS Access":
        messages.error(request, "This ticket is not a CBS access request.")
        return redirect("ticket_detail", ticket_id=ticket.id)
    if remote_access_approval is None or remote_access_approval.status != RemoteAccessApproval.STATUS_APPROVED:
        messages.error(request, "The approved CBS access document can be sent only after final approval.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    recipient_value = (request.POST.get("document_recipient_emails") or "").strip()
    recipients = parse_email_list(recipient_value)
    if not recipients:
        messages.error(request, "Enter at least one recipient email address.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    invalid_emails = []
    for email in recipients:
        try:
            validate_email(email)
        except ValidationError:
            invalid_emails.append(email)
    if invalid_emails:
        messages.error(request, "Enter valid recipient email addresses only: " + ", ".join(invalid_emails))
        return redirect("ticket_detail", ticket_id=ticket.id)

    sender_message = (request.POST.get("document_message") or "").strip()
    try:
        attachments = _build_cbs_access_email_attachments(ticket, remote_access_approval)
    except ValueError:
        messages.error(request, "The approved CBS access image could not be prepared.")
        return redirect("ticket_detail", ticket_id=ticket.id)
    subject = f"Approved CBS Access Document: {ticket.ticket_id}"
    body = (
        "Dear Team,\n\n"
        f"The approved CBS access request document for ticket {ticket.ticket_id} is attached.\n\n"
        f"Request Subject: {ticket.subject}\n"
        f"Sent By: {_format_user_contact(request.user)}\n"
    )
    if sender_message:
        body += f"\nMessage:\n{sender_message}\n"
    body += f"\nOpen Ticket:\n{_ticket_detail_url(request, ticket)}\n\nThank you.\n"
    try:
        _send_email_message(subject, body, recipients, email_attachments=attachments)
    except Exception:
        messages.error(request, "The approved CBS access document could not be sent.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    messages.success(request, "Approved CBS access document sent to: " + ", ".join(recipients))
    return redirect("ticket_detail", ticket_id=ticket.id)


@login_required
@require_POST
def cbs_access_request_assign_after_approval(request, ticket_id):
    ticket = get_object_or_404(
        Ticket.objects.select_related(
            "created_by",
            "assigned_to",
            "remote_access_approval",
            "remote_access_approval__recommended_by",
            "remote_access_approval__decided_by",
        ),
        id=ticket_id,
    )
    remote_access_approval = _get_remote_access_approval(ticket)
    if _approval_request_kind(ticket) != "CBS Access":
        messages.error(request, "This ticket is not a CBS access request.")
        return redirect("ticket_detail", ticket_id=ticket.id)
    if ticket.created_by_id != request.user.id:
        messages.error(request, "Only the requester can assign this approved CBS access request.")
        return redirect("ticket_detail", ticket_id=ticket.id)
    if remote_access_approval is None or remote_access_approval.status != RemoteAccessApproval.STATUS_APPROVED:
        messages.error(request, "CBS access can be assigned only after final approval.")
        return redirect("ticket_detail", ticket_id=ticket.id)
    if ticket.status in {"resolved", "closed", "cancelled_duplicate"}:
        messages.error(request, "This CBS access request is already solved and cannot be reassigned by requester.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    assigned_to_id = (request.POST.get("cbs_assigned_to") or "").strip()
    department = (request.POST.get("cbs_department") or "").strip()
    if not assigned_to_id.isdigit():
        messages.error(request, "Select the concerned user to assign this CBS access request.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    assignee = CustomUser.objects.filter(id=int(assigned_to_id), is_active=True).first()
    if assignee is None:
        messages.error(request, "Select an active user to assign this CBS access request.")
        return redirect("ticket_detail", ticket_id=ticket.id)
    if assignee.id == request.user.id:
        messages.error(request, "You cannot assign this CBS access request to yourself.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    cc_user_ids = [
        value
        for value in request.POST.getlist("cbs_cc_users")
        if (value or "").strip().isdigit()
    ]
    cc_users = CustomUser.objects.filter(id__in=[int(value) for value in cc_user_ids], is_active=True)
    cc_emails = [
        (user.email or "").strip()
        for user in cc_users
        if (user.email or "").strip()
    ]
    typed_cc_value = (request.POST.get("cbs_cc_emails") or "").strip()
    typed_cc_emails = parse_email_list(typed_cc_value)
    invalid_cc_emails = []
    for email in typed_cc_emails:
        try:
            validate_email(email)
        except ValidationError:
            invalid_cc_emails.append(email)
    if invalid_cc_emails:
        messages.error(request, "Enter valid CC email addresses only: " + ", ".join(invalid_cc_emails))
        return redirect("ticket_detail", ticket_id=ticket.id)
    cc_emails.extend(typed_cc_emails)

    previous_assigned_to_id = ticket.assigned_to_id
    ticket.assigned_to = assignee
    ticket._assignment_actor_id = request.user.id
    if department:
        ticket.department = department
    if ticket.status in {"new", "acknowledged"}:
        ticket.status = "in_progress"
    ticket.save(update_fields=["assigned_to", "department", "status", "updated_at"])

    if previous_assigned_to_id != assignee.id:
        _notify_user(
            assignee.id,
            {
                "kind": "ticket_assigned",
                "level": "info",
                "title": "Approved CBS access assigned",
                "message": f"{ticket.ticket_id}: {ticket.subject}",
                "url": reverse("ticket_detail", args=[ticket.id]),
                "ticket_id": ticket.id,
                "ticket_code": ticket.ticket_id,
                "assigned_by": request.user.get_username(),
            },
        )
        _send_assignment_email(request, ticket, request.user, "CBS access request assigned", cc_list=cc_emails)

    messages.success(
        request,
        f"Approved CBS access request assigned to {assignee.get_full_name() or assignee.username}. The signed document was attached to the assignment email.",
    )
    return redirect("ticket_detail", ticket_id=ticket.id)


@login_required
def cbs_access_request(request):
    return _cbs_access_request_view(request, office_type="head_office")


@login_required
def cbs_access_branch_request(request):
    return _cbs_access_request_view(request, office_type="branch")


def _cbs_access_request_view(request, office_type="head_office"):
    request_type = _cbs_access_request_type_for_office(office_type)
    submission_token = _new_submission_token()
    if request.method == "POST":
        submission_token = _clean_submission_token(request.POST.get("submission_token")) or _new_submission_token()
        existing_ticket = _ticket_for_submission_token(submission_token)
        if existing_ticket is not None:
            messages.info(request, "This CBS access request was already submitted. Opening the existing request instead.")
            return redirect("ticket_detail", ticket_id=existing_ticket.id)

        form = CBSAccessRequestForm(request.POST, request_user=request.user, office_type=office_type)
        if form.is_valid():
            form.cleaned_data["request_type"] = request_type
            if request.POST.get("action") in {"download", "download_pdf"}:
                form.cleaned_data["requested_signature_user"] = request.user
                form.cleaned_data["requested_signature_signed_at"] = timezone.localtime(timezone.now()).strftime("%m/%d/%Y")
                employee_id = get_valid_filename(form.cleaned_data.get("employee_id") or "filled")
                return _cbs_access_template_response(
                    form.cleaned_data,
                    filename=f"cbs-access-request-{employee_id}.doc",
                    output_format="pdf" if request.POST.get("action") == "download_pdf" else "docx",
                )
            recommender = form.cleaned_data["recommender"]
            second_recommender = form.cleaned_data.get("second_recommender") if office_type == "branch" else None
            approver = form.cleaned_data["approver"]
            form.cleaned_data["second_recommender"] = second_recommender
            form.cleaned_data["requested_signature_user"] = request.user
            form.cleaned_data["requested_signature_signed_at"] = timezone.localtime(timezone.now()).strftime("%m/%d/%Y")
            request_details = _build_cbs_access_request_description(form.cleaned_data)
            try:
                with transaction.atomic():
                    ticket = Ticket.objects.create(
                        created_by=request.user,
                        subject=form.cleaned_data["subject"],
                        request_type=request_type,
                        department=(form.cleaned_data.get("department") or "").strip(),
                        branch=(getattr(request.user, "branch", "") or "").strip(),
                        notify_email=getattr(settings, "IT_SUPPORT_EMAIL", ""),
                        description=request_details,
                        impact="single_user",
                        urgency="medium",
                        priority=Ticket.calculate_priority("single_user", "medium"),
                        submission_token=submission_token or None,
                    )
                    remote_access_approval = RemoteAccessApproval.objects.create(
                        ticket=ticket,
                        recommender=recommender,
                        second_recommender=second_recommender,
                        approver=approver,
                        status=RemoteAccessApproval.initial_status_for(recommender, second_recommender),
                    )
                    remote_access_approval.copy_signature_snapshot("requested_signature_snapshot", request.user, save=False)
                    remote_access_approval.copy_signature_snapshot(
                        "access_user_signature_snapshot",
                        form.cleaned_data.get("access_user"),
                        save=False,
                    )
                    remote_access_approval.save(
                        update_fields=["requested_signature_snapshot", "access_user_signature_snapshot"]
                    )
            except IntegrityError:
                existing_ticket = _ticket_for_submission_token(submission_token)
                if existing_ticket is not None:
                    messages.info(
                        request,
                        "This CBS access request was already submitted. Opening the existing request instead.",
                    )
                    return redirect("ticket_detail", ticket_id=existing_ticket.id)
                raise

            reviewer, stage_meta = _notify_remote_access_reviewer(request, ticket, remote_access_approval)
            reviewer_email = (getattr(reviewer, "email", "") or "").strip()
            if reviewer_email:
                mail_subject = f"{stage_meta['email_subject']}: {ticket.ticket_id}"
                mail_body = _build_remote_access_request_email_body(request, ticket, remote_access_approval)
                try:
                    _send_email_message(mail_subject, mail_body, [reviewer_email])
                except Exception:
                    messages.warning(
                        request,
                        f"CBS access request was created, but the {stage_meta['stage_label_lower']} email could not be sent.",
                    )
            else:
                messages.warning(
                    request,
                    f"CBS access request was created, but the selected {stage_meta['stage_label_lower']} user has no email address.",
                )
            messages.success(request, "CBS access request submitted successfully!")
            return redirect("ticket_detail", ticket_id=ticket.id)
    else:
        form = CBSAccessRequestForm(request_user=request.user, office_type=office_type)

    return render(
        request,
        "tickets/cbs_access_request.html",
        {
            "form": form,
            "submission_token": submission_token,
            "user_group_rows": _cbs_user_group_rows(
                form.data.getlist("user_groups") if form.is_bound else form.initial.get("user_groups"),
                request_type=request_type,
            ),
            "cbs_office_type": office_type,
            "cbs_request_type": request_type,
            "cbs_office_label": _cbs_access_office_label(request_type),
        },
    )


@login_required
def cbs_access_request_correct(request, ticket_id):
    ticket = get_object_or_404(
        Ticket.objects.select_related(
            "created_by",
            "remote_access_approval",
            "remote_access_approval__recommender",
            "remote_access_approval__approver",
            "remote_access_approval__recommended_by",
            "remote_access_approval__decided_by",
        ),
        id=ticket_id,
    )
    remote_access_approval = _get_remote_access_approval(ticket)
    if _approval_request_kind(ticket) != "CBS Access" or remote_access_approval is None:
        messages.error(request, "This ticket is not a CBS access request.")
        return redirect("ticket_detail", ticket_id=ticket.id)
    if ticket.created_by_id != request.user.id:
        messages.error(request, "Only the requester can correct and resubmit this CBS access request.")
        return redirect("ticket_detail", ticket_id=ticket.id)
    if remote_access_approval.status != RemoteAccessApproval.STATUS_REJECTED:
        messages.error(request, "Only rejected CBS access requests can be corrected and resubmitted.")
        return redirect("ticket_detail", ticket_id=ticket.id)
    office_type = _cbs_access_office_type_from_request_type(ticket.request_type)

    if request.method == "POST":
        form = CBSAccessRequestForm(request.POST, request_user=request.user, office_type=office_type)
        if form.is_valid():
            form.cleaned_data["request_type"] = ticket.request_type
            if request.POST.get("action") in {"download", "download_pdf"}:
                form.cleaned_data["requested_signature_user"] = request.user
                form.cleaned_data["requested_signature_signed_at"] = timezone.localtime(timezone.now()).strftime("%m/%d/%Y")
                employee_id = get_valid_filename(form.cleaned_data.get("employee_id") or "corrected")
                return _cbs_access_template_response(
                    form.cleaned_data,
                    filename=f"cbs-access-request-{employee_id}.doc",
                    output_format="pdf" if request.POST.get("action") == "download_pdf" else "docx",
                )

            recommender = form.cleaned_data["recommender"]
            second_recommender = form.cleaned_data.get("second_recommender") if office_type == "branch" else None
            approver = form.cleaned_data["approver"]
            form.cleaned_data["second_recommender"] = second_recommender
            form.cleaned_data["requested_signature_user"] = request.user
            form.cleaned_data["requested_signature_signed_at"] = timezone.localtime(timezone.now()).strftime("%m/%d/%Y")
            request_details = _build_cbs_access_request_description(form.cleaned_data)

            with transaction.atomic():
                ticket.subject = form.cleaned_data["subject"]
                ticket.department = (form.cleaned_data.get("department") or "").strip()
                ticket.description = request_details
                ticket.status = "new"
                ticket.save(update_fields=["subject", "department", "description", "status", "updated_at"])

                _reset_cbs_access_approval_for_resubmission(remote_access_approval, recommender, second_recommender, approver)
                for field_name in ("requested_signature_snapshot", "access_user_signature_snapshot"):
                    snapshot = getattr(remote_access_approval, field_name, None)
                    if snapshot:
                        try:
                            snapshot.delete(save=False)
                        except Exception:
                            pass
                        setattr(remote_access_approval, field_name, None)
                remote_access_approval.copy_signature_snapshot("requested_signature_snapshot", request.user, save=False)
                remote_access_approval.copy_signature_snapshot(
                    "access_user_signature_snapshot",
                    form.cleaned_data.get("access_user"),
                    save=False,
                )
                remote_access_approval.save(
                    update_fields=[
                        "recommender",
                        "second_recommender",
                        "approver",
                        "status",
                        "recommendation_note",
                        "recommended_by",
                        "recommended_at",
                        "second_recommendation_note",
                        "second_recommended_by",
                        "second_recommended_at",
                        "decision_note",
                        "decided_by",
                        "decided_at",
                        "requested_signature_snapshot",
                        "access_user_signature_snapshot",
                        "recommended_signature_snapshot",
                        "second_recommended_signature_snapshot",
                        "approved_signature_snapshot",
                    ]
                )

            reviewer, stage_meta = _notify_remote_access_reviewer(request, ticket, remote_access_approval)
            reviewer_email = (getattr(reviewer, "email", "") or "").strip()
            if reviewer_email:
                mail_subject = f"{stage_meta['email_subject']}: {ticket.ticket_id}"
                mail_body = _build_remote_access_request_email_body(request, ticket, remote_access_approval)
                try:
                    _send_email_message(mail_subject, mail_body, [reviewer_email])
                except Exception:
                    messages.warning(
                        request,
                        f"CBS access request was resubmitted, but the {stage_meta['stage_label_lower']} email could not be sent.",
                    )
            else:
                messages.warning(
                    request,
                    f"CBS access request was resubmitted, but the selected {stage_meta['stage_label_lower']} user has no email address.",
                )
            messages.success(request, "CBS access request corrected and resubmitted for approval.")
            return redirect("ticket_detail", ticket_id=ticket.id)
    else:
        form = CBSAccessRequestForm(request_user=request.user, office_type=office_type, initial=_cbs_access_form_initial_from_ticket(ticket))

    selected_groups = form.data.getlist("user_groups") if form.is_bound else form.initial.get("user_groups")
    return render(
        request,
        "tickets/cbs_access_request.html",
        {
            "form": form,
            "submission_token": _new_submission_token(),
            "user_group_rows": _cbs_user_group_rows(selected_groups, request_type=ticket.request_type),
            "cbs_form_mode": "correct",
            "cbs_form_ticket": ticket,
            "cbs_office_type": office_type,
            "cbs_request_type": ticket.request_type,
            "cbs_office_label": _cbs_access_office_label(ticket.request_type),
        },
    )


@login_required
def remote_access_request(request):
    submission_token = _new_submission_token()
    if request.method == "POST":
        submission_token = _clean_submission_token(request.POST.get("submission_token")) or _new_submission_token()
        existing_ticket = _ticket_for_submission_token(submission_token)
        if existing_ticket is not None:
            messages.info(request, "This remote access request was already submitted. Opening the existing request instead.")
            return redirect("ticket_detail", ticket_id=existing_ticket.id)

        form = RemoteAccessRequestForm(request.POST, request_user=request.user)
        if form.is_valid():
            recommender = form.cleaned_data["recommender"]
            approver = form.cleaned_data["approver"]
            request_details = (form.cleaned_data["details"] or "").strip()
            try:
                with transaction.atomic():
                    ticket = Ticket.objects.create(
                        created_by=request.user,
                        subject=form.cleaned_data["subject"],
                        request_type="access",
                        department="",
                        branch=(getattr(request.user, "branch", "") or "").strip(),
                        notify_email="",
                        description=request_details,
                        impact="single_user",
                        urgency="medium",
                        priority=Ticket.calculate_priority("single_user", "medium"),
                        submission_token=submission_token or None,
                    )
                    remote_access_approval = RemoteAccessApproval.objects.create(
                        ticket=ticket,
                        recommender=recommender,
                        approver=approver,
                        status=RemoteAccessApproval.initial_status_for(recommender),
                    )
            except IntegrityError:
                existing_ticket = _ticket_for_submission_token(submission_token)
                if existing_ticket is not None:
                    messages.info(
                        request,
                        "This remote access request was already submitted. Opening the existing request instead.",
                    )
                    return redirect("ticket_detail", ticket_id=existing_ticket.id)
                raise

            reviewer, stage_meta = _notify_remote_access_reviewer(request, ticket, remote_access_approval)
            reviewer_email = (getattr(reviewer, "email", "") or "").strip()
            if reviewer_email:
                mail_subject = f"{stage_meta['email_subject']}: {ticket.ticket_id}"
                mail_body = _build_remote_access_request_email_body(request, ticket, remote_access_approval)
                try:
                    _send_email_message(mail_subject, mail_body, [reviewer_email])
                except Exception:
                    messages.warning(
                        request,
                        f"Remote access request was created, but the {stage_meta['stage_label_lower']} email could not be sent.",
                    )
            else:
                messages.warning(
                    request,
                    f"Remote access request was created, but the selected {stage_meta['stage_label_lower']} user has no email address.",
                )

            messages.success(request, "Remote access approval request submitted successfully!")
            return redirect("ticket_detail", ticket_id=ticket.id)
    else:
        form = RemoteAccessRequestForm(request_user=request.user)

    return render(
        request,
        "tickets/remote_access_request.html",
        {
            "form": form,
            "submission_token": submission_token,
        },
    )


@login_required
@require_POST
def remote_access_approval_update(request, ticket_id):
    ticket = get_object_or_404(
        Ticket.objects.select_related(
            "created_by",
            "assigned_to",
            "resolved_by",
            "closed_by",
            "remote_access_approval",
            "remote_access_approval__recommender",
            "remote_access_approval__recommended_by",
            "remote_access_approval__second_recommender",
            "remote_access_approval__second_recommended_by",
            "remote_access_approval__approver",
            "remote_access_approval__decided_by",
        ).prefetch_related("incident_report__signoffs__user"),
        id=ticket_id,
    )
    if not _is_ticket_participant(request.user, ticket):
        messages.error(request, "You do not have access to this ticket.")
        return redirect("ticket_list")

    remote_access_approval = _get_remote_access_approval(ticket)
    if remote_access_approval is None:
        messages.error(request, "This ticket does not have a remote access approval request.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    if not _can_decide_remote_access_approval(request.user, remote_access_approval):
        messages.error(request, "You are not allowed to decide this remote access request.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    form = RemoteAccessApprovalDecisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Please choose whether to approve or reject the request.")
        return render(
            request,
            "tickets/ticket_detail.html",
            _build_ticket_detail_context(request, ticket, remote_access_approval_form=form),
        )

    decision = form.cleaned_data["decision"]
    decision_stage = remote_access_approval.current_stage
    request_kind = _approval_request_kind(ticket)
    remote_access_approval.record_decision(
        decision,
        request.user,
        note=form.cleaned_data["decision_note"],
    )
    _refresh_cbs_access_ticket_description(ticket, remote_access_approval)

    actor_name = request.user.get_full_name().strip() or request.user.username
    status_label = remote_access_approval.get_status_display()
    if decision_stage in {"recommendation", "second_recommendation"} and decision == RemoteAccessApproval.STATUS_APPROVED:
        reviewer, stage_meta = _notify_remote_access_reviewer(request, ticket, remote_access_approval)
        reviewer_email = (getattr(reviewer, "email", "") or "").strip()
        if reviewer_email:
            mail_subject = f"{stage_meta['email_subject']}: {ticket.ticket_id}"
            mail_body = _build_remote_access_request_email_body(request, ticket, remote_access_approval)
            try:
                _send_email_message(mail_subject, mail_body, [reviewer_email])
            except Exception:
                messages.warning(
                    request,
                    f"Recommendation was saved, but the {stage_meta['stage_label_lower']} email could not be sent.",
                )
        else:
            messages.warning(
                request,
                f"Recommendation was saved, but the selected {stage_meta['stage_label_lower']} user has no email address.",
            )

        if ticket.created_by_id != request.user.id:
            _notify_user(
                ticket.created_by_id,
                {
                    "kind": "remote_access_approval_update",
                    "level": "warning",
                    "title": f"{request_kind} forwarded for approval",
                    "message": f"{ticket.ticket_id}: Recommended by {actor_name} and sent for approval",
                    "url": reverse("ticket_detail", args=[ticket.id]),
                    "ticket_id": ticket.id,
                    "ticket_code": ticket.ticket_id,
                    "delay": 12000,
                },
            )

        messages.success(request, f"{request_kind} request recommended and forwarded for approval.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    notify_targets = []
    for user_id in [ticket.created_by_id, ticket.assigned_to_id]:
        if user_id and user_id != request.user.id and user_id not in notify_targets:
            notify_targets.append(user_id)
    for user_id in notify_targets:
        _notify_user(
            user_id,
            {
                "kind": "remote_access_approval_update",
                "level": "success" if decision == RemoteAccessApproval.STATUS_APPROVED else "warning",
                "title": f"{request_kind} {status_label.lower()}",
                "message": f"{ticket.ticket_id}: {status_label} by {actor_name}",
                "url": reverse("ticket_detail", args=[ticket.id]),
                "ticket_id": ticket.id,
                "ticket_code": ticket.ticket_id,
                "delay": 12000,
            },
        )

    requester_email = (getattr(ticket.created_by, "email", "") or "").strip()
    if requester_email and ticket.created_by_id != request.user.id:
        mail_subject = f"{request_kind} {status_label}: {ticket.ticket_id}"
        mail_body = _build_remote_access_decision_email_body(request, ticket, remote_access_approval)
        try:
            _send_email_message(mail_subject, mail_body, [requester_email])
        except Exception:
            messages.warning(request, f"{request_kind} request was updated, but requester email could not be sent.")
    elif not requester_email:
        messages.warning(request, f"{request_kind} request was updated, but the requester has no email address.")

    messages.success(request, f"{request_kind} request {status_label.lower()}.")
    return redirect("ticket_detail", ticket_id=ticket.id)


@login_required
def ticket_list(request):
    query = _clean_query_value(request.GET.get("q"))
    status = _clean_query_value(request.GET.get("status"))
    scope = _clean_query_value(request.GET.get("scope"))
    request_type = _clean_query_value(request.GET.get("request_type"))
    date_from = _clean_query_value(request.GET.get("date_from"))
    date_to = _clean_query_value(request.GET.get("date_to"))
    allowed_statuses = {value for value, _label in Ticket.TICKET_STATUS}
    if status not in allowed_statuses:
        status = ""
    request_type_choices = [
        ("", "All ticket types"),
        ("incident", "Incident Reports"),
        ("cbs_access", "CBS Access Requests"),
    ]
    allowed_request_types = {value for value, _label in request_type_choices}
    if request_type not in allowed_request_types:
        request_type = ""
    parsed_date_from = _parse_filter_date(date_from)
    parsed_date_to = _parse_filter_date(date_to)
    if parsed_date_from and parsed_date_to and parsed_date_from > parsed_date_to:
        date_from, date_to = date_to, date_from
    is_support_user = _is_support_user(request.user)
    scope_choices = []
    allowed_scopes = {""}
    if not is_support_user:
        scope_choices = [
            ("", "All visible tickets"),
            ("created_by_me", "Created by me"),
            ("assigned_to_me", "Assigned to me"),
        ]
        allowed_scopes.update(value for value, _label in scope_choices if value)
    if scope not in allowed_scopes:
        scope = ""
    base_queryset = Ticket.objects.select_related("created_by", "assigned_to", "incident_report", "remote_access_approval")
    if is_support_user:
        latest_assigned_to_subquery = TicketAssignmentLog.objects.filter(
            ticket_id=OuterRef("pk"),
            assigned_to__isnull=False,
        ).order_by("-assigned_at", "-id")
        tickets = base_queryset.annotate(
            last_assigned_to_id=Subquery(latest_assigned_to_subquery.values("assigned_to_id")[:1])
        ).filter(
            Q(assigned_to=request.user)
            | Q(status__in={"resolved", "closed"}, last_assigned_to_id=request.user.id)
            | _remote_access_ticket_q(request.user)
            | _incident_report_signer_ticket_q(request.user)
        ).distinct()
    else:
        tickets = base_queryset.filter(
            Q(created_by=request.user)
            | Q(assigned_to=request.user)
            | _department_ticket_q(request.user)
            | _remote_access_ticket_q(request.user)
            | _incident_report_signer_ticket_q(request.user)
        ).distinct()

    if scope == "created_by_me":
        tickets = tickets.filter(created_by=request.user)
    elif scope == "assigned_to_me":
        tickets = tickets.filter(assigned_to=request.user)

    if query:
        tickets = tickets.filter(ticket_id__icontains=query)
    if status:
        tickets = tickets.filter(status=status)
    if request_type == "incident":
        tickets = tickets.filter(request_type="incident")
    elif request_type == "cbs_access":
        tickets = tickets.filter(request_type__in=("cbs_access_ho", "cbs_access_branch"))
    if date_from:
        parsed = _parse_filter_date(date_from)
        if parsed:
            tickets = tickets.filter(created_at__date__gte=parsed)
    if date_to:
        parsed = _parse_filter_date(date_to)
        if parsed:
            tickets = tickets.filter(created_at__date__lte=parsed)
    tickets = _attach_ticket_chat_flags(tickets.order_by("-created_at"), request.user)
    tickets = _attach_ticket_display_assignees(tickets)
    for ticket in tickets:
        remote_access_approval = _get_remote_access_approval(ticket)
        cbs_approved = _apply_ticket_display_status(ticket, remote_access_approval)
        if remote_access_approval is not None and not cbs_approved:
            ticket.is_remote_access_request = True
        else:
            ticket.is_remote_access_request = False
        ticket.can_claim = _can_claim_department_ticket(request.user, ticket)
        ticket.is_department_ticket = _is_department_ticket_member(request.user, ticket)
    return render(
        request,
        'tickets/ticket_list.html',
        {
            'tickets': tickets,
            'query': query,
            'selected_status': status,
            'selected_scope': scope,
            'selected_request_type': request_type,
            'request_type_choices': request_type_choices,
            'scope_choices': scope_choices,
            'show_scope_filters': not is_support_user,
            'status_choices': Ticket.TICKET_STATUS,
            'date_from': date_from,
            'date_to': date_to,
            'has_active_filters': bool(query or status or scope or request_type or date_from or date_to),
        },
    )


@login_required
def tech_docs(request):
    documents = (
        TechnicalDocument.objects.select_related("uploaded_by")
        .prefetch_related("allowed_branches", "allowed_departments")
        .order_by("-created_at")
    )
    if not _is_support_user(request.user):
        documents = documents.filter(_tech_doc_visibility_q(request.user)).distinct()
    return render(
        request,
        "docs/tech_docs.html",
        {"documents": documents, "can_upload": _is_support_user(request.user)},
    )


def _build_incident_response_template_pdf_response(cleaned_data, *, as_attachment=True):
    payload = _incident_response_template_docx_to_pdf_payload(cleaned_data or {})

    if payload is not None:
        output = BytesIO(payload)
        output.seek(0)
        base_name = (
            (cleaned_data.get("incident_title") or "").strip()
            or (cleaned_data.get("incident_id") or "").strip()
            or "incident-response-template"
        )
        filename = f"{get_valid_filename(base_name).replace(' ', '_')}.pdf"
        return FileResponse(
            output,
            as_attachment=as_attachment,
            filename=filename,
            content_type="application/pdf",
        )

    return HttpResponse(
        "Incident report PDF export is unavailable. The server must have LibreOffice/soffice installed so the Word template can be converted to PDF with the same format.",
        status=503,
        content_type="text/plain",
    )

    page_size = (1654, 2339)
    margin_x = 94
    top_margin = 64
    bottom_margin = 86
    gutter = 14
    content_width = page_size[0] - (margin_x * 2)

    colors = {
        "ink": (19, 33, 28),
        "muted": (89, 104, 97),
        "green": (18, 63, 57),
        "green_soft": (238, 246, 242),
        "blue": (0, 102, 178),
        "blue_soft": (235, 245, 255),
        "gold_soft": (255, 244, 214),
        "red_soft": (255, 230, 227),
        "amber": (167, 117, 18),
        "border": (214, 225, 219),
        "card": (248, 251, 249),
        "card_alt": (243, 248, 245),
        "white": (255, 255, 255),
    }

    def load_font(size, bold=False):
        candidates = (
            ["arialbd.ttf", "DejaVuSans-Bold.ttf"]
            if bold
            else ["arial.ttf", "DejaVuSans.ttf"]
        )
        for candidate in candidates:
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    title_font = load_font(34, bold=True)
    report_title_font = load_font(80, bold=True)
    heading_font = load_font(22, bold=True)
    card_label_font = load_font(20, bold=True)
    field_label_font = load_font(22, bold=True)
    body_font = load_font(16)
    field_value_font = load_font(18)
    body_small_font = load_font(13)
    meta_font = load_font(13, bold=True)
    tiny_font = load_font(11)
    signature_badge_font = load_font(14, bold=True)

    pages = []
    page = None
    draw = None
    y = top_margin

    def load_letterhead_logo():
        candidate_paths = [
            os.path.join(settings.BASE_DIR, "static", "images", "logo.png"),
            os.path.join(settings.BASE_DIR, "staticfiles", "images", "logo.png"),
        ]
        for candidate_path in candidate_paths:
            if not os.path.exists(candidate_path):
                continue
            try:
                logo_image = Image.open(candidate_path)
                logo_image = logo_image.convert("RGBA")
                logo_image.thumbnail((360, 88))
                return logo_image
            except Exception:
                continue
        return None

    letterhead_logo = load_letterhead_logo()

    def build_watermark_logo():
        if letterhead_logo is None:
            return None
        watermark = letterhead_logo.copy().convert("RGBA")
        watermark.thumbnail((1280, 760))
        alpha = watermark.getchannel("A")
        alpha = alpha.point(lambda value: int(value * 0.20))
        watermark.putalpha(alpha)
        return watermark

    watermark_logo = build_watermark_logo()

    def line_height(font, extra=6):
        bbox = draw.textbbox((0, 0), "Ag", font=font)
        return max((bbox[3] - bbox[1]) + extra, 18)

    def normalize_value(value):
        if value is None:
            return "-"
        if isinstance(value, bool):
            return "Yes" if value else "No"
        text = str(value).strip()
        return text or "-"

    def wrap_text(value, font, max_width):
        normalized = normalize_value(value)
        if not normalized:
            return ["-"]

        lines = []
        for paragraph in normalized.splitlines():
            paragraph = paragraph.strip()
            if not paragraph:
                lines.append("")
                continue

            words = paragraph.split()
            current_line = ""
            for word in words:
                candidate = word if not current_line else f"{current_line} {word}"
                if draw.textlength(candidate, font=font) <= max_width:
                    current_line = candidate
                else:
                    if current_line:
                        lines.append(current_line)
                    if draw.textlength(word, font=font) <= max_width:
                        current_line = word
                    else:
                        current_line = ""
                        chunk = ""
                        for character in word:
                            candidate_chunk = f"{chunk}{character}"
                            if draw.textlength(candidate_chunk, font=font) <= max_width:
                                chunk = candidate_chunk
                            else:
                                if chunk:
                                    lines.append(chunk)
                                chunk = character
                        current_line = chunk
            if current_line:
                lines.append(current_line)

        return lines or ["-"]

    def ensure_space(required_height):
        nonlocal y
        if y + required_height > page_size[1] - bottom_margin:
            new_page()

    def new_page():
        nonlocal page, draw, y
        page = Image.new("RGB", page_size, colors["white"])
        draw = ImageDraw.Draw(page)
        pages.append(page)
        if watermark_logo is not None:
            watermark_x = (page_size[0] - watermark_logo.width) // 2
            watermark_y = (page_size[1] - watermark_logo.height) // 2
            page.paste(watermark_logo, (watermark_x, watermark_y), watermark_logo)
        draw.rectangle((34, 34, page_size[0] - 34, page_size[1] - 34), outline=colors["border"], width=2)

        header_bottom = top_margin + 132
        draw.rectangle(
            (margin_x, top_margin, page_size[0] - margin_x, header_bottom),
            fill=colors["card"],
            outline=colors["border"],
            width=2,
        )
        draw.rectangle((margin_x, top_margin, page_size[0] - margin_x, top_margin + 10), fill=colors["green"])
        header_content_top = top_margin + 22
        header_content_height = 86
        if letterhead_logo is not None:
            logo_y = header_content_top + max(0, (header_content_height - letterhead_logo.height) // 2)
            page.paste(letterhead_logo, (margin_x + 20, logo_y), letterhead_logo)

        header_title = "INCIDENT RESPONSE REPORT"
        header_text_left = margin_x + 390
        header_text_right = page_size[0] - margin_x - 20
        max_header_title_width = header_text_right - header_text_left
        max_header_title_height = 62
        if letterhead_logo is not None:
            max_header_title_height = min(62, max(44, int(letterhead_logo.height * 0.72)))

        header_title_font = report_title_font
        header_title_bbox = draw.textbbox((0, 0), header_title, font=header_title_font)
        header_title_width = header_title_bbox[2] - header_title_bbox[0]
        header_title_height = header_title_bbox[3] - header_title_bbox[1]
        for font_size in range(80, 39, -2):
            if header_title_width <= max_header_title_width and header_title_height <= max_header_title_height:
                break
            header_title_font = load_font(font_size, bold=True)
            header_title_bbox = draw.textbbox((0, 0), header_title, font=header_title_font)
            header_title_width = header_title_bbox[2] - header_title_bbox[0]
            header_title_height = header_title_bbox[3] - header_title_bbox[1]
        header_text_x = header_text_left + max(0, (header_text_right - header_text_left - header_title_width) // 2)
        left_visual_height = letterhead_logo.height if letterhead_logo is not None else header_content_height
        left_visual_top = header_content_top + max(0, (header_content_height - left_visual_height) // 2)
        left_visual_center_y = left_visual_top + (left_visual_height / 2)
        header_title_y = int(left_visual_center_y - (header_title_height / 2) - 8)
        draw.text(
            (header_text_x - header_title_bbox[0], header_title_y - header_title_bbox[1]),
            header_title,
            font=header_title_font,
            fill=colors["green"],
        )

        footer_y = page_size[1] - 58
        draw.line((margin_x, footer_y, page_size[0] - margin_x, footer_y), fill=colors["border"], width=2)
        draw.text((margin_x, footer_y + 10), "Incident Response Report", font=tiny_font, fill=colors["muted"])
        y = header_bottom + 22

    def draw_pill(text, fill, text_fill, x_right, y_top):
        pill_text = normalize_value(text)
        width = int(draw.textlength(pill_text, font=meta_font)) + 34
        height = 36
        x_left = x_right - width
        draw.rounded_rectangle((x_left, y_top, x_right, y_top + height), radius=18, fill=fill)
        draw.text((x_left + 17, y_top + 9), pill_text, font=meta_font, fill=text_fill)
        return width

    def draw_banner(report_title, report_id, generated_at, severity_value, current_status, regulatory_impact):
        nonlocal y
        banner_height = 104
        ensure_space(banner_height + 18)
        left = margin_x
        right = page_size[0] - margin_x
        top = y
        bottom = top + banner_height
        draw.rectangle(
            (left, top, right, bottom),
            fill=colors["green_soft"],
            outline=colors["border"],
            width=2,
        )
        draw.text((left + 22, top + 18), report_title, font=heading_font, fill=colors["green"])

        subtitle = f"Generated on {generated_at}"
        if report_id and report_id != "-":
            subtitle = f"{subtitle} • {report_id}"
        draw.text((left + 22, top + 52), subtitle, font=body_small_font, fill=colors["muted"])

        severity_key = normalize_value(severity_value).casefold()
        severity_palette = {
            "critical": (colors["red_soft"], (161, 43, 35)),
            "high": (colors["gold_soft"], (146, 93, 5)),
            "medium": ((255, 248, 228), colors["amber"]),
            "low": (colors["blue_soft"], colors["blue"]),
        }
        pill_fill, pill_text_fill = severity_palette.get(severity_key, (colors["card_alt"], colors["green"]))
        pill_right = right - 24
        draw_pill(f"Severity: {normalize_value(severity_value)}", pill_fill, pill_text_fill, pill_right, top + 18)
        draw_pill(f"Status: {normalize_value(current_status)}", colors["card_alt"], colors["green"], pill_right, top + 58)
        reg_text = "NRB Impact: Yes" if regulatory_impact else "NRB Impact: No"
        reg_fill = colors["gold_soft"] if regulatory_impact else colors["card_alt"]
        reg_text_fill = colors["amber"] if regulatory_impact else colors["muted"]
        reg_width = draw_pill(reg_text, reg_fill, reg_text_fill, pill_right - 300, top + 62)
        draw_pill(report_id if report_id and report_id != "-" else "No Incident ID", colors["blue_soft"], colors["blue"], pill_right - reg_width - 18, top + 20)
        y = bottom + 18

    def prepare_card(label, value, width, compact=False):
        body_font_to_use = body_small_font if compact else body_font
        inner_width = width - 32
        lines = wrap_text(value, body_font_to_use, inner_width)
        title_h = line_height(card_label_font, extra=2)
        body_h = len(lines) * line_height(body_font_to_use, extra=4)
        min_height = 86 if compact else 118
        height = max(min_height, 20 + title_h + 14 + body_h + 16)
        return {
            "label": label,
            "lines": lines,
            "height": height,
            "compact": compact,
            "body_font": body_font_to_use,
        }

    def draw_card(x_left, y_top, width, height, card, fill=None):
        fill_color = fill or colors["card"]
        draw.rounded_rectangle(
            (x_left, y_top, x_left + width, y_top + height),
            radius=22,
            fill=fill_color,
            outline=colors["border"],
            width=2,
        )
        draw.rounded_rectangle(
            (x_left, y_top, x_left + width, y_top + 12),
            radius=12,
            fill=colors["green"],
        )
        draw.text((x_left + 16, y_top + 20), card["label"], font=card_label_font, fill=colors["green"])
        text_y = y_top + 52
        for line in card["lines"]:
            draw.text((x_left + 16, text_y), line or " ", font=card["body_font"], fill=colors["ink"])
            text_y += line_height(card["body_font"], extra=4)

    def draw_cards_row(specs):
        nonlocal y
        total_span = sum(spec.get("span", 1) for spec in specs)
        available_width = content_width - (gutter * (len(specs) - 1))
        cards = []
        for spec in specs:
            width = int((available_width * spec.get("span", 1)) / total_span)
            cards.append((spec, width, prepare_card(spec["label"], spec["value"], width, compact=spec.get("compact", False))))
        row_height = max(card["height"] for _spec, _width, card in cards)
        ensure_space(row_height + gutter)

        cursor_x = margin_x
        for index, (spec, width, card) in enumerate(cards):
            if index == len(cards) - 1:
                width = (page_size[0] - margin_x) - cursor_x
            draw_card(cursor_x, y, width, row_height, card, fill=spec.get("fill"))
            cursor_x += width + gutter
        y += row_height + gutter

    def draw_section_header(title, note=""):
        nonlocal y
        height = 46
        ensure_space(height + 14)
        left = margin_x
        right = page_size[0] - margin_x
        draw.rounded_rectangle((left, y, right, y + height), radius=22, fill=colors["green"], outline=colors["green"], width=1)
        draw.text((left + 20, y + 11), title, font=heading_font, fill=colors["white"])
        if note:
            note_width = int(draw.textlength(note, font=tiny_font))
            draw.text((right - note_width - 18, y + 15), note, font=tiny_font, fill=(213, 235, 228))
        y += height + 14

    def load_signature_image(upload, max_size=(380, 150)):
        if not upload:
            return None
        try:
            if hasattr(upload, "open"):
                upload.open("rb")
            if hasattr(upload, "seek"):
                upload.seek(0)
            image = Image.open(upload)
            image = image.convert("RGB")
            image.thumbnail(max_size)
            return image
        except Exception:
            return None

    def draw_signature_card(role_label, person_name, signature_upload):
        nonlocal y
        signature_image = load_signature_image(signature_upload)
        card_height = 198
        ensure_space(card_height + gutter)

        left = margin_x
        right = page_size[0] - margin_x
        top = y
        bottom = top + card_height
        draw.rounded_rectangle((left, top, right, bottom), radius=24, fill=colors["card"], outline=colors["border"], width=2)
        draw.rounded_rectangle((left, top, right, top + 12), radius=12, fill=colors["green"])

        draw.text((left + 20, top + 22), role_label, font=card_label_font, fill=colors["green"])
        person_lines = wrap_text(person_name, body_font, 620)
        text_y = top + 58
        for line in person_lines[:4]:
            draw.text((left + 20, text_y), line, font=body_font, fill=colors["ink"])
            text_y += line_height(body_font, extra=5)

        draw.text((left + 20, bottom - 36), "Digital sign-off", font=tiny_font, fill=colors["muted"])

        sig_box_left = right - 430
        sig_box_top = top + 26
        sig_box_right = right - 20
        sig_box_bottom = bottom - 24
        draw.rounded_rectangle(
            (sig_box_left, sig_box_top, sig_box_right, sig_box_bottom),
            radius=20,
            fill=colors["white"],
            outline=colors["border"],
            width=2,
        )
        if signature_image is not None:
            image_left = sig_box_left + ((sig_box_right - sig_box_left - signature_image.width) // 2)
            image_top = sig_box_top + ((sig_box_bottom - sig_box_top - signature_image.height) // 2)
            page.paste(signature_image, (image_left, image_top))
        else:
            line_y = sig_box_top + 88
            draw.line((sig_box_left + 28, line_y, sig_box_right - 28, line_y), fill=colors["muted"], width=1)
            draw.text((sig_box_left + 28, line_y + 10), "Signature pending", font=body_small_font, fill=colors["muted"])

        y = bottom + gutter

    def format_downtime(value):
        if value in {"", None}:
            return "-"
        try:
            total_minutes = int(value)
        except (TypeError, ValueError):
            return str(value)

        hours, remainder = divmod(total_minutes, 60)
        days, hours = divmod(hours, 24)
        if days > 0:
            return f"{days}d {hours}h {remainder}m"
        if hours > 0:
            return f"{hours}h {remainder}m"
        return f"{remainder}m"

    def format_service(value):
        if not value:
            return "-"
        service_lookup = dict(IncidentReport.SERVICE_CHOICES)
        return service_lookup.get(value, str(value))

    def format_choice(value, choices):
        if not value:
            return "-"
        return dict(choices).get(value, str(value))

    def draw_report_section(title, note=""):
        nonlocal y
        height = 36
        ensure_space(height + 8)
        left = margin_x
        right = page_size[0] - margin_x
        draw.rectangle((left, y, right, y + height), fill=colors["green"])
        draw.text((left + 14, y + 8), title, font=heading_font, fill=colors["white"])
        if note:
            note_width = int(draw.textlength(note, font=tiny_font))
            draw.text((right - note_width - 14, y + 12), note, font=tiny_font, fill=(218, 238, 232))
        y += height + 8

    def measure_value_block(label, value, width):
        inner_width = width - 28
        value_lines = wrap_text(value, field_value_font, inner_width)
        return {
            "label": label,
            "value_lines": value_lines,
            "height": 14
            + line_height(field_label_font, extra=1)
            + 7
            + len(value_lines) * line_height(field_value_font, extra=6)
            + 12,
        }

    def draw_value_cell(x_left, y_top, width, height, block, fill):
        draw.rectangle(
            (x_left, y_top, x_left + width, y_top + height),
            fill=fill,
            outline=colors["border"],
            width=1,
        )
        draw.text((x_left + 14, y_top + 12), block["label"], font=field_label_font, fill=colors["green"])
        text_y = y_top + 48
        for line in block["value_lines"]:
            draw.text((x_left + 14, text_y), line or " ", font=field_value_font, fill=colors["ink"])
            text_y += line_height(field_value_font, extra=6)

    def draw_field_table(title, rows, note="", columns=2):
        nonlocal y
        draw_report_section(title, note)
        col_gap = 0
        col_width = int((content_width - (col_gap * (columns - 1))) / columns)
        row_index = 0
        for row_start in range(0, len(rows), columns):
            row_items = rows[row_start : row_start + columns]
            blocks = [measure_value_block(label, value, col_width) for label, value in row_items]
            row_height = max(70, max(block["height"] for block in blocks))
            ensure_space(row_height + 8)
            cursor_x = margin_x
            fill = colors["white"] if row_index % 2 == 0 else colors["card"]
            for index, block in enumerate(blocks):
                width = col_width if index < columns - 1 else (page_size[0] - margin_x) - cursor_x
                draw_value_cell(cursor_x, y, width, row_height, block, fill)
                cursor_x += width + col_gap
            y += row_height
            row_index += 1
        y += gutter

    def draw_narrative_section(title, items, note=""):
        nonlocal y
        draw_report_section(title, note)
        for label, value in items:
            block = measure_value_block(label, value, content_width)
            block_height = max(86, block["height"])
            ensure_space(block_height + 8)
            draw_value_cell(margin_x, y, content_width, block_height, block, colors["white"])
            y += block_height
        y += gutter

    def format_signed_at(value):
        if not value:
            return ""
        if isinstance(value, str):
            return value
        try:
            return timezone.localtime(value).strftime("%b %d, %Y %H:%M")
        except Exception:
            return str(value)

    def draw_signature_badge(text, x_left, y_top, fill, outline, text_fill):
        badge_width = int(draw.textlength(text, font=signature_badge_font)) + 28
        badge_height = 30
        draw.rectangle(
            (x_left, y_top, x_left + badge_width, y_top + badge_height),
            fill=fill,
            outline=outline,
            width=1,
        )
        draw.text((x_left + 14, y_top + 7), text, font=signature_badge_font, fill=text_fill)

    def draw_signature_block(role_label, person_name, signature_upload, signed_at=None):
        nonlocal y
        signature_image = load_signature_image(signature_upload)
        signed_at_label = format_signed_at(signed_at)
        box_height = 150
        ensure_space(box_height + 8)
        left = margin_x
        right = page_size[0] - margin_x
        top = y
        signature_left = right - 430
        draw.rectangle((left, top, right, top + box_height), fill=colors["white"], outline=colors["border"], width=1)
        draw.text((left + 16, top + 14), role_label, font=card_label_font, fill=colors["green"])
        text_y = top + 44
        for line in wrap_text(person_name, body_font, signature_left - left - 46)[:4]:
            draw.text((left + 16, text_y), line or " ", font=body_font, fill=colors["ink"])
            text_y += line_height(body_font, extra=3)

        sig_box_top = top + 16
        sig_box_bottom = top + box_height - 16
        draw.rectangle(
            (signature_left, sig_box_top, right - 16, sig_box_bottom),
            fill=colors["card"],
            outline=colors["border"],
            width=1,
        )
        if signature_image is not None:
            image_left = signature_left + ((right - 16 - signature_left - signature_image.width) // 2)
            image_top = sig_box_top + max(8, ((sig_box_bottom - sig_box_top - signature_image.height) // 2) - 10)
            page.paste(signature_image, (image_left, image_top))
            draw_signature_badge(
                "SIGNED",
                signature_left + 18,
                sig_box_top + 12,
                colors["green_soft"],
                colors["green"],
                colors["green"],
            )
            if signed_at_label:
                draw.text(
                    (signature_left + 18, sig_box_bottom - 26),
                    f"Signed: {signed_at_label}",
                    font=body_small_font,
                    fill=colors["ink"],
                )
        else:
            line_y = sig_box_top + 70
            draw.line((signature_left + 26, line_y, right - 42, line_y), fill=colors["muted"], width=1)
            draw_signature_badge(
                "PENDING SIGNATURE",
                signature_left + 26,
                line_y + 14,
                colors["gold_soft"],
                colors["amber"],
                colors["amber"],
            )
        y += box_height

    def draw_signature_row(entries):
        nonlocal y
        if not entries:
            return

        max_columns = min(4, len(entries))
        for row_start in range(0, len(entries), max_columns):
            row_entries = entries[row_start : row_start + max_columns]
            columns = len(row_entries)
            col_width = int((content_width - (gutter * (columns - 1))) / columns)
            row_height = 236
            ensure_space(row_height + gutter)
            top = y
            cursor_x = margin_x

            for index, entry in enumerate(row_entries):
                width = col_width if index < columns - 1 else (page_size[0] - margin_x) - cursor_x
                left = cursor_x
                right = left + width
                draw.rectangle((left, top, right, top + row_height), fill=colors["white"], outline=colors["border"], width=1)
                draw.rectangle((left, top, right, top + 8), fill=colors["green"])
                draw.text((left + 14, top + 18), entry["role_label"], font=card_label_font, fill=colors["green"])

                name_lines = wrap_text(entry.get("person_name"), body_font, width - 28)
                name_y = top + 52
                for line in name_lines[:2]:
                    draw.text((left + 14, name_y), line or " ", font=body_font, fill=colors["ink"])
                    name_y += line_height(body_font, extra=2)

                sig_box_left = left + 14
                sig_box_right = right - 14
                sig_box_top = top + 106
                sig_box_bottom = top + 194
                draw.rectangle(
                    (sig_box_left, sig_box_top, sig_box_right, sig_box_bottom),
                    fill=colors["card"],
                    outline=colors["border"],
                    width=1,
                )

                signature_image = load_signature_image(
                    entry.get("signature_upload"),
                    max_size=(max(40, sig_box_right - sig_box_left - 22), max(40, sig_box_bottom - sig_box_top - 22)),
                )
                signed_at_label = format_signed_at(entry.get("signed_at"))
                if signature_image is not None:
                    image_left = sig_box_left + ((sig_box_right - sig_box_left - signature_image.width) // 2)
                    image_top = sig_box_top + ((sig_box_bottom - sig_box_top - signature_image.height) // 2)
                    page.paste(signature_image, (image_left, image_top))
                    status_text = "SIGNED"
                    status_color = colors["green"]
                else:
                    line_y = sig_box_top + ((sig_box_bottom - sig_box_top) // 2)
                    draw.line((sig_box_left + 18, line_y, sig_box_right - 18, line_y), fill=colors["muted"], width=1)
                    status_text = "PENDING SIGNATURE"
                    status_color = colors["amber"]

                draw.text((left + 14, top + 202), status_text, font=signature_badge_font, fill=status_color)
                if signed_at_label:
                    signed_text = f"Signed: {signed_at_label}"
                    draw.text((left + 14, top + 220), signed_text, font=tiny_font, fill=colors["ink"])

                cursor_x += width + gutter

            y += row_height + gutter

    report_title = (cleaned_data.get("incident_title") or "").strip() or "Incident Response Template"
    report_id = (cleaned_data.get("incident_id") or "").strip() or "-"
    generated_at = timezone.localtime(timezone.now()).strftime("%b %d, %Y %H:%M")
    severity_value = cleaned_data.get("severity_level") or cleaned_data.get("severity_choice")
    current_status = cleaned_data.get("current_status")
    regulatory_impact = bool(cleaned_data.get("regulatory_impact"))

    new_page()
    draw_banner(report_title, report_id, generated_at, severity_value, current_status, regulatory_impact)

    draw_field_table(
        "Incident Details",
        [
            ("Incident ID", cleaned_data.get("incident_id")),
            ("Date / Time Detected", cleaned_data.get("detected_at")),
            ("Reported By", cleaned_data.get("reported_by")),
            ("Incident Commander / Owner", cleaned_data.get("incident_commander")),
            ("Severity", format_choice(severity_value, IncidentReport.SEVERITY_CHOICES)),
            ("Current Status", cleaned_data.get("current_status")),
            ("Service Affected", format_service(cleaned_data.get("service_affected"))),
            ("Branch Impacted", cleaned_data.get("branch_impacted")),
            ("Downtime Duration", format_downtime(cleaned_data.get("downtime_duration_minutes"))),
            ("Regulatory Impact (NRB)", regulatory_impact),
        ],
        "Core incident metadata",
    )

    draw_narrative_section(
        "1. Summary",
        [
            ("What happened", cleaned_data.get("summary_what_happened")),
            ("How was the incident detected", cleaned_data.get("summary_detected")),
            ("Which systems, users, or services are affected", cleaned_data.get("summary_affected")),
        ],
        "Incident overview",
    )

    draw_narrative_section(
        "2. Business Impact",
        [
            ("Affected branch / department", cleaned_data.get("impact_branch_department")),
            ("Number of users affected", cleaned_data.get("impact_users")),
            ("Operational or customer impact", cleaned_data.get("impact_operational")),
            ("Regulatory / compliance impact", cleaned_data.get("impact_regulatory")),
        ],
        "Operational and regulatory effect",
    )

    draw_field_table(
        "3. Timeline",
        [
            ("Detection", cleaned_data.get("timeline_detection")),
            ("Initial triage", cleaned_data.get("timeline_initial_triage")),
            ("Containment started", cleaned_data.get("timeline_containment_started")),
            ("Recovery started", cleaned_data.get("timeline_recovery_started")),
            ("Service restored", cleaned_data.get("timeline_service_restored")),
            ("Incident closed", cleaned_data.get("timeline_incident_closed")),
        ],
        "Key response checkpoints",
        columns=3,
    )

    draw_narrative_section(
        "4. Containment Actions",
        [
            ("Immediate actions taken", cleaned_data.get("containment_actions")),
            ("Temporary workarounds", cleaned_data.get("temporary_workarounds")),
            ("Escalations raised", cleaned_data.get("escalations_raised")),
        ],
        "Immediate response steps",
    )

    draw_narrative_section(
        "5. Eradication and Recovery",
        [
            ("Root cause identified", cleaned_data.get("eradication_root_cause")),
            ("Fix applied", cleaned_data.get("eradication_fix_applied")),
            ("Validation steps completed", cleaned_data.get("eradication_validation_steps")),
            ("Systems restored", cleaned_data.get("eradication_systems_restored")),
        ],
        "Remediation and restoration",
    )

    draw_narrative_section(
        "6. Communication Log",
        [
            ("Stakeholders notified", cleaned_data.get("communication_stakeholders")),
            ("Update frequency", cleaned_data.get("communication_update_frequency")),
            ("Latest update shared", cleaned_data.get("communication_latest_update")),
        ],
        "Stakeholder updates",
    )

    draw_narrative_section(
        "7. Evidence and References",
        [
            ("Ticket / case number", cleaned_data.get("evidence_ticket_case")),
            ("Logs collected", cleaned_data.get("evidence_logs")),
            ("Screenshots / attachments", cleaned_data.get("evidence_attachments")),
            ("Related vendors / contacts", cleaned_data.get("evidence_vendors")),
        ],
        "Supporting records",
    )

    draw_narrative_section(
        "8. Post-Incident Review",
        [
            ("Root cause summary", cleaned_data.get("review_root_cause_summary")),
            ("Lessons learned", cleaned_data.get("review_lessons_learned")),
            ("Preventive actions", cleaned_data.get("review_preventive_actions")),
            ("Action owners and due dates", cleaned_data.get("review_action_owners")),
        ],
        "Lessons and actions",
    )

    draw_report_section("9. Sign-Off", "Registered, reviewed, and approved")
    signature_entries = [
        {
            "role_label": "Incident Registered By:",
            "person_name": cleaned_data.get("incident_registered_person"),
            "signature_upload": cleaned_data.get("registered_signature"),
            "signed_at": cleaned_data.get("registered_signed_at"),
        }
    ]
    notified_signoffs = cleaned_data.get("notified_signoffs") or []
    if notified_signoffs:
        sorted_notified_signoffs = sorted(notified_signoffs, key=lambda item: item.get("level") or 0)
        final_level = max(
            (signoff.get("level") or index)
            for index, signoff in enumerate(sorted_notified_signoffs, start=1)
        )
        for index, signoff in enumerate(sorted_notified_signoffs, start=1):
            level = signoff.get("level") or index
            if level == final_level:
                formal_label = "Acknowledged By:"
            else:
                formal_label = "Reviewed By:"
            signature_entries.append(
                {
                    "role_label": signoff.get("formal_label") or formal_label,
                    "person_name": signoff.get("person_name"),
                    "signature_upload": signoff.get("signature_upload"),
                    "signed_at": signoff.get("signed_at"),
                }
            )
    draw_signature_row(signature_entries)

    output = BytesIO()
    pages[0].save(output, format="PDF", save_all=True, append_images=pages[1:])
    output.seek(0)
    base_name = (
        (cleaned_data.get("incident_title") or "").strip()
        or (cleaned_data.get("incident_id") or "").strip()
        or "incident-response-template"
    )
    filename = f"{get_valid_filename(base_name).replace(' ', '_')}.pdf"
    return FileResponse(output, as_attachment=as_attachment, filename=filename, content_type="application/pdf")


def _format_incident_docx_value(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if hasattr(value, "strftime"):
        try:
            return timezone.localtime(value).strftime("%b %d, %Y %H:%M")
        except Exception:
            return value.strftime("%b %d, %Y %H:%M")
    return str(value).strip()


def _incident_docx_choice_label(value, choices):
    if not value:
        return ""
    return dict(choices).get(value, str(value))


def _incident_docx_service_label(value):
    if not value:
        return ""
    return dict(IncidentReport.SERVICE_CHOICES).get(value, str(value))


def _incident_docx_append_value(text, value):
    value = _format_incident_docx_value(value)
    if not value:
        return text
    spacer = "" if text.endswith((" ", "\t", "\n")) else " "
    return f"{text}{spacer}{value}"


def _incident_docx_set_node_value(text_nodes, index, value, *, suffix_indexes=()):
    if index >= len(text_nodes):
        return
    base_text = text_nodes[index].text or ""
    if suffix_indexes and ":" not in base_text:
        base_text = f"{base_text}: "
    text_nodes[index].text = _incident_docx_append_value(base_text, value)
    for suffix_index in suffix_indexes:
        if suffix_index < len(text_nodes):
            text_nodes[suffix_index].text = ""


def _replace_docx_label_value(root, label, value):
    formatted = _format_incident_docx_value(value)
    normalized_label = label.strip()
    normalized_label_without_colon = normalized_label.rstrip(":")
    parent_map = {child: parent for parent in root.iter() for child in parent}
    block_value_labels = {
        "source of incident",
        "incident description",
        "system(s) impacted",
        "network impacted",
        "operations impacted",
        "recovery actions",
        "post recovery verification",
        "communication",
        "quarantine process",
        "immediate actions",
        "root cause analysis",
        "eradication",
        "lessons learned",
        "recommendations for improvement",
        "action plan",
        "unit or department requiring notification",
        "point of contact",
    }

    def comparable(text):
        return (text or "").replace("’", "'").casefold()

    comparable_label = comparable(normalized_label)
    comparable_label_without_colon = comparable(normalized_label_without_colon)

    def label_matches(paragraph_text):
        comparable_paragraph_text = comparable(paragraph_text)
        if normalized_label.endswith(":"):
            return (
                comparable_paragraph_text.startswith(comparable_label)
                or comparable_paragraph_text.startswith(f"{comparable_label_without_colon}:")
            )
        if comparable_paragraph_text == comparable_label or comparable_paragraph_text.startswith(f"{comparable_label}:"):
            return True
        return comparable_label != "date" and comparable_paragraph_text.startswith(f"{comparable_label} ")

    for paragraph in root.findall(".//w:p", WORD_NS):
        text_nodes = paragraph.findall(".//w:t", WORD_NS)
        paragraph_text = "".join(node.text or "" for node in text_nodes)
        normalized_paragraph_text = paragraph_text.strip()
        if not label_matches(normalized_paragraph_text):
            continue

        separator = " " if normalized_label.endswith(":") else ": "
        _clear_docx_paragraph_runs(paragraph)
        _set_docx_paragraph_alignment(paragraph, "left")
        _append_docx_text_run(paragraph, normalized_label, bold=True)
        if formatted:
            use_block_value_layout = (
                normalized_label_without_colon.casefold() in block_value_labels
                or len(formatted) > 80
            )
            if use_block_value_layout:
                value_paragraph = ET.Element(f"{{{WORD_NS['w']}}}p")
                _set_docx_paragraph_alignment(value_paragraph, "both")
                _append_docx_text_run(value_paragraph, formatted, bold=False)
                parent = parent_map.get(paragraph)
                if parent is not None:
                    siblings = list(parent)
                    try:
                        parent.insert(siblings.index(paragraph) + 1, value_paragraph)
                    except ValueError:
                        parent.append(value_paragraph)
                else:
                    _append_docx_text_run(paragraph, f"{separator}{formatted}", bold=False)
            else:
                _append_docx_text_run(paragraph, f"{separator}{formatted}", bold=False)
        return True
    return False


def _replace_incident_template_signoff_paragraph(root, body):
    if body is None:
        return False

    signoff_paragraph = None
    for paragraph in root.findall(".//w:p", WORD_NS):
        paragraph_text = "".join(node.text or "" for node in paragraph.findall(".//w:t", WORD_NS))
        if "Registered By:" in paragraph_text and "Approved By:" in paragraph_text:
            signoff_paragraph = paragraph
            break
    if signoff_paragraph is None:
        return False

    table_width = 9355
    labels = ["Registered By:", "Reviewed By:", "Reviewed By:", "Acknowledged By:"]
    cell_width = str(int(table_width / len(labels)))
    table = ET.Element(f"{{{WORD_NS['w']}}}tbl")
    table_properties = ET.SubElement(table, f"{{{WORD_NS['w']}}}tblPr")
    ET.SubElement(
        table_properties,
        f"{{{WORD_NS['w']}}}tblW",
        {f"{{{WORD_NS['w']}}}w": str(table_width), f"{{{WORD_NS['w']}}}type": "dxa"},
    )
    ET.SubElement(table_properties, f"{{{WORD_NS['w']}}}tblLayout", {f"{{{WORD_NS['w']}}}type": "fixed"})
    borders = ET.SubElement(table_properties, f"{{{WORD_NS['w']}}}tblBorders")
    for border_name in ("top", "left", "bottom", "right", "insideH", "insideV"):
        ET.SubElement(
            borders,
            f"{{{WORD_NS['w']}}}{border_name}",
            {
                f"{{{WORD_NS['w']}}}val": "single",
                f"{{{WORD_NS['w']}}}sz": "4",
                f"{{{WORD_NS['w']}}}space": "0",
                f"{{{WORD_NS['w']}}}color": "000000",
            },
        )

    row = ET.SubElement(table, f"{{{WORD_NS['w']}}}tr")
    row_properties = ET.SubElement(row, f"{{{WORD_NS['w']}}}trPr")
    ET.SubElement(row_properties, f"{{{WORD_NS['w']}}}cantSplit")
    for label in labels:
        cell = ET.SubElement(row, f"{{{WORD_NS['w']}}}tc")
        cell_properties = ET.SubElement(cell, f"{{{WORD_NS['w']}}}tcPr")
        ET.SubElement(
            cell_properties,
            f"{{{WORD_NS['w']}}}tcW",
            {f"{{{WORD_NS['w']}}}w": cell_width, f"{{{WORD_NS['w']}}}type": "dxa"},
        )
        paragraph = ET.SubElement(cell, f"{{{WORD_NS['w']}}}p")
        paragraph_properties = ET.SubElement(paragraph, f"{{{WORD_NS['w']}}}pPr")
        ET.SubElement(paragraph_properties, f"{{{WORD_NS['w']}}}keepLines")
        run = ET.SubElement(paragraph, f"{{{WORD_NS['w']}}}r")
        run_properties = ET.SubElement(run, f"{{{WORD_NS['w']}}}rPr")
        ET.SubElement(run_properties, f"{{{WORD_NS['w']}}}b")
        text_node = ET.SubElement(run, f"{{{WORD_NS['w']}}}t")
        text_node.text = label

    children = list(body)
    try:
        paragraph_index = children.index(signoff_paragraph)
    except ValueError:
        _set_docx_paragraph_text(signoff_paragraph, "")
        body.append(table)
        return True
    body.remove(signoff_paragraph)
    body.insert(paragraph_index, table)
    return True


def _incident_docx_signature_summary(cleaned_data):
    lines = []
    registered_name = cleaned_data.get("incident_registered_person")
    registered_signed_at = _format_incident_docx_value(cleaned_data.get("registered_signed_at"))
    registered_status = "Signed" if cleaned_data.get("registered_signature") else "Pending signature"
    lines.append(
        "Incident Registered By: "
        f"{_format_incident_docx_value(registered_name) or '-'}"
        f" ({registered_status}{', ' + registered_signed_at if registered_signed_at else ''})"
    )

    notified_signoffs = sorted(
        cleaned_data.get("notified_signoffs") or [],
        key=lambda item: item.get("level") or 0,
    )
    if notified_signoffs:
        final_level = max((item.get("level") or index) for index, item in enumerate(notified_signoffs, start=1))
        for index, signoff in enumerate(notified_signoffs, start=1):
            level = signoff.get("level") or index
            role_label = signoff.get("formal_label") or ("Acknowledged By:" if level == final_level else "Reviewed By:")
            signed_at = _format_incident_docx_value(signoff.get("signed_at"))
            status = "Signed" if signoff.get("signature_upload") else "Pending signature"
            lines.append(
                f"{role_label} {_format_incident_docx_value(signoff.get('person_name')) or '-'} "
                f"({status}{', ' + signed_at if signed_at else ''})"
            )
    elif cleaned_data.get("incident_notified_person"):
        lines.append(f"Incident Notified Person: {_format_incident_docx_value(cleaned_data.get('incident_notified_person'))}")
    return "\n".join(lines)


def _build_incident_response_template_docx(cleaned_data):
    data = dict(cleaned_data or {})
    severity_value = data.get("severity_choice") or data.get("severity_level")
    severity_label = _incident_docx_choice_label(severity_value, IncidentReport.SEVERITY_CHOICES) or _format_incident_docx_value(severity_value)
    attachment_parts = [
        data.get("evidence_attachments"),
        _incident_docx_signature_summary(data),
    ]
    attachment_summary = "\n".join(part for part in attachment_parts if _format_incident_docx_value(part))

    label_value_pairs = {
        "Version": "1.0",
        "Date": data.get("date_of_report") or timezone.localtime(timezone.now()).strftime("%b %d, %Y"),
        "Reporting Employee’s Name:": data.get("reporting_employee_name") or data.get("reported_by"),
        "Designation:": data.get("reporting_employee_designation"),
        "Email:": data.get("reporting_employee_email"),
        "Contact Number:": data.get("reporting_employee_contact"),
        "Date of Report:": data.get("date_of_report"),
        "Incident ID:": data.get("incident_id"),
        "Date & Time of Occurrence:": data.get("date_time_of_occurrence") or data.get("timeline_detection"),
        "Date & Time of Detection:": data.get("date_time_of_detection") or data.get("detected_at"),
        "Source of Incident:": data.get("source_of_incident") or data.get("summary_detected"),
        "Incident Location (IP):": data.get("incident_location_ip") or data.get("branch_impacted"),
        "Incident Description:": data.get("incident_description") or data.get("summary_what_happened"),
        "Unit or Department Impacted:": data.get("unit_or_department_impacted") or data.get("impact_branch_department"),
        "System(s) Impacted:": data.get("systems_impacted") or _incident_docx_service_label(data.get("service_affected")) or data.get("summary_affected"),
        "Network Impacted:": data.get("network_impacted"),
        "Operations Impacted:": data.get("operations_impacted") or data.get("impact_operational"),
        "Incident Severity:": severity_label,
        "Recovery Actions:": data.get("recovery_actions") or data.get("eradication_fix_applied"),
        "Recovery Timeframe:": data.get("recovery_timeframe") or data.get("timeline_service_restored"),
        "Post Recovery Verification:": data.get("post_recovery_verification") or data.get("eradication_validation_steps"),
        "Communication:": data.get("recovery_communication") or data.get("communication_latest_update"),
        "Quarantine Process:": data.get("quarantine_process") or data.get("containment_actions"),
        "Immediate Actions:": data.get("immediate_actions") or data.get("containment_actions"),
        "Root Cause Analysis:": data.get("root_cause_analysis") or data.get("eradication_root_cause") or data.get("review_root_cause_summary"),
        "Eradication:": data.get("eradication_method") or data.get("eradication_fix_applied"),
        "Lessons Learned:": data.get("lessons_learned") or data.get("review_lessons_learned"),
        "Recommendations for Improvement:": data.get("recommendations_for_improvement") or data.get("review_preventive_actions"),
        "Action Plan:": data.get("action_plan") or data.get("review_action_owners"),
        "Unit or Department Requiring Notification:": data.get("unit_or_department_requiring_notification") or data.get("communication_stakeholders"),
        "Point of Contact:": data.get("point_of_contact") or data.get("evidence_vendors"),
        "Date of Notification:": data.get("date_of_notification"),
    }

    def _set_docx_label_value(label, value):
        return _replace_docx_label_value(root, label, value)

    output = BytesIO()
    with zipfile.ZipFile(INCIDENT_REPORT_TEMPLATE_DOCX, "r") as source_docx:
        document_xml = source_docx.read("word/document.xml")
        rels_xml = source_docx.read("word/_rels/document.xml.rels")
        content_types_xml = source_docx.read("[Content_Types].xml")
        root = ET.fromstring(document_xml)
        rels_root = ET.fromstring(rels_xml)
        content_types_root = ET.fromstring(content_types_xml)
        media_items = {}
        body = root.find("w:body", WORD_NS)
        _remove_docx_leading_empty_paragraphs(body)
        for label, value in label_value_pairs.items():
            _set_docx_label_value(label, value)

        tables = root.findall(".//w:tbl", WORD_NS)

        if len(tables) > 1:
            rows = tables[1].findall("./w:tr", WORD_NS)
            attachment_row_index = None
            for row_index in range(len(rows) - 1):
                row_text = "".join(
                    text_node.text or ""
                    for cell in rows[row_index].findall("./w:tc", WORD_NS)
                    for text_node in cell.findall(".//w:t", WORD_NS)
                )
                if "ATTACHMENTS" in row_text.upper():
                    attachment_row_index = row_index + 1
                    break

            if attachment_row_index is not None and attachment_row_index < len(rows):
                cells = rows[attachment_row_index].findall("./w:tc", WORD_NS)
                if cells:
                    _set_docx_cell_text(cells[0], attachment_summary)

        _clear_docx_checkbox_glyphs(root)
        _clear_incident_docx_placeholders(root)
        _remove_incident_docx_severity_option_table(root)
        _replace_incident_template_signoff_paragraph(root, body)
        _normalize_incident_docx_cover_page(root)

        _normalize_docx_text_font(root, font_name="Times New Roman", font_size="24", bold=False)
        _apply_incident_docx_header_bold(root, font_name="Times New Roman", font_size="24")
        _apply_incident_docx_label_bold(root, font_name="Times New Roman", font_size="24")

        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target_docx:
            for item in source_docx.infolist():
                if item.filename == "word/document.xml":
                    target_docx.writestr(item, ET.tostring(root, encoding="utf-8", xml_declaration=True))
                elif item.filename.startswith("word/header") and item.filename.endswith(".xml"):
                    header_root = ET.fromstring(source_docx.read(item.filename))
                    _clear_incident_docx_header(header_root)
                    target_docx.writestr(item, ET.tostring(header_root, encoding="utf-8", xml_declaration=True))
                elif item.filename == "word/glossary/document.xml":
                    glossary_root = ET.fromstring(source_docx.read(item.filename))
                    _clear_incident_docx_placeholders(glossary_root)
                    target_docx.writestr(item, ET.tostring(glossary_root, encoding="utf-8", xml_declaration=True))
                elif item.filename == "word/_rels/document.xml.rels":
                    target_docx.writestr(item, _serialize_docx_package_xml(rels_root, DOCX_REL_NS, "rel"))
                elif item.filename == "[Content_Types].xml":
                    target_docx.writestr(item, _serialize_docx_package_xml(content_types_root, DOCX_CONTENT_TYPES_NS, "ct"))
                else:
                    target_docx.writestr(item, source_docx.read(item.filename))
            for media_path, payload in media_items.items():
                target_docx.writestr(media_path, payload)

    return output.getvalue()


def _build_incident_response_template_docx_response(cleaned_data, *, as_attachment=True):
    payload = _build_incident_response_template_docx(cleaned_data)
    output = BytesIO(payload)
    output.seek(0)
    base_name = (
        (cleaned_data.get("incident_title") or "").strip()
        or (cleaned_data.get("incident_id") or "").strip()
        or "incident-response-template"
    )
    filename = f"{get_valid_filename(base_name).replace(' ', '_')}.docx"
    return FileResponse(
        output,
        as_attachment=as_attachment,
        filename=filename,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


def _incident_response_template_image_payloads(cleaned_data, image_format="png"):
    converter = shutil.which("libreoffice") or shutil.which("soffice")
    if not converter or not os.path.exists(INCIDENT_REPORT_TEMPLATE_DOCX):
        return []

    normalized_format = "jpeg" if image_format in {"jpg", "jpeg"} else "png"
    docx_payload = _build_incident_response_template_docx(cleaned_data or {})
    with tempfile.TemporaryDirectory() as tmpdir:
        docx_path = os.path.join(tmpdir, "incident-response-template.docx")
        with open(docx_path, "wb") as docx_file:
            docx_file.write(docx_payload)

        try:
            subprocess.run(
                [
                    converter,
                    "--headless",
                    "--nologo",
                    "--nofirststartwizard",
                    "--convert-to",
                    "png",
                    "--outdir",
                    tmpdir,
                    docx_path,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except Exception:
            return []

        png_paths = sorted(
            os.path.join(tmpdir, item)
            for item in os.listdir(tmpdir)
            if item.lower().endswith(".png")
        )
        image_payloads = []
        for png_path in png_paths:
            with open(png_path, "rb") as image_file:
                image_payload = image_file.read()
            if normalized_format == "jpeg":
                try:
                    with Image.open(BytesIO(image_payload)) as image:
                        converted = BytesIO()
                        image.convert("RGB").save(converted, format="JPEG", quality=92)
                        image_payload = converted.getvalue()
                except Exception:
                    return []
            image_payloads.append(image_payload)
        return image_payloads


def _incident_response_template_docx_to_pdf_payload(cleaned_data=None):
    converter = shutil.which("libreoffice") or shutil.which("soffice")
    if not converter:
        return None

    docx_payload = _build_incident_response_template_docx(cleaned_data or {})
    with tempfile.TemporaryDirectory() as tmpdir:
        docx_path = os.path.join(tmpdir, "incident-response-template.docx")
        with open(docx_path, "wb") as docx_file:
            docx_file.write(docx_payload)

        try:
            subprocess.run(
                [
                    converter,
                    "--headless",
                    "--nologo",
                    "--nofirststartwizard",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    tmpdir,
                    docx_path,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except Exception:
            return None

        pdf_path = os.path.join(tmpdir, "incident-response-template.pdf")
        if not os.path.exists(pdf_path):
            return None

        with open(pdf_path, "rb") as pdf_file:
            return pdf_file.read()


def _find_html_pdf_converter():
    candidates = [
        shutil.which("chrome"),
        shutil.which("chrome.exe"),
        shutil.which("msedge"),
        shutil.which("msedge.exe"),
        r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
        r"C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
        r"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _incident_response_template_html_to_pdf_payload(cleaned_data=None):
    converter = _find_html_pdf_converter()
    if not converter:
        return None

    form = IncidentResponseTemplateForm(cleaned_data or {})
    html = render_to_string("docs/incident_response_template_pdf.html", {"form": form, "data": cleaned_data or {}})
    with tempfile.TemporaryDirectory() as tmpdir:
        html_path = os.path.join(tmpdir, "incident-response-template.html")
        pdf_path = os.path.join(tmpdir, "incident-response-template.pdf")
        with open(html_path, "w", encoding="utf-8") as html_file:
            html_file.write(html)

        try:
            subprocess.run(
                [
                    converter,
                    "--headless",
                    "--disable-gpu",
                    f"--print-to-pdf={pdf_path}",
                    Path(html_path).as_uri(),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except Exception:
            return None

        if not os.path.exists(pdf_path):
            return None

        with open(pdf_path, "rb") as pdf_file:
            return pdf_file.read()


def _get_signoff_names_by_level(signoffs, levels):
    """Extract signoff names for specific approval levels."""
    if not signoffs:
        return ""
    names = []
    for signoff in signoffs:
        if signoff.get("level") in levels:
            name = signoff.get("person_name", "").strip()
            if name:
                names.append(name)
    return ", ".join(names) if names else ""


def _get_signoff_signature_by_level(signoffs, levels):
    """Extract signoff signature payload for specific approval levels."""
    if not signoffs:
        return None
    for signoff in signoffs:
        if signoff.get("level") in levels:
            signature = signoff.get("signature_upload")
            if signature:
                return signature
    return None


def _set_docx_label_value_with_signature(label, value, signature_payload=None, rels_root=None, content_types_root=None, media_items=None):
    """Set label value and optionally insert signature image after it."""
    if value is None and not signature_payload:
        return False
    formatted = _format_incident_docx_value(value) if value else ""
    if not formatted and not signature_payload:
        return False

    for paragraph in root.findall(".//w:p", WORD_NS):
        paragraph_text = "".join(node.text or "" for node in paragraph.findall(".//w:t", WORD_NS))
        if paragraph_text.strip().startswith(label.strip()):
            runs = paragraph.findall("./w:r", WORD_NS)
            if not runs:
                run = ET.SubElement(paragraph, f"{{{WORD_NS['w']}}}r")
                text = ET.SubElement(run, f"{{{WORD_NS['w']}}}t")
                text.text = f" {formatted}"
            else:
                last_run = runs[-1]
                text = last_run.find("w:t", WORD_NS)
                if text is None:
                    text = ET.SubElement(last_run, f"{{{WORD_NS['w']}}}t")
                    text.text = ""
                suffix = "" if (text.text or "").endswith(" ") else " "
                text.text = f"{text.text or ''}{suffix}{formatted}"

            # Insert signature image if provided
            if signature_payload and rels_root is not None and content_types_root is not None and media_items is not None:
                image_name = f"signature_{label.lower().replace(':', '').replace(' ', '_')}"
                rel_id = _add_docx_png_relationship(rels_root, content_types_root, media_items, image_name, signature_payload)
                if rel_id:
                    # Add image in a new run after the text
                    image_run = ET.SubElement(paragraph, f"{{{WORD_NS['w']}}}r")
                    _append_docx_image_to_run(image_run, rel_id, name=f"{label} Signature")

            return True
    return False


def _append_docx_image_to_run(run, relationship_id, *, name="Digital signature"):
    """Append an image to an existing run."""
    drawing = ET.SubElement(run, f"{{{WORD_NS['w']}}}drawing")

    wp_ns = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
    a_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    pic_ns = "http://schemas.openxmlformats.org/drawingml/2006/picture"
    r_ns = DOCX_WORD_REL_NS

    inline = ET.SubElement(drawing, f"{{{wp_ns}}}inline", {"distT": "0", "distB": "0", "distL": "0", "distR": "0"})
    ET.SubElement(inline, f"{{{wp_ns}}}extent", {"cx": "1714500", "cy": "594360"})  # Standard signature size
    ET.SubElement(inline, f"{{{wp_ns}}}effectExtent", {"l": "0", "t": "0", "r": "0", "b": "0"})
    ET.SubElement(inline, f"{{{wp_ns}}}docPr", {"id": str(secrets.randbelow(900000) + 100000), "name": name})
    ET.SubElement(inline, f"{{{wp_ns}}}cNvGraphicFramePr")
    graphic = ET.SubElement(inline, f"{{{a_ns}}}graphic")
    graphic_data = ET.SubElement(graphic, f"{{{a_ns}}}graphicData", {"uri": pic_ns})
    pic = ET.SubElement(graphic_data, f"{{{pic_ns}}}pic")
    nv_pic_pr = ET.SubElement(pic, f"{{{pic_ns}}}nvPicPr")
    ET.SubElement(nv_pic_pr, f"{{{pic_ns}}}cNvPr", {"id": "0", "name": f"{name}.png"})
    ET.SubElement(nv_pic_pr, f"{{{pic_ns}}}cNvPicPr")
    blip_fill = ET.SubElement(pic, f"{{{pic_ns}}}blipFill")
    ET.SubElement(blip_fill, f"{{{a_ns}}}blip", {f"{{{r_ns}}}embed": relationship_id})
    stretch = ET.SubElement(blip_fill, f"{{{a_ns}}}stretch")
    ET.SubElement(stretch, f"{{{a_ns}}}fillRect")
    sp_pr = ET.SubElement(pic, f"{{{pic_ns}}}spPr")
    xfrm = ET.SubElement(sp_pr, f"{{{a_ns}}}xfrm")
    ET.SubElement(xfrm, f"{{{a_ns}}}off", {"x": "0", "y": "0"})
    ET.SubElement(xfrm, f"{{{a_ns}}}ext", {"cx": "1714500", "cy": "594360"})
    prst_geom = ET.SubElement(sp_pr, f"{{{a_ns}}}prstGeom", {"prst": "rect"})
    ET.SubElement(prst_geom, f"{{{a_ns}}}avLst")


def _build_ticket_incident_report_docx(ticket, incident_report):
    if not os.path.exists(INCIDENT_REPORT_TEMPLATE_DOCX):
        return None

    data = _incident_response_template_source_from_report(incident_report)
    severity_value = incident_report.severity_choice or incident_report.severity_level
    severity_label = _incident_docx_choice_label(severity_value, IncidentReport.SEVERITY_CHOICES) or _format_incident_docx_value(severity_value)
    attachment_summary = ""
    notified_signoffs = data.get("notified_signoffs", [])
    signoff_levels = [signoff.get("level") for signoff in notified_signoffs if signoff.get("level")]
    final_signoff_level = max(signoff_levels) if signoff_levels else None
    reviewed_signoff_levels = [
        signoff.get("level")
        for signoff in notified_signoffs
        if signoff.get("level") and signoff.get("level") != final_signoff_level
    ]
    approved_signoff_levels = [final_signoff_level] if final_signoff_level else []

    label_value_pairs = {
        "Version": "1.0",
        "Date": incident_report.date_of_report or timezone.localtime(timezone.now()).strftime("%b %d, %Y"),
        "Reporting Employee’s Name:": data.get("reporting_employee_name"),
        "Designation:": data.get("reporting_employee_designation"),
        "Email:": data.get("reporting_employee_email"),
        "Contact Number:": data.get("reporting_employee_contact"),
        "Date of Report:": data.get("date_of_report"),
        "Incident ID:": incident_report.incident_id,
        "Date & Time of Occurrence:": incident_report.date_time_of_occurrence or incident_report.timeline_detection,
        "Date & Time of Detection:": incident_report.date_time_of_detection or incident_report.detected_at,
        "Source of Incident:": incident_report.source_of_incident or incident_report.summary_detected,
        "Incident Location (IP):": incident_report.incident_location_ip or incident_report.branch_impacted,
        "Incident Description:": incident_report.incident_description or incident_report.summary_what_happened,
        "Unit or Department Impacted:": incident_report.unit_or_department_impacted or incident_report.impact_branch_department,
        "System(s) Impacted:": incident_report.systems_impacted or _incident_docx_service_label(incident_report.service_affected) or incident_report.summary_affected,
        "Network Impacted:": incident_report.network_impacted,
        "Operations Impacted:": incident_report.operations_impacted or incident_report.impact_operational,
        "Incident Severity:": severity_label,
        "Recovery Actions:": incident_report.recovery_actions or incident_report.eradication_fix_applied,
        "Recovery Timeframe:": incident_report.recovery_timeframe or incident_report.timeline_service_restored,
        "Post Recovery Verification:": incident_report.post_recovery_verification or incident_report.eradication_validation_steps,
        "Communication:": incident_report.recovery_communication or incident_report.communication_latest_update,
        "Quarantine Process:": incident_report.quarantine_process or incident_report.containment_actions,
        "Immediate Actions:": incident_report.immediate_actions or incident_report.containment_actions,
        "Root Cause Analysis:": incident_report.root_cause_analysis or incident_report.eradication_root_cause or incident_report.review_root_cause_summary,
        "Eradication:": incident_report.eradication_method or incident_report.eradication_fix_applied,
        "Lessons Learned:": incident_report.lessons_learned or incident_report.review_lessons_learned,
        "Recommendations for Improvement:": incident_report.recommendations_for_improvement or incident_report.review_preventive_actions,
        "Action Plan:": incident_report.action_plan or incident_report.review_action_owners,
        "Unit or Department Requiring Notification:": incident_report.unit_or_department_requiring_notification or incident_report.communication_stakeholders,
        "Point of Contact:": incident_report.point_of_contact or incident_report.evidence_vendors,
        "Date of Notification:": incident_report.date_of_notification,
    }

    def _set_docx_label_value(label, value):
        return _replace_docx_label_value(root, label, value)

    def _set_docx_label_value_with_signature(label, value, signature_payload=None):
        inserted = False
        if value is not None:
            inserted = _set_docx_label_value(label, value)
        if signature_payload is None:
            return inserted

        for paragraph in root.findall(".//w:p", WORD_NS):
            paragraph_text = "".join(node.text or "" for node in paragraph.findall(".//w:t", WORD_NS))
            if paragraph_text.strip().startswith(label.strip()):
                cell = None
                for candidate in root.findall(".//w:tc", WORD_NS):
                    if paragraph in list(candidate.iter()):
                        cell = candidate
                        break
                if cell is None:
                    return inserted
                rel_id = _add_docx_png_relationship(
                    rels_root,
                    content_types_root,
                    media_items,
                    f"incident_report_signature_{label.strip().lower().replace(' ', '_').replace(':', '')}",
                    signature_payload,
                )
                if rel_id:
                    _append_docx_image_to_cell(cell, rel_id, name=f"{label} Signature")
                    return True
        return inserted

    def _docx_table_cell(text="", width="2160"):
        cell = ET.Element(f"{{{WORD_NS['w']}}}tc")
        cell_properties = ET.SubElement(cell, f"{{{WORD_NS['w']}}}tcPr")
        ET.SubElement(cell_properties, f"{{{WORD_NS['w']}}}tcW", {f"{{{WORD_NS['w']}}}w": width, f"{{{WORD_NS['w']}}}type": "dxa"})
        paragraph = ET.SubElement(cell, f"{{{WORD_NS['w']}}}p")
        if text:
            run = ET.SubElement(paragraph, f"{{{WORD_NS['w']}}}r")
            text_node = ET.SubElement(run, f"{{{WORD_NS['w']}}}t")
            text_node.text = str(text)
        return cell

    def _docx_table_row(values, width="2160"):
        row = ET.Element(f"{{{WORD_NS['w']}}}tr")
        row_properties = ET.SubElement(row, f"{{{WORD_NS['w']}}}trPr")
        ET.SubElement(row_properties, f"{{{WORD_NS['w']}}}cantSplit")
        for value in values:
            row.append(_docx_table_cell(value, width=width))
        return row

    def _append_docx_cell_paragraph(cell, value=""):
        paragraph = ET.SubElement(cell, f"{{{WORD_NS['w']}}}p")
        paragraph_properties = ET.SubElement(paragraph, f"{{{WORD_NS['w']}}}pPr")
        ET.SubElement(paragraph_properties, f"{{{WORD_NS['w']}}}keepLines")
        run = ET.SubElement(paragraph, f"{{{WORD_NS['w']}}}r")
        text_node = ET.SubElement(run, f"{{{WORD_NS['w']}}}t")
        text_node.text = str(value or "")
        return paragraph

    def _format_incident_signoff_date(value):
        if not value:
            return ""
        if isinstance(value, str):
            return value
        try:
            return timezone.localtime(value).strftime("%b %d, %Y %H:%M")
        except Exception:
            return str(value)

    def _replace_incident_docx_signoff_paragraph():
        signoff_paragraph = None
        report_table_width = 9355
        for paragraph in root.findall(".//w:p", WORD_NS):
            paragraph_text = "".join(node.text or "" for node in paragraph.findall(".//w:t", WORD_NS))
            if "Registered By:" in paragraph_text and "Reviewed By:" in paragraph_text:
                signoff_paragraph = paragraph
                break
        if signoff_paragraph is None or body is None:
            return

        entries = [
            {
                "label": "Registered By:",
                "person_name": incident_report.incident_registered_person or incident_report.display_registered_person,
                "signature_upload": incident_report.registered_signature,
                "signed_at": incident_report.registered_signed_at,
            }
        ]
        sorted_notified_signoffs = sorted(notified_signoffs, key=lambda item: item.get("level") or 0)
        for signoff in sorted_notified_signoffs:
            entries.append(
                {
                    "label": signoff.get("formal_label") or ("Acknowledged By:" if signoff.get("level") == final_signoff_level else "Reviewed By:"),
                    "person_name": signoff.get("person_name"),
                    "signature_upload": signoff.get("signature_upload"),
                    "signed_at": signoff.get("signed_at"),
                }
            )

        table = ET.Element(f"{{{WORD_NS['w']}}}tbl")
        table_properties = ET.SubElement(table, f"{{{WORD_NS['w']}}}tblPr")
        ET.SubElement(table_properties, f"{{{WORD_NS['w']}}}tblW", {f"{{{WORD_NS['w']}}}w": str(report_table_width), f"{{{WORD_NS['w']}}}type": "dxa"})
        ET.SubElement(table_properties, f"{{{WORD_NS['w']}}}tblLayout", {f"{{{WORD_NS['w']}}}type": "fixed"})
        borders = ET.SubElement(table_properties, f"{{{WORD_NS['w']}}}tblBorders")
        for border_name in ("top", "left", "bottom", "right", "insideH", "insideV"):
            ET.SubElement(
                borders,
                f"{{{WORD_NS['w']}}}{border_name}",
                {f"{{{WORD_NS['w']}}}val": "single", f"{{{WORD_NS['w']}}}sz": "4", f"{{{WORD_NS['w']}}}space": "0", f"{{{WORD_NS['w']}}}color": "000000"},
            )

        for chunk_start in range(0, len(entries), 4):
            chunk = entries[chunk_start : chunk_start + 4]
            cell_width = str(int(report_table_width / max(len(chunk), 1)))
            signoff_row = _docx_table_row(["" for _entry in chunk], width=cell_width)
            for index, entry in enumerate(chunk):
                cell = signoff_row.findall("./w:tc", WORD_NS)[index]
                _set_docx_cell_text(cell, entry["label"])
                payload = _incident_signature_png_payload(entry.get("signature_upload"))
                if not payload:
                    _append_docx_cell_paragraph(cell, "Signature pending")
                else:
                    rel_id = _add_docx_png_relationship(
                        rels_root,
                        content_types_root,
                        media_items,
                        f"incident_report_signoff_{chunk_start + index + 1}",
                        payload,
                    )
                    if rel_id:
                        _append_docx_image_to_existing_cell(
                            cell,
                            rel_id,
                            name=f"{entry['label']} Signature",
                            width_emu="1500000",
                            height_emu="520000",
                        )
                _append_docx_cell_paragraph(cell, f"Name: {_format_incident_docx_value(entry.get('person_name')) or '-'}")
                _append_docx_cell_paragraph(cell, f"Date: {_format_incident_signoff_date(entry.get('signed_at')) or '-'}")
            table.append(signoff_row)

        children = list(body)
        try:
            paragraph_index = children.index(signoff_paragraph)
        except ValueError:
            _set_docx_paragraph_text(signoff_paragraph, "")
            body.append(table)
            return
        body.remove(signoff_paragraph)
        body.insert(paragraph_index, table)

    output = BytesIO()
    with zipfile.ZipFile(INCIDENT_REPORT_TEMPLATE_DOCX, "r") as source_docx:
        document_xml = source_docx.read("word/document.xml")
        rels_xml = source_docx.read("word/_rels/document.xml.rels")
        content_types_xml = source_docx.read("[Content_Types].xml")
        root = ET.fromstring(document_xml)
        rels_root = ET.fromstring(rels_xml)
        content_types_root = ET.fromstring(content_types_xml)
        media_items = {}
        body = root.find("w:body", WORD_NS)
        _remove_docx_leading_empty_paragraphs(body)
        for label, value in label_value_pairs.items():
            _set_docx_label_value(label, value)

        tables = root.findall(".//w:tbl", WORD_NS)
        _replace_incident_docx_signoff_paragraph()

        if len(tables) > 1:
            rows = tables[1].findall("./w:tr", WORD_NS)
            attachment_row_index = None
            for row_index in range(len(rows) - 1):
                row_text = "".join(
                    text_node.text or ""
                    for cell in rows[row_index].findall("./w:tc", WORD_NS)
                    for text_node in cell.findall(".//w:t", WORD_NS)
                )
                if "ATTACHMENTS" in row_text.upper():
                    attachment_row_index = row_index + 1
                    break

            if attachment_row_index is not None and attachment_row_index < len(rows):
                cells = rows[attachment_row_index].findall("./w:tc", WORD_NS)
                if cells:
                    _set_docx_cell_text(cells[0], attachment_summary)

        _clear_docx_checkbox_glyphs(root)
        _clear_incident_docx_placeholders(root)
        _remove_incident_docx_severity_option_table(root)
        _normalize_incident_docx_cover_page(root)

        _normalize_docx_text_font(root, font_name="Times New Roman", font_size="24", bold=False)
        _apply_incident_docx_header_bold(root, font_name="Times New Roman", font_size="24")
        _apply_incident_docx_label_bold(root, font_name="Times New Roman", font_size="24")

        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target_docx:
            for item in source_docx.infolist():
                if item.filename == "word/document.xml":
                    target_docx.writestr(item, ET.tostring(root, encoding="utf-8", xml_declaration=True))
                elif item.filename.startswith("word/header") and item.filename.endswith(".xml"):
                    header_root = ET.fromstring(source_docx.read(item.filename))
                    _clear_incident_docx_header(header_root)
                    target_docx.writestr(item, ET.tostring(header_root, encoding="utf-8", xml_declaration=True))
                elif item.filename == "word/glossary/document.xml":
                    glossary_root = ET.fromstring(source_docx.read(item.filename))
                    _clear_incident_docx_placeholders(glossary_root)
                    target_docx.writestr(item, ET.tostring(glossary_root, encoding="utf-8", xml_declaration=True))
                elif item.filename == "word/_rels/document.xml.rels":
                    target_docx.writestr(item, _serialize_docx_package_xml(rels_root, DOCX_REL_NS, "rel"))
                elif item.filename == "[Content_Types].xml":
                    target_docx.writestr(item, _serialize_docx_package_xml(content_types_root, DOCX_CONTENT_TYPES_NS, "ct"))
                else:
                    target_docx.writestr(item, source_docx.read(item.filename))
            for media_path, payload in media_items.items():
                target_docx.writestr(media_path, payload)

    return output.getvalue()


def _build_ticket_incident_report_docx_response(ticket, incident_report, *, as_attachment=True):
    docx_payload = _build_ticket_incident_report_docx(ticket, incident_report)
    if not docx_payload:
        return HttpResponse(
            "Incident report export is unavailable. Please contact support.",
            status=503,
            content_type="text/plain",
        )

    base_name = (
        (incident_report.incident_title or "").strip()
        or (incident_report.incident_id or "").strip()
        or "incident-report"
    )
    safe_name = get_valid_filename(base_name).replace(" ", "_") or "incident-report"
    output = BytesIO(docx_payload)
    output.seek(0)
    return FileResponse(
        output,
        as_attachment=as_attachment,
        filename=f"{safe_name}.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


def _ticket_incident_report_docx_to_pdf_payload(ticket, incident_report):
    converter = shutil.which("libreoffice") or shutil.which("soffice")
    if not converter:
        return None

    docx_payload = _build_ticket_incident_report_docx(ticket, incident_report)
    if not docx_payload:
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        docx_path = os.path.join(tmpdir, "incident-report.docx")
        with open(docx_path, "wb") as docx_file:
            docx_file.write(docx_payload)

        try:
            subprocess.run(
                [
                    converter,
                    "--headless",
                    "--nologo",
                    "--nofirststartwizard",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    tmpdir,
                    docx_path,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except Exception:
            return None

        pdf_path = os.path.join(tmpdir, "incident-report.pdf")
        if not os.path.exists(pdf_path):
            return None

        with open(pdf_path, "rb") as pdf_file:
            return pdf_file.read()


def _build_ticket_incident_report_pdf_response(ticket, incident_report, *, as_attachment=True):
    pdf_payload = _ticket_incident_report_docx_to_pdf_payload(ticket, incident_report)
    if not pdf_payload:
        return HttpResponse(
            "Incident report PDF export is unavailable. Install LibreOffice/soffice on the server so the signed Word template can be converted to PDF.",
            status=503,
            content_type="text/plain",
        )

    base_name = (
        (incident_report.incident_title or "").strip()
        or (incident_report.incident_id or "").strip()
        or "incident-report"
    )
    safe_name = get_valid_filename(base_name).replace(" ", "_") or "incident-report"
    output = BytesIO(pdf_payload)
    output.seek(0)
    return FileResponse(
        output,
        as_attachment=as_attachment,
        filename=f"{safe_name}.pdf",
        content_type="application/pdf",
    )


def _incident_response_template_image_response(cleaned_data, *, as_attachment=True, image_format="png"):
    normalized_format = "jpeg" if image_format in {"jpg", "jpeg"} else "png"
    extension = "jpg" if normalized_format == "jpeg" else "png"
    content_type = "image/jpeg" if normalized_format == "jpeg" else "image/png"
    image_payloads = _incident_response_template_image_payloads(cleaned_data, image_format=normalized_format)
    if not image_payloads:
        return HttpResponse(
            "Incident report image export is unavailable. Please contact support.",
            status=503,
            content_type="text/plain",
        )

    base_name = (
        (cleaned_data.get("incident_title") or "").strip()
        or (cleaned_data.get("incident_id") or "").strip()
        or "incident-response-template"
    )
    safe_name = get_valid_filename(base_name).replace(" ", "_")
    if len(image_payloads) == 1:
        output = BytesIO(image_payloads[0])
        output.seek(0)
        return FileResponse(
            output,
            as_attachment=as_attachment,
            filename=f"{safe_name}-page-1.{extension}",
            content_type=content_type,
        )

    archive = BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for index, image_payload in enumerate(image_payloads, start=1):
            zip_file.writestr(f"{safe_name}-page-{index}.{extension}", image_payload)
    archive.seek(0)
    return FileResponse(
        archive,
        as_attachment=as_attachment,
        filename=f"{safe_name}-{extension}-pages.zip",
        content_type="application/zip",
    )


def _incident_response_template_source_from_report(incident_report):
    field_names = [
        "reporting_employee_name",
        "reporting_employee_designation",
        "reporting_employee_email",
        "reporting_employee_contact",
        "date_of_report",
        "incident_title",
        "incident_id",
        "detected_at",
        "date_time_of_occurrence",
        "date_time_of_detection",
        "source_of_incident",
        "incident_location_ip",
        "incident_description",
        "reported_by",
        "incident_commander",
        "severity_level",
        "severity_choice",
        "current_status",
        "service_affected",
        "downtime_duration_minutes",
        "branch_impacted",
        "regulatory_impact",
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
        "date_of_notification",
        "review_root_cause_summary",
        "review_lessons_learned",
        "review_preventive_actions",
        "review_action_owners",
        "incident_registered_person",
        "incident_notified_person",
        "registered_signature",
        "notified_signature",
        "registered_signed_at",
        "notified_signed_at",
    ]
    source = {name: getattr(incident_report, name, "") for name in field_names}
    requester = getattr(getattr(incident_report, "ticket", None), "created_by", None)
    requester_name = incident_report_person_display(requester)
    source["incident_title"] = source.get("incident_title") or incident_report.display_title
    source["incident_id"] = source.get("incident_id") or incident_report.display_incident_reference
    source["evidence_ticket_case"] = source.get("evidence_ticket_case") or getattr(incident_report.ticket, "ticket_id", "")
    source["reporting_employee_name"] = source.get("reporting_employee_name") or source.get("reported_by") or requester_name
    source["reporting_employee_designation"] = source.get("reporting_employee_designation") or (getattr(requester, "position", "") or "").strip()
    source["reporting_employee_email"] = source.get("reporting_employee_email") or (getattr(requester, "email", "") or "").strip()
    source["reporting_employee_contact"] = source.get("reporting_employee_contact") or (getattr(requester, "phone_number", "") or "").strip()
    source["date_of_report"] = source.get("date_of_report") or incident_report.created_at
    source["severity_level"] = incident_report.get_severity_choice_display() or source.get("severity_level")
    source["service_affected"] = incident_report.get_service_affected_display()
    source["incident_registered_person"] = source.get("incident_registered_person") or incident_report.display_registered_person
    source["incident_notified_person"] = source.get("incident_notified_person") or incident_report.display_notified_person
    attachment_summary = _incident_report_attachment_notes_for_pdf(incident_report)
    if attachment_summary:
        source["evidence_attachments"] = "\n\n".join(
            part for part in [source.get("evidence_attachments"), attachment_summary] if part
        )
    notified_signoffs = []
    for signoff in _apply_incident_report_formal_signoff_labels(list(_ordered_notified_signoffs(incident_report))):
        person_name = signoff.display_person
        notified_signoffs.append(
            {
                "level": signoff.level,
                "formal_label": signoff.formal_label,
                "person_name": person_name,
                "signature_upload": signoff.snapshot_signature,
                "signed_at": signoff.signed_at,
            }
        )
    if notified_signoffs:
        source["notified_signoffs"] = notified_signoffs
    return source


def _copy_profile_signature_to_incident_report(incident_report, role, user):
    signature_field_name = f"{role}_signature"
    signed_at_field_name = f"{role}_signed_at"

    if not getattr(user, "signature_image", None):
        raise ValueError("No profile signature is available for this user.")

    existing_signature = getattr(incident_report, signature_field_name)
    if existing_signature:
        try:
            existing_signature.delete(save=False)
        except Exception:
            pass

    try:
        user.signature_image.open("rb")
        payload = user.signature_image.read()
    finally:
        try:
            user.signature_image.close()
        except Exception:
            pass

    if not payload:
        raise ValueError("No profile signature is available for this user.")

    source_name = os.path.basename(user.signature_image.name or f"{role}-signature.png")
    display_name = incident_report_person_display(user) or user.username
    safe_display_name = get_valid_filename(display_name.replace(" ", "_")) or role
    target_name = f"{role}_{safe_display_name}_{source_name}"
    getattr(incident_report, signature_field_name).save(target_name, ContentFile(payload), save=False)
    setattr(incident_report, signed_at_field_name, timezone.now())
    if role == "registered":
        incident_report.incident_registered_person = display_name
    else:
        incident_report.incident_notified_person = display_name


def _copy_profile_signature_to_incident_signoff(signoff, user):
    if not getattr(user, "signature_image", None):
        raise ValueError("No profile signature is available for this user.")

    existing_signature = getattr(signoff, "snapshot_signature", None)
    if existing_signature:
        try:
            existing_signature.delete(save=False)
        except Exception:
            pass

    try:
        user.signature_image.open("rb")
        payload = user.signature_image.read()
    finally:
        try:
            user.signature_image.close()
        except Exception:
            pass

    if not payload:
        raise ValueError("No profile signature is available for this user.")

    source_name = os.path.basename(user.signature_image.name or "notified-signature.png")
    display_name = incident_report_person_display(user) or user.username
    safe_display_name = get_valid_filename(display_name.replace(" ", "_")) or "notified"
    target_name = f"notified_level_{signoff.level}_{safe_display_name}_{source_name}"
    signoff.snapshot_signature.save(target_name, ContentFile(payload), save=False)
    signoff.signed_display_name = display_name
    signoff.signed_at = timezone.now()


def _auto_copy_registered_profile_signature(incident_report):
    registered_user = getattr(incident_report, "registered_user", None)
    if registered_user is None:
        return False, ""
    display_name = incident_report_person_display(registered_user) or registered_user.username
    if not getattr(registered_user, "signature_image", None):
        return False, f"{display_name} does not have a profile signature uploaded by admin."
    if getattr(incident_report, "registered_signature", None):
        return False, ""
    try:
        _copy_profile_signature_to_incident_report(incident_report, "registered", registered_user)
    except ValueError:
        return False, f"{display_name} does not have a readable profile signature."
    except Exception:
        return False, f"{display_name}'s profile signature could not be copied. Please re-upload the signature image."
    incident_report.save()
    return True, ""


@login_required
def incident_response_template(request):
    if request.method == "POST":
        form = IncidentResponseTemplateForm(request.POST, request.FILES)
        if form.is_valid():
            return _build_incident_response_template_pdf_response(
                form.cleaned_data,
                as_attachment=request.POST.get("pdf_mode") != "print",
            )
        messages.error(request, "Please review the template fields and try again.")
    else:
        form = IncidentResponseTemplateForm()

    return render(request, "docs/incident_response_template.html", {"form": form})


@login_required
def ticket_incident_report(request, ticket_id):
    ticket = get_object_or_404(
        Ticket.objects.select_related(
            "created_by",
            "assigned_to",
            "incident_report",
            "incident_report__created_by",
            "incident_report__updated_by",
            "incident_report__registered_user",
            "incident_report__notified_user",
            "incident_report__incident_commander_user",
            "remote_access_approval",
        ).prefetch_related("incident_report__signoffs__user"),
        id=ticket_id,
    )
    incident_report = _get_incident_report(ticket)
    if incident_report is not None and request.method == "GET" and _can_manage_incident_report(request.user, ticket):
        _auto_copy_registered_profile_signature(incident_report)
    notified_signoffs = _apply_incident_report_signoff_sequence(_ordered_notified_signoffs(incident_report), request.user)
    incident_attachments = _ordered_incident_report_attachments(incident_report)
    if not _can_access_incident_report(request.user, ticket, incident_report):
        messages.error(request, "You do not have access to this incident report.")
        return redirect("ticket_list")

    if _get_remote_access_approval(ticket) is not None or ticket.request_type != "incident":
        if _is_ticket_participant(request.user, ticket):
            messages.error(request, "Incident reports are only available for incident tickets.")
            return redirect("ticket_detail", ticket_id=ticket.id)
        messages.error(request, "Incident reports are only available for incident tickets.")
        return redirect("ticket_list")

    can_manage_incident_report = _can_manage_incident_report(request.user, ticket)
    can_sign_registered = bool(incident_report and incident_report.registered_user_id == request.user.id)
    can_sign_notified = bool(incident_report and incident_report.notified_user_id == request.user.id)
    signable_notified_signoff_ids = {signoff.id for signoff in notified_signoffs if signoff.can_sign_now}
    signature_status = _incident_report_signature_status(incident_report)
    incident_report_locked = _is_incident_report_locked(ticket, incident_report)
    signoff_formset_instance = incident_report or IncidentReport(ticket=ticket)
    incident_signoff_level_count = max(
        2,
        min(
            6,
            max((signoff.level or 0 for signoff in notified_signoffs), default=2),
        ),
    )

    if request.method == "POST":
        if not can_manage_incident_report:
            messages.error(request, "Only support users, the ticket requester, or the incident commander can create or update incident reports.")
            return redirect("ticket_incident_report", ticket_id=ticket.id)
        if incident_report_locked:
            messages.error(request, "This incident report has already been submitted and is no longer editable.")
            return redirect("ticket_incident_report", ticket_id=ticket.id)

        action = ((request.POST.get("action") or "save").strip().lower()) or "save"
        incident_signoff_level_count = _incident_report_signoff_level_count_from_request(request, incident_signoff_level_count)
        previous_signer_state = {
            "registered_user_id": getattr(incident_report, "registered_user_id", None),
            "notified_user_id": getattr(incident_report, "notified_user_id", None),
            "notified_assignments": {
                (signoff.user_id, signoff.level)
                for signoff in notified_signoffs
                if signoff.user_id
            },
        }
        form = IncidentReportForm(request.POST, request.FILES, instance=incident_report, ticket=ticket, user=request.user)
        notified_signoff_formset = IncidentReportNotifiedSignoffFormSet(
            request.POST,
            instance=signoff_formset_instance,
            prefix="notified_signoffs",
        )
        notified_signoff_formset.require_notified_user = action == "submit"
        notified_signoff_formset.required_level_count = incident_signoff_level_count
        evidence_uploads = [upload for upload in request.FILES.getlist("evidence_files") if getattr(upload, "name", "")]
        upload_errors = []
        if len(evidence_uploads) > INCIDENT_REPORT_ATTACHMENT_MAX_FILES:
            upload_errors.append(
                f"You can upload up to {INCIDENT_REPORT_ATTACHMENT_MAX_FILES} evidence files at once."
            )
        for upload in evidence_uploads:
            if upload.size and upload.size > settings.TICKET_ATTACHMENT_MAX_BYTES:
                upload_errors.append(
                    f"File too large (max {settings.TICKET_ATTACHMENT_MAX_BYTES} bytes): {upload.name}"
                )
        if upload_errors:
            for error_message in upload_errors:
                form.add_error(None, error_message)
        if form.is_valid() and notified_signoff_formset.is_valid():
            was_correction_request = bool(
                incident_report
                and getattr(incident_report, "correction_requested_at", None)
            )
            incident_report = form.save(commit=False)
            incident_report.ticket = ticket
            if not incident_report.created_by_id:
                incident_report.created_by = request.user
            incident_report.updated_by = request.user
            if was_correction_request:
                incident_report.correction_requested_by = None
                incident_report.correction_requested_at = None
                incident_report.correction_note = ""
            incident_report.save()
            notified_signoff_formset.instance = incident_report
            signoff_instances = notified_signoff_formset.save(commit=False)
            for deleted_signoff in notified_signoff_formset.deleted_objects:
                deleted_signoff.delete()
            for signoff in signoff_instances:
                if not signoff.level or not signoff.user_id:
                    continue
                signoff.incident_report = incident_report
                signoff.role = IncidentReportSignoff.ROLE_NOTIFIED
                signoff.save()
            registered_signature_copied, registered_signature_warning = _auto_copy_registered_profile_signature(incident_report)
            attachment_save_warning = None
            for upload in evidence_uploads:
                try:
                    IncidentReportAttachment.objects.create(
                        incident_report=incident_report,
                        file=upload,
                        original_name=(upload.name or "").strip(),
                        content_type=(getattr(upload, "content_type", "") or "").strip(),
                        size=getattr(upload, "size", 0) or 0,
                        uploaded_by=request.user,
                    )
                except (OperationalError, ProgrammingError):
                    attachment_save_warning = (
                        "Evidence file storage is not ready yet. Run migrations, then upload the files again."
                    )
                    break
            notified_signoffs = _apply_incident_report_signoff_sequence(_ordered_notified_signoffs(incident_report), request.user)
            incident_signoff_level_count = max(
                incident_signoff_level_count,
                max((signoff.level or 0 for signoff in notified_signoffs), default=incident_signoff_level_count),
            )
            incident_attachments = _ordered_incident_report_attachments(incident_report)
            if attachment_save_warning:
                messages.warning(request, attachment_save_warning)
            if registered_signature_warning:
                messages.warning(request, registered_signature_warning)
            signature_status = _incident_report_signature_status(incident_report)

            signer_email_count, signer_warnings = _notify_incident_report_signers(
                request,
                ticket,
                incident_report,
                request.user,
                previous_signer_state={} if was_correction_request else previous_signer_state,
            )
            for warning_message in signer_warnings:
                messages.warning(request, warning_message)

            if action == "submit":
                if not signature_status["complete"]:
                    messages.error(
                        request,
                        "Complete all required signatures before using Submit & Send: "
                        + ", ".join(signature_status["missing"]),
                    )
                    return redirect("ticket_incident_report", ticket_id=ticket.id)
                try:
                    submission_warnings = _send_incident_report_submission_email(
                        request,
                        ticket,
                        incident_report,
                        form.cleaned_data.get("cc_recipients", []),
                    )
                    incident_report.submitted_at = timezone.now()
                    incident_report.submitted_by = request.user
                    incident_report.save(update_fields=["submitted_at", "submitted_by", "updated_at"])
                    _mark_incident_ticket_resolved_after_submission(request, ticket, incident_report, request.user)
                    for warning_message in submission_warnings:
                        messages.warning(request, warning_message)
                    success_message = "Incident report submitted, emailed, and the linked ticket was marked resolved."
                    if registered_signature_copied:
                        success_message += " Registered user signature added automatically."
                    if signer_email_count:
                        success_message += f" {signer_email_count} signer notification email(s) sent."
                    messages.success(request, success_message)
                except Exception as e:
                    messages.warning(request, f"Incident report saved but email could not be sent: {str(e)}")
            else:
                success_message = "Incident report draft saved successfully."
                if registered_signature_copied:
                    success_message += " Registered user signature added automatically."
                if signer_email_count:
                    success_message += f" {signer_email_count} signer notification email(s) sent."
                messages.success(request, success_message)

            return redirect("ticket_incident_report", ticket_id=ticket.id)
        messages.error(request, "Could not save the incident report. Please review the form and try again.")
    else:
        if can_manage_incident_report and not incident_report_locked:
            form = IncidentReportForm(instance=incident_report, ticket=ticket, user=request.user)
            notified_signoff_formset = IncidentReportNotifiedSignoffFormSet(
                instance=signoff_formset_instance,
                prefix="notified_signoffs",
            )
            notified_signoff_formset.required_level_count = incident_signoff_level_count
        elif incident_report is not None:
            form = IncidentReportForm(instance=incident_report, ticket=ticket)
            for field in form.fields.values():
                field.disabled = True
            notified_signoff_formset = None
        else:
            form = None
            notified_signoff_formset = None

    has_notified_signoff_errors = bool(
        notified_signoff_formset
        and notified_signoff_formset.is_bound
        and (
            notified_signoff_formset.non_form_errors()
            or any(signoff_form.errors for signoff_form in notified_signoff_formset.forms)
        )
    )

    return render(
        request,
        "tickets/incident_report.html",
        {
            "ticket": ticket,
            "incident_report": incident_report,
            "notified_signoffs": notified_signoffs,
            "incident_attachments": incident_attachments,
            "form": form,
            "notified_signoff_formset": notified_signoff_formset,
            "has_notified_signoff_errors": has_notified_signoff_errors,
            "can_manage_incident_report": can_manage_incident_report,
            "incident_report_locked": incident_report_locked,
            "can_download_incident_report": incident_report is not None,
            "can_sign_registered": can_sign_registered,
            "can_sign_notified": can_sign_notified,
            "signable_notified_signoff_ids": signable_notified_signoff_ids,
            "incident_report_signature_complete": signature_status["complete"],
            "missing_incident_report_signatures": signature_status["missing"],
            "incident_signoff_level_count": incident_signoff_level_count,
            "incident_signoff_level_count_options": range(1, 7),
            "current_user_has_signature_image": bool(getattr(request.user, "signature_image", None)),
            "incident_attachment_max_files": INCIDENT_REPORT_ATTACHMENT_MAX_FILES,
            "incident_attachment_max_bytes": int(getattr(settings, "TICKET_ATTACHMENT_MAX_BYTES", 20 * 1024 * 1024)),
        },
    )


@login_required
def ticket_incident_report_download(request, ticket_id):
    ticket = get_object_or_404(
        Ticket.objects.select_related(
            "incident_report",
            "incident_report__registered_user",
            "incident_report__notified_user",
            "incident_report__incident_commander_user",
        ).prefetch_related(
            "incident_report__signoffs__user",
        ),
        id=ticket_id,
    )
    incident_report = _get_incident_report(ticket)
    if not _can_access_incident_report(request.user, ticket, incident_report):
        messages.error(request, "You do not have access to this incident report.")
        return redirect("ticket_list")

    if incident_report is None:
        messages.error(request, "No incident report has been created for this ticket yet.")
        return redirect("ticket_incident_report", ticket_id=ticket.id)

    output_format = _clean_query_value(request.GET.get("format")) or "docx"
    if output_format == "docx":
        return _build_ticket_incident_report_docx_response(
            ticket,
            incident_report,
            as_attachment=request.GET.get("print") != "1",
        )
    if output_format == "pdf":
        return _build_ticket_incident_report_pdf_response(
            ticket,
            incident_report,
            as_attachment=request.GET.get("print") != "1",
        )
    if output_format not in {"png", "jpg", "jpeg"}:
        output_format = "png"
    return _incident_response_template_image_response(
        _incident_response_template_source_from_report(incident_report),
        as_attachment=request.GET.get("print") != "1",
        image_format=output_format,
    )


@login_required
def ticket_incident_report_attachment_download(request, ticket_id, attachment_id):
    ticket = get_object_or_404(
        Ticket.objects.select_related("incident_report"),
        id=ticket_id,
    )
    incident_report = _get_incident_report(ticket)
    if not _can_access_incident_report(request.user, ticket, incident_report):
        messages.error(request, "You do not have access to this incident report.")
        return redirect("ticket_list")

    if incident_report is None:
        messages.error(request, "No incident report has been created for this ticket yet.")
        return redirect("ticket_incident_report", ticket_id=ticket.id)

    try:
        attachment = get_object_or_404(IncidentReportAttachment, incident_report=incident_report, id=attachment_id)
    except (OperationalError, ProgrammingError):
        messages.error(request, "Evidence attachment storage is not ready yet. Please run migrations first.")
        return redirect("ticket_incident_report", ticket_id=ticket.id)
    try:
        attachment.file.open("rb")
    except Exception:
        messages.error(request, "The requested evidence file could not be found.")
        return redirect("ticket_incident_report", ticket_id=ticket.id)

    response = FileResponse(
        attachment.file,
        as_attachment=True,
        filename=attachment.filename,
        content_type=attachment.content_type or "application/octet-stream",
    )
    if attachment.size:
        response["Content-Length"] = str(attachment.size)
    return response


@login_required
def ticket_incident_report_signature_view(request, ticket_id, signature_kind):
    ticket = get_object_or_404(
        Ticket.objects.select_related(
            "incident_report",
            "incident_report__registered_user",
            "incident_report__notified_user",
            "incident_report__incident_commander_user",
        ).prefetch_related("incident_report__signoffs__user"),
        id=ticket_id,
    )
    incident_report = _get_incident_report(ticket)
    if not _can_access_incident_report(request.user, ticket, incident_report):
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    if incident_report is None:
        return JsonResponse({"ok": False, "error": "File not found"}, status=404)

    signature_field_name = {
        "registered": "registered_signature",
        "notified": "notified_signature",
    }.get(signature_kind)
    if not signature_field_name:
        return JsonResponse({"ok": False, "error": "File not found"}, status=404)

    signature_field = getattr(incident_report, signature_field_name, None)
    if not signature_field:
        return JsonResponse({"ok": False, "error": "File not found"}, status=404)

    try:
        image_file = signature_field.open("rb")
    except Exception:
        return JsonResponse({"ok": False, "error": "File not found"}, status=404)

    filename = os.path.basename(signature_field.name or "signature").replace('"', "")
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    response = FileResponse(image_file, content_type=content_type)
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


@login_required
@require_POST
def ticket_incident_report_sign(request, ticket_id, role):
    ticket = get_object_or_404(
        Ticket.objects.select_related(
            "incident_report",
            "incident_report__registered_user",
            "incident_report__notified_user",
            "incident_report__incident_commander_user",
        ).prefetch_related("incident_report__signoffs__user"),
        id=ticket_id,
    )
    incident_report = _get_incident_report(ticket)
    if incident_report is None:
        messages.error(request, "No incident report has been created for this ticket yet.")
        return redirect("ticket_incident_report", ticket_id=ticket.id)

    if not _can_access_incident_report(request.user, ticket, incident_report):
        messages.error(request, "You do not have access to this incident report.")
        return redirect("ticket_list")
    role_config = {
        "registered": ("registered_user_id", "Incident Registered By"),
        "notified": ("notified_user_id", "Incident Notified User"),
    }.get(role)
    if role_config is None:
        messages.error(request, "Unknown signature role.")
        return redirect("ticket_incident_report", ticket_id=ticket.id)

    user_field_name, role_label = role_config
    if getattr(incident_report, user_field_name) != request.user.id:
        messages.error(request, f"Only the selected {role_label.lower()} can sign this section.")
        return redirect("ticket_incident_report", ticket_id=ticket.id)

    if not getattr(request.user, "signature_image", None):
        messages.error(
            request,
            "Your signature is not uploaded on your user profile yet. Please ask an admin to add it first.",
        )
        return redirect("ticket_incident_report", ticket_id=ticket.id)

    try:
        _copy_profile_signature_to_incident_report(incident_report, role, request.user)
    except ValueError:
        messages.error(
            request,
            "Your signature is not uploaded on your user profile yet. Please ask an admin to add it first.",
        )
        return redirect("ticket_incident_report", ticket_id=ticket.id)

    incident_report.updated_by = request.user
    incident_report.save()
    messages.success(request, f"{role_label} signature captured successfully.")
    return redirect("ticket_incident_report", ticket_id=ticket.id)


@login_required
def ticket_incident_report_signoff_signature_view(request, ticket_id, signoff_id):
    ticket = get_object_or_404(
        Ticket.objects.select_related("incident_report"),
        id=ticket_id,
    )
    incident_report = _get_incident_report(ticket)
    if not _can_access_incident_report(request.user, ticket, incident_report):
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)
    if incident_report is None:
        return JsonResponse({"ok": False, "error": "File not found"}, status=404)

    signoff = get_object_or_404(
        incident_report.signoffs.select_related("user"),
        pk=signoff_id,
        role=IncidentReportSignoff.ROLE_NOTIFIED,
    )
    if not signoff.snapshot_signature:
        return JsonResponse({"ok": False, "error": "File not found"}, status=404)

    try:
        image_file = signoff.snapshot_signature.open("rb")
    except Exception:
        return JsonResponse({"ok": False, "error": "File not found"}, status=404)

    filename = os.path.basename(signoff.snapshot_signature.name or "signature").replace('"', "")
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    response = FileResponse(image_file, content_type=content_type)
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


@login_required
@require_POST
def ticket_incident_report_signoff_sign(request, ticket_id, signoff_id):
    ticket = get_object_or_404(
        Ticket.objects.select_related("incident_report"),
        id=ticket_id,
    )
    incident_report = _get_incident_report(ticket)
    if incident_report is None:
        messages.error(request, "No incident report has been created for this ticket yet.")
        return redirect("ticket_incident_report", ticket_id=ticket.id)

    if not _can_access_incident_report(request.user, ticket, incident_report):
        messages.error(request, "You do not have access to this incident report.")
        return redirect("ticket_list")
    signoff = get_object_or_404(
        incident_report.signoffs.select_related("user"),
        pk=signoff_id,
        role=IncidentReportSignoff.ROLE_NOTIFIED,
    )
    if signoff.user_id != request.user.id:
        messages.error(request, "Only the selected notified user can sign this section.")
        return redirect("ticket_incident_report", ticket_id=ticket.id)

    signoffs = _apply_incident_report_signoff_sequence(_ordered_notified_signoffs(incident_report), request.user)
    current_signoff = next((item for item in signoffs if item.id == signoff.id), None)
    formal_label = getattr(current_signoff, "formal_label", "Assigned reviewer")
    if current_signoff is not None and current_signoff.waiting_for_prior_signoff:
        messages.error(
            request,
            f"{formal_label} can sign only after earlier reviewer sign-off is complete: {current_signoff.waiting_for_label}.",
        )
        return redirect("ticket_incident_report", ticket_id=ticket.id)
    if getattr(signoff, "snapshot_signature", None):
        messages.info(request, f"{formal_label} signature is already captured.")
        return redirect("ticket_incident_report", ticket_id=ticket.id)

    if not getattr(request.user, "signature_image", None):
        messages.error(
            request,
            "Your signature is not uploaded on your user profile yet. Please ask an admin to add it first.",
        )
        return redirect("ticket_incident_report", ticket_id=ticket.id)

    try:
        _copy_profile_signature_to_incident_signoff(signoff, request.user)
    except ValueError:
        messages.error(
            request,
            "Your signature is not uploaded on your user profile yet. Please ask an admin to add it first.",
        )
        return redirect("ticket_incident_report", ticket_id=ticket.id)

    signoff.save()
    incident_report.updated_by = request.user
    incident_report.save(update_fields=["updated_by", "updated_at"])
    messages.success(request, f"{formal_label} signature captured successfully.")
    return redirect("ticket_incident_report", ticket_id=ticket.id)


@login_required
@require_POST
def ticket_incident_report_signoff_reject(request, ticket_id, signoff_id):
    ticket = get_object_or_404(
        Ticket.objects.select_related(
            "created_by",
            "incident_report",
            "incident_report__created_by",
            "incident_report__incident_commander_user",
        ),
        id=ticket_id,
    )
    incident_report = _get_incident_report(ticket)
    if incident_report is None:
        messages.error(request, "No incident report has been created for this ticket yet.")
        return redirect("ticket_incident_report", ticket_id=ticket.id)

    if not _can_access_incident_report(request.user, ticket, incident_report):
        messages.error(request, "You do not have access to this incident report.")
        return redirect("ticket_list")

    signoff = get_object_or_404(
        incident_report.signoffs.select_related("user"),
        pk=signoff_id,
        role=IncidentReportSignoff.ROLE_NOTIFIED,
    )
    if signoff.user_id != request.user.id:
        messages.error(request, "Only the selected reviewer or approver can request correction.")
        return redirect("ticket_incident_report", ticket_id=ticket.id)

    signoffs = _apply_incident_report_signoff_sequence(_ordered_notified_signoffs(incident_report), request.user)
    current_signoff = next((item for item in signoffs if item.id == signoff.id), None)
    formal_label = getattr(current_signoff, "formal_label", "Assigned reviewer")
    if current_signoff is not None and current_signoff.waiting_for_prior_signoff:
        messages.error(
            request,
            f"{formal_label} can request correction only after earlier reviewer sign-off is complete: {current_signoff.waiting_for_label}.",
        )
        return redirect("ticket_incident_report", ticket_id=ticket.id)

    note = (request.POST.get("correction_note") or "").strip()
    if not note:
        messages.error(request, "Enter a correction note before requesting correction.")
        return redirect("ticket_incident_report", ticket_id=ticket.id)

    signoff.formal_label = formal_label
    incident_report.correction_requested_by = request.user
    incident_report.correction_requested_at = timezone.now()
    incident_report.correction_note = note
    incident_report.submitted_at = None
    incident_report.submitted_by = None
    incident_report.updated_by = request.user
    incident_report.save(
        update_fields=[
            "correction_requested_by",
            "correction_requested_at",
            "correction_note",
            "submitted_at",
            "submitted_by",
            "updated_by",
            "updated_at",
        ]
    )
    _clear_incident_signoffs_from_level(incident_report, signoff.level)
    if ticket.status == "resolved":
        ticket.status = "in_progress"
        ticket.resolved_by = None
        ticket.resolved_note = ""
        ticket.save(update_fields=["status", "resolved_by", "resolved_note", "updated_at"])

    _notify_incident_report_correction_requested(request, ticket, incident_report, signoff, note)
    messages.success(request, f"Correction requested from the incident owner at {formal_label}.")
    return redirect("ticket_incident_report", ticket_id=ticket.id)


@login_required
@user_passes_test(_is_support_user)
def tech_docs_upload(request):
    if request.method == "POST":
        uploads = request.FILES.getlist("files")
        titles = [(value or "").strip() for value in request.POST.getlist("titles")]
        descriptions = [(value or "").strip() for value in request.POST.getlist("descriptions")]
        visibility = (request.POST.get("visibility") or TechnicalDocument.VISIBILITY_PUBLIC).strip()
        allowed_raw = request.POST.get("allowed_users") or ""
        selected_branch_ids = [(value or "").strip() for value in request.POST.getlist("branches")]
        selected_department_ids = [(value or "").strip() for value in request.POST.getlist("departments")]
        form_values = {
            "visibility": visibility,
            "allowed_users": allowed_raw,
            "branches": [value for value in selected_branch_ids if value],
            "departments": [value for value in selected_department_ids if value],
        }

        allowed_visibility_values = {
            TechnicalDocument.VISIBILITY_PUBLIC,
            TechnicalDocument.VISIBILITY_BRANCH,
            TechnicalDocument.VISIBILITY_DEPARTMENT,
            TechnicalDocument.VISIBILITY_RESTRICTED,
            TechnicalDocument.VISIBILITY_SUPPORT_ONLY,
        }
        if visibility not in allowed_visibility_values:
            visibility = TechnicalDocument.VISIBILITY_PUBLIC
            form_values["visibility"] = visibility

        selected_branches = []
        if visibility in {
            TechnicalDocument.VISIBILITY_BRANCH,
            TechnicalDocument.VISIBILITY_DEPARTMENT,
        }:
            selected_branches, missing_branch_ids = _selected_tech_doc_branches(selected_branch_ids)
            if missing_branch_ids:
                messages.error(request, "Please choose a valid branch.")
                return render(
                    request,
                    "docs/tech_docs_upload.html",
                    _tech_docs_upload_context(form_values),
                )

        selected_departments = []
        if visibility == TechnicalDocument.VISIBILITY_DEPARTMENT:
            if not form_values["departments"]:
                messages.error(request, "Please choose at least one department for department visibility.")
                return render(
                    request,
                    "docs/tech_docs_upload.html",
                    _tech_docs_upload_context(form_values),
                )
            selected_departments, missing_department_ids = _selected_tech_doc_departments(
                selected_department_ids
            )
            if missing_department_ids:
                messages.error(request, "Please choose a valid department.")
                return render(
                    request,
                    "docs/tech_docs_upload.html",
                    _tech_docs_upload_context(form_values),
                )

        if not uploads:
            messages.error(request, "Please select at least one PDF or Excel file to upload.")
            return render(
                request,
                "docs/tech_docs_upload.html",
                _tech_docs_upload_context(form_values),
            )

        allowed_users = []
        if visibility == TechnicalDocument.VISIBILITY_RESTRICTED:
            tokens = []
            for item in allowed_raw.replace(",", "\n").splitlines():
                value = item.strip()
                if value:
                    tokens.append(value)

            if not tokens:
                messages.error(
                    request, "Restricted visibility requires at least one allowed username/email."
                )
                return render(
                    request,
                    "docs/tech_docs_upload.html",
                    _tech_docs_upload_context(form_values),
                )

            usernames = [item for item in tokens if "@" not in item]
            emails = [item.lower() for item in tokens if "@" in item]

            query = Q(pk__in=[])
            for username in usernames:
                query |= Q(username__iexact=username)
            for email in emails:
                query |= Q(email__iexact=email)
            allowed_users = list(CustomUser.objects.filter(query).only("id", "username", "email"))

            if not allowed_users:
                messages.error(request, "No matching users found for the allowed list.")
                return render(
                    request,
                    "docs/tech_docs_upload.html",
                    _tech_docs_upload_context(form_values),
                )

            found_usernames = {user.username.lower() for user in allowed_users if user.username}
            found_emails = {user.email.lower() for user in allowed_users if user.email}
            missing = []
            for token in tokens:
                token_lower = token.lower()
                if "@" in token:
                    if token_lower not in found_emails:
                        missing.append(token)
                else:
                    if token_lower not in found_usernames:
                        missing.append(token)
            if missing:
                messages.warning(
                    request,
                    "Some users were not found and were ignored: " + ", ".join(sorted(set(missing))),
                )

        max_bytes = int(getattr(settings, "TICKET_ATTACHMENT_MAX_BYTES", 20 * 1024 * 1024))
        errors = []
        normalized_titles = []
        normalized_descriptions = []
        for idx, upload in enumerate(uploads):
            filename = os.path.basename(getattr(upload, "name", "") or "upload")
            ext = os.path.splitext(filename)[1].lower()
            if ext not in TECH_DOC_ALLOWED_EXTENSIONS:
                errors.append(f"Only PDF and Excel files are allowed: {filename}")
            if upload.size and upload.size > max_bytes:
                errors.append(f"File too large (max {max_bytes} bytes): {filename}")

            title = titles[idx] if idx < len(titles) else ""
            if not title:
                title = os.path.splitext(filename)[0] or filename
            normalized_titles.append(title)
            normalized_descriptions.append(descriptions[idx] if idx < len(descriptions) else "")

        if errors:
            for msg in errors:
                messages.error(request, msg)
            return render(
                request,
                "docs/tech_docs_upload.html",
                _tech_docs_upload_context(form_values),
            )

        try:
            minio_cfg = get_minio_config()
            s3 = get_s3_client()
        except Exception:
            messages.error(request, "Document storage is not configured.")
            return render(
                request,
                "docs/tech_docs_upload.html",
                _tech_docs_upload_context(form_values),
            )

        created_count = 0
        for upload, title, description in zip(
            uploads, normalized_titles, normalized_descriptions, strict=True
        ):
            raw_name = getattr(upload, "name", "") or "document.pdf"
            object_key = TechnicalDocument.build_object_key(raw_name)
            filename = os.path.basename(raw_name)
            ext = os.path.splitext(filename)[1].lower()
            content_type = getattr(upload, "content_type", "") or TECH_DOC_ALLOWED_CONTENT_TYPES.get(
                ext, "application/octet-stream"
            )

            s3.upload_fileobj(
                upload,
                minio_cfg.bucket,
                object_key,
                ExtraArgs={"ContentType": content_type},
            )

            document = TechnicalDocument.objects.create(
                title=title,
                description=description,
                visibility=visibility,
                object_key=object_key,
                filename=filename,
                content_type=content_type,
                size=getattr(upload, "size", 0) or 0,
                uploaded_by=request.user,
            )
            if visibility == TechnicalDocument.VISIBILITY_RESTRICTED and allowed_users:
                document.allowed_users.set(allowed_users)
            if visibility in {
                TechnicalDocument.VISIBILITY_BRANCH,
                TechnicalDocument.VISIBILITY_DEPARTMENT,
            } and selected_branches:
                document.allowed_branches.set(selected_branches)
            if visibility == TechnicalDocument.VISIBILITY_DEPARTMENT and selected_departments:
                document.allowed_departments.set(selected_departments)
            created_count += 1

        messages.success(request, f"Uploaded {created_count} document(s).")
        return redirect("tech_docs")

    return render(request, "docs/tech_docs_upload.html", _tech_docs_upload_context())


@login_required
def tech_doc_download(request, doc_id):
    document = get_object_or_404(TechnicalDocument, id=doc_id)
    if not _can_view_tech_doc(request.user, document):
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)
    try:
        minio_cfg = get_minio_config()
        s3 = get_s3_client()
    except Exception:
        return JsonResponse({"ok": False, "error": "Document storage is not configured"}, status=500)

    try:
        obj = s3.get_object(Bucket=minio_cfg.bucket, Key=document.object_key)
    except Exception as exc:
        if ClientError is not None and isinstance(exc, ClientError):
            return JsonResponse({"ok": False, "error": "File not found"}, status=404)
        raise

    body = obj["Body"]
    response = StreamingHttpResponse(
        _stream_s3_body(body),
        content_type=document.content_type or "application/octet-stream",
    )
    filename = (document.filename or "document.pdf").replace('"', "")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    if "ContentLength" in obj:
        response["Content-Length"] = str(obj["ContentLength"])
    return response


@login_required
def tech_doc_view(request, doc_id):
    document = get_object_or_404(TechnicalDocument, id=doc_id)
    if not _can_view_tech_doc(request.user, document):
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)
    try:
        minio_cfg = get_minio_config()
        s3 = get_s3_client()
    except Exception:
        return JsonResponse({"ok": False, "error": "Document storage is not configured"}, status=500)

    try:
        obj = s3.get_object(Bucket=minio_cfg.bucket, Key=document.object_key)
    except Exception as exc:
        if ClientError is not None and isinstance(exc, ClientError):
            return JsonResponse({"ok": False, "error": "File not found"}, status=404)
        raise

    extension = _tech_doc_extension(document)
    if extension in TECH_DOC_EXCEL_PREVIEWABLE_EXTENSIONS:
        workbook_bytes = _read_s3_body_bytes(obj["Body"])
        try:
            sheets = _excel_preview_sheets(workbook_bytes)
            preview_error = ""
        except ValueError as exc:
            sheets = []
            preview_error = str(exc)
        return render(
            request,
            "docs/tech_doc_excel_preview.html",
            {
                "document": document,
                "download_url": reverse("tech_doc_download", args=[document.id]),
                "preview_error": preview_error,
                "sheets": sheets,
            },
        )

    body = obj["Body"]
    response = StreamingHttpResponse(
        _stream_s3_body(body),
        content_type=document.content_type or "application/octet-stream",
    )
    filename = (document.filename or "document.pdf").replace('"', "")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    if "ContentLength" in obj:
        response["Content-Length"] = str(obj["ContentLength"])
    return response


@login_required
@user_passes_test(_is_support_user)
def portal_flash_upload(request):
    recent_flashes = PortalFlashAnnouncement.objects.select_related("uploaded_by").order_by("-created_at")[:5]
    default_start_at = timezone.localtime(timezone.now()).replace(second=0, microsecond=0)
    default_end_at = default_start_at + timedelta(days=1)

    if request.method == "POST":
        title = (request.POST.get("title") or "").strip()
        message = (request.POST.get("message") or "").strip()
        category = (request.POST.get("category") or PortalFlashAnnouncement.CATEGORY_IT).strip()
        starts_at = _parse_local_datetime_input(request.POST.get("starts_at"))
        ends_at = _parse_local_datetime_input(request.POST.get("ends_at"))
        image = request.FILES.get("image")
        errors = []

        if not title:
            errors.append("Please enter a title for the login flash.")
        if category not in {PortalFlashAnnouncement.CATEGORY_IT, PortalFlashAnnouncement.CATEGORY_BANK}:
            errors.append("Please select a valid news category.")
        if not starts_at or not ends_at:
            errors.append("Please enter both the start and end date/time.")
        elif ends_at <= starts_at:
            errors.append("End date/time must be later than the start date/time.")
        if not image:
            errors.append("Please select a JPEG image to upload.")
        else:
            filename = os.path.basename(getattr(image, "name", "") or "upload")
            ext = os.path.splitext(filename)[1].lower()
            if ext not in {".jpg", ".jpeg"}:
                errors.append(f"Only JPEG images are allowed: {filename}")
            max_bytes = int(getattr(settings, "TICKET_ATTACHMENT_MAX_BYTES", 20 * 1024 * 1024))
            if getattr(image, "size", 0) and image.size > max_bytes:
                errors.append(f"File too large (max {max_bytes} bytes): {filename}")

        if errors:
            for error in errors:
                messages.error(request, error)
            return render(
                request,
                "tickets/portal_flash_upload.html",
                {
                    "recent_flashes": recent_flashes,
                    "default_start_at": default_start_at.strftime("%Y-%m-%dT%H:%M"),
                    "default_end_at": default_end_at.strftime("%Y-%m-%dT%H:%M"),
                    "submitted_title": title,
                    "submitted_message": message,
                    "submitted_category": category,
                    "submitted_starts_at": request.POST.get("starts_at") or "",
                    "submitted_ends_at": request.POST.get("ends_at") or "",
                },
            )

        PortalFlashAnnouncement.objects.create(
            category=category,
            title=title,
            message=message,
            image=image,
            uploaded_by=request.user,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        messages.success(request, "Login flash image uploaded successfully.")
        return redirect("portal_flash_upload")

    return render(
        request,
        "tickets/portal_flash_upload.html",
        {
            "recent_flashes": recent_flashes,
            "default_start_at": default_start_at.strftime("%Y-%m-%dT%H:%M"),
            "default_end_at": default_end_at.strftime("%Y-%m-%dT%H:%M"),
            "submitted_title": "",
            "submitted_message": "",
            "submitted_category": PortalFlashAnnouncement.CATEGORY_IT,
            "submitted_starts_at": default_start_at.strftime("%Y-%m-%dT%H:%M"),
            "submitted_ends_at": default_end_at.strftime("%Y-%m-%dT%H:%M"),
        },
    )


@login_required
def portal_flash_image_view(request, announcement_id):
    announcement = get_object_or_404(PortalFlashAnnouncement, id=announcement_id)
    if not announcement.image:
        return JsonResponse({"ok": False, "error": "File not found"}, status=404)

    try:
        image_file = announcement.image.open("rb")
    except Exception:
        return JsonResponse({"ok": False, "error": "File not found"}, status=404)

    filename = os.path.basename(announcement.image.name or "portal-flash.jpg").replace('"', "")
    content_type = mimetypes.guess_type(filename)[0] or "image/jpeg"
    response = FileResponse(image_file, content_type=content_type)
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


@login_required
@require_POST
def tech_doc_delete(request, doc_id):
    if not request.user.has_perm("tickets.delete_technicaldocument"):
        messages.error(request, "You do not have permission to delete documents.")
        return redirect("tech_docs")

    document = get_object_or_404(TechnicalDocument, id=doc_id)
    title = document.title or document.filename or "document"
    document.delete()
    messages.success(request, f"Deleted: {title}")
    return redirect("tech_docs")


@login_required
def ticket_detail(request, ticket_id):
    ticket = get_object_or_404(
        Ticket.objects.select_related(
            "created_by",
            "assigned_to",
            "resolved_by",
            "closed_by",
            "incident_report",
            "incident_report__created_by",
            "incident_report__updated_by",
            "incident_report__registered_user",
            "incident_report__notified_user",
            "incident_report__incident_commander_user",
            "remote_access_approval",
            "remote_access_approval__recommender",
            "remote_access_approval__recommended_by",
            "remote_access_approval__approver",
            "remote_access_approval__decided_by",
        ),
        id=ticket_id,
    )
    if not _is_ticket_participant(request.user, ticket):
        messages.error(request, "You do not have access to this ticket.")
        return redirect("ticket_list")
    return render(request, 'tickets/ticket_detail.html', _build_ticket_detail_context(request, ticket))


@login_required
@require_POST
def ticket_chat_privacy_update(request, ticket_id):
    ticket = get_object_or_404(Ticket, id=ticket_id)
    if not _is_ticket_participant(request.user, ticket):
        messages.error(request, "You do not have access to this ticket.")
        return redirect("ticket_list")
    if not can_manage_ticket_chat_privacy(request.user, ticket):
        messages.error(request, "You do not have permission to manage chat privacy for this ticket.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    form = TicketChatPrivacyForm(request.POST, ticket=ticket, user=request.user)
    if form.is_valid():
        form.save()
        messages.success(request, "Chat privacy updated successfully.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    messages.error(request, "Could not update chat privacy. Please review the form and try again.")
    return render(request, 'tickets/ticket_detail.html', _build_ticket_detail_context(request, ticket, form))


@login_required
@require_POST
def ticket_chat_mark_seen(request, ticket_id):
    ticket = get_object_or_404(Ticket, id=ticket_id)
    if not can_access_ticket_chat(request.user, ticket):
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    _mark_ticket_chat_seen(ticket, request.user)
    return JsonResponse({"ok": True})


@login_required
@require_POST
def ticket_claim(request, ticket_id):
    ticket = get_object_or_404(Ticket, id=ticket_id)
    if _get_remote_access_approval(ticket) is not None:
        messages.error(request, "Remote access approval requests cannot be claimed as support tickets.")
        return redirect("ticket_detail", ticket_id=ticket.id)
    if not _is_department_ticket_member(request.user, ticket):
        messages.error(request, "You do not have permission to take ownership of this ticket.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    if ticket.created_by_id == request.user.id and ticket.assigned_to_id != request.user.id:
        messages.error(request, "You cannot take ownership of a ticket you created.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    if ticket.assigned_to_id == request.user.id:
        messages.info(request, "You already own this ticket.")
        return redirect("ticket_update", ticket_id=ticket.id)

    if ticket.assigned_to_id:
        messages.warning(request, "This ticket is already assigned to someone else.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    ticket.assigned_to = request.user
    ticket._assignment_actor_id = request.user.id
    ticket.save()
    messages.success(request, "You now own this ticket.")
    return redirect("ticket_update", ticket_id=ticket.id)


@login_required
@require_POST
def ticket_attachment_upload(request, ticket_id):
    ticket = get_object_or_404(Ticket, id=ticket_id)
    if not can_access_ticket_chat(request.user, ticket):
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)
    if is_ticket_chat_locked(ticket):
        return JsonResponse({"ok": False, "error": ticket_chat_locked_message(ticket)}, status=400)

    uploads = request.FILES.getlist("file") or request.FILES.getlist("files")
    if not uploads:
        return JsonResponse({"ok": False, "error": "No file uploaded"}, status=400)
    if len(uploads) > TICKET_CHAT_ATTACHMENT_MAX_FILES:
        return JsonResponse(
            {
                "ok": False,
                "error": f"You can upload up to {TICKET_CHAT_ATTACHMENT_MAX_FILES} attachments at once.",
            },
            status=400,
        )

    for upload in uploads:
        if upload.size and upload.size > settings.TICKET_ATTACHMENT_MAX_BYTES:
            return JsonResponse(
                {
                    "ok": False,
                    "error": f"File too large (max {settings.TICKET_ATTACHMENT_MAX_BYTES} bytes): {upload.name}",
                },
                status=400,
            )

    try:
        minio_cfg = get_minio_config()
        s3 = get_s3_client()
    except Exception:
        return JsonResponse({"ok": False, "error": "Attachment storage is not configured"}, status=500)

    events = []
    for upload in uploads:
        object_key = TicketMessageAttachment.build_object_key(ticket.id, upload.name)
        content_type = getattr(upload, "content_type", "") or "application/octet-stream"

        s3.upload_fileobj(
            upload,
            minio_cfg.bucket,
            object_key,
            ExtraArgs={"ContentType": content_type},
        )

        message = TicketMessage.objects.create(
            ticket=ticket,
            author=request.user,
            body=f"Attachment uploaded: {upload.name}",
        )
        attachment = TicketMessageAttachment.objects.create(
            ticket=ticket,
            message=message,
            uploaded_by=request.user,
            object_key=object_key,
            filename=upload.name,
            content_type=content_type,
            size=upload.size or 0,
        )
        events.append(_build_ticket_attachment_event(ticket, message, attachment, request.user.username))

    channel_layer = get_channel_layer()
    if channel_layer:
        try:
            for event in events:
                async_to_sync(channel_layer.group_send)(
                    f"ticket_chat_{ticket.id}",
                    {"type": "chat_message", **event},
                )

            notify_body = events[0]["body"] if len(events) == 1 else f"{len(events)} attachments uploaded"
            notify_payload = build_chat_notification_payload(ticket, request.user, notify_body)
            for target_id in get_chat_notification_target_ids(ticket, request.user.id):
                async_to_sync(channel_layer.group_send)(
                    f"user_notify_{target_id}",
                    {"type": "notify", "payload": notify_payload},
                )
        except Exception:
            pass

    return JsonResponse(
        {
            "ok": True,
            "event": events[0] if len(events) == 1 else None,
            "events": events,
        }
    )


@login_required
@require_POST
def ticket_chat_message_delete(request, ticket_id, message_id):
    ticket = get_object_or_404(Ticket, id=ticket_id)
    if not can_access_ticket_chat(request.user, ticket):
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)
    if is_ticket_chat_locked(ticket):
        return JsonResponse({"ok": False, "error": ticket_chat_locked_message(ticket)}, status=400)

    message = get_object_or_404(
        TicketMessage.objects.select_related("attachment"),
        id=message_id,
        ticket=ticket,
    )
    if not _can_delete_ticket_message(request.user, message):
        return JsonResponse(
            {"ok": False, "error": "You can delete only your own chat messages."},
            status=403,
        )

    attachment = message.attachment if hasattr(message, "attachment") else None
    object_keys = [attachment.object_key] if attachment and attachment.object_key else []
    _try_delete_minio_objects(object_keys)

    deleted_message_id = message.id
    message.delete()

    channel_layer = get_channel_layer()
    if channel_layer:
        try:
            async_to_sync(channel_layer.group_send)(
                f"ticket_chat_{ticket.id}",
                {"type": "chat_message_deleted", "id": deleted_message_id},
            )
        except Exception:
            pass

    return JsonResponse({"ok": True, "deleted_message_id": deleted_message_id})


@login_required
def ticket_attachment_download(request, ticket_id, attachment_id):
    ticket = get_object_or_404(Ticket, id=ticket_id)
    if not can_access_ticket_chat(request.user, ticket):
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    attachment = get_object_or_404(TicketMessageAttachment, id=attachment_id, ticket=ticket)

    try:
        minio_cfg = get_minio_config()
        s3 = get_s3_client()
    except Exception:
        return JsonResponse({"ok": False, "error": "Attachment storage is not configured"}, status=500)

    try:
        obj = s3.get_object(Bucket=minio_cfg.bucket, Key=attachment.object_key)
    except Exception as exc:
        if ClientError is not None and isinstance(exc, ClientError):
            return JsonResponse({"ok": False, "error": "File not found"}, status=404)
        raise

    body = obj["Body"]

    def stream():
        try:
            while True:
                chunk = body.read(1024 * 256)
                if not chunk:
                    break
                yield chunk
        finally:
            body.close()

    response = StreamingHttpResponse(
        stream(),
        content_type=attachment.content_type or "application/octet-stream",
    )
    filename = (attachment.filename or "download").replace('"', "")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    if "ContentLength" in obj:
        response["Content-Length"] = str(obj["ContentLength"])
    return response


@login_required
def ticket_attachment_view(request, ticket_id, attachment_id):
    ticket = get_object_or_404(Ticket, id=ticket_id)
    if not can_access_ticket_chat(request.user, ticket):
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    attachment = get_object_or_404(TicketMessageAttachment, id=attachment_id, ticket=ticket)

    try:
        minio_cfg = get_minio_config()
        s3 = get_s3_client()
    except Exception:
        return JsonResponse({"ok": False, "error": "Attachment storage is not configured"}, status=500)

    try:
        obj = s3.get_object(Bucket=minio_cfg.bucket, Key=attachment.object_key)
    except Exception as exc:
        if ClientError is not None and isinstance(exc, ClientError):
            return JsonResponse({"ok": False, "error": "File not found"}, status=404)
        raise

    body = obj["Body"]

    def stream():
        try:
            while True:
                chunk = body.read(1024 * 256)
                if not chunk:
                    break
                yield chunk
        finally:
            body.close()

    response = StreamingHttpResponse(
        stream(),
        content_type=attachment.content_type or "application/octet-stream",
    )
    filename = (attachment.filename or "download").replace('"', "")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    if "ContentLength" in obj:
        response["Content-Length"] = str(obj["ContentLength"])
    return response


@login_required
def ticket_image_view(request, ticket_id):
    ticket = get_object_or_404(Ticket, id=ticket_id)
    if not _is_ticket_participant(request.user, ticket):
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    if not ticket.image:
        return JsonResponse({"ok": False, "error": "File not found"}, status=404)

    try:
        image_file = ticket.image.open("rb")
    except Exception:
        return JsonResponse({"ok": False, "error": "File not found"}, status=404)

    filename = os.path.basename(ticket.image.name or "ticket-image").replace('"', "")
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    response = FileResponse(image_file, content_type=content_type)
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


@login_required
def ticket_image_download(request, ticket_id):
    ticket = get_object_or_404(Ticket, id=ticket_id)
    if not _is_ticket_participant(request.user, ticket):
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    if not ticket.image:
        return JsonResponse({"ok": False, "error": "File not found"}, status=404)

    try:
        image_file = ticket.image.open("rb")
    except Exception:
        return JsonResponse({"ok": False, "error": "File not found"}, status=404)

    filename = os.path.basename(ticket.image.name or "ticket-image").replace('"', "")
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    response = FileResponse(image_file, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
@user_passes_test(_is_support_user)
def support_dashboard(request):
    filters = _get_support_filters(request)
    tickets = _apply_support_filters(
        Ticket.objects.select_related("created_by", "assigned_to"),
        filters,
    )
    recent_tickets = _attach_support_ticket_display_flags(tickets.order_by("-created_at")[:10], request.user)
    agent_workload = _build_agent_workload(
        tickets,
        filters=filters,
        queue_url_name="support_queue",
        limit=10,
    )
    context = {
        "query": filters["q"],
        "selected_status": filters["status"],
        "selected_priority": filters["priority"],
        "selected_department": filters["department"],
        "selected_branch": filters["branch"],
        "selected_status_group": filters["status_group"],
        "created_by_username": filters["created_by_username"],
        "assigned_to_username": filters["assigned_to_username"],
        "selected_assignment_scope": filters["assignment_scope"],
        "date_from": filters["date_from"],
        "date_to": filters["date_to"],
        "support_department_options": _support_department_filter_options(filters["department"]),
        "support_branch_options": _support_branch_filter_options(filters["branch"], filters["department"]),
        "has_active_filters": _has_active_support_filters(filters),
        "support_queue_url": _build_support_url("support_queue", filters),
        "support_cbs_access_url": _build_support_url("support_cbs_access_requests", filters),
        "support_incident_url": _build_support_url("support_incident_tickets", filters),
        "new_assigned_tickets_url": _build_support_url(
            "support_queue",
            filters,
            status_group="new",
            assignment_scope="assigned",
            status="",
        ),
        "new_unassigned_tickets_url": _build_support_url(
            "support_queue",
            filters,
            status_group="new",
            assignment_scope="unassigned",
            status="",
        ),
        "in_progress_tickets_url": _build_support_url("support_queue", filters, status_group="in_progress", status=""),
        "resolved_tickets_url": _build_support_url("support_queue", filters, status_group="resolved", status=""),
        "closed_tickets_url": _build_support_url("support_queue", filters, status_group="closed", status=""),
        "total_tickets": tickets.count(),
        "new_assigned_tickets": _count_support_status_group_by_assignment(tickets, "new", True),
        "new_unassigned_tickets": _count_support_status_group_by_assignment(tickets, "new", False),
        "in_progress_tickets": _count_support_status_group(tickets, "in_progress"),
        "resolved_tickets": _count_support_status_group(tickets, "resolved"),
        "closed_tickets": _count_support_status_group(tickets, "closed"),
        "my_workload_tickets": _agent_workload_total_for_user(agent_workload, request.user),
        "cbs_access_tickets": Ticket.objects.filter(
            request_type__in=("cbs_access_ho", "cbs_access_branch")
        ).count(),
        "incident_tickets": Ticket.objects.filter(request_type="incident").count(),
        "recent_tickets": recent_tickets,
        "agent_workload": agent_workload,
    }
    return render(request, "tickets/support_dashboard.html", context)


@login_required
def agent_workload_view(request):
    if not is_agent_workload_view_enabled():
        messages.error(request, "Agent workload view is currently unavailable.")
        if _is_support_user(request.user):
            return redirect("support_dashboard")
        return redirect("ticket_list")

    agent_workload = _build_agent_workload(
        Ticket.objects.select_related("created_by", "assigned_to").filter(remote_access_approval__isnull=True),
        limit=None,
    )
    context = {
        "agent_workload": agent_workload,
        "total_agents": len(agent_workload),
        "total_workload": sum(item["total"] for item in agent_workload),
    }
    return render(request, "tickets/agent_workload_view.html", context)


@login_required
@user_passes_test(_is_support_user)
def support_users(request):
    active_users, online_window_minutes = _get_currently_logged_in_users(request)
    context = {
        "total_users": CustomUser.objects.count(),
        "currently_logged_in_total": len(active_users),
        "currently_logged_in_users": active_users,
        "online_window_minutes": online_window_minutes,
    }
    return render(request, "tickets/support_users.html", context)


@login_required
@user_passes_test(_is_support_user)
def support_queue(request):
    filters = _get_support_filters(request)
    tickets = _apply_support_filters(
        Ticket.objects.select_related("created_by", "assigned_to").order_by("-created_at"),
        filters,
    )
    tickets = _attach_support_ticket_display_flags(tickets, request.user)
    return render(
        request,
        "tickets/support_queue.html",
        {
            "page_browser_title": "Support Queue",
            "page_title": "Support Queue",
            "page_description": "Search by Ticket ID, priority, creator username, assigned username, or a date range.",
            "clear_url": reverse("support_queue"),
            "show_assigned_to_filter": True,
            "selected_department": filters["department"],
            "selected_branch": filters["branch"],
            "support_department_options": _support_department_filter_options(filters["department"]),
            "support_branch_options": _support_branch_filter_options(filters["branch"], filters["department"]),
            "tickets": tickets,
            "query": filters["q"],
            "selected_status": filters["status"],
            "selected_priority": filters["priority"],
            "selected_status_group": filters["status_group"],
            "created_by_username": filters["created_by_username"],
            "assigned_to_username": filters["assigned_to_username"],
            "selected_assignment_scope": filters["assignment_scope"],
            "date_from": filters["date_from"],
            "date_to": filters["date_to"],
            "has_active_filters": _has_active_support_filters(filters),
        },
    )


@login_required
@user_passes_test(_is_support_user)
def support_cbs_access_requests(request):
    filters = _get_support_filters(request)
    tickets = _apply_support_filters(
        Ticket.objects.select_related(
            "created_by",
            "assigned_to",
            "remote_access_approval",
            "remote_access_approval__recommender",
            "remote_access_approval__recommended_by",
            "remote_access_approval__approver",
            "remote_access_approval__decided_by",
        )
        .filter(request_type__in=("cbs_access_ho", "cbs_access_branch"))
        .order_by("-created_at"),
        filters,
        include_approval_tickets=True,
        include_special_requests=True,
    )
    tickets = _attach_support_ticket_display_flags(tickets, request.user)
    return render(
        request,
        "tickets/support_queue.html",
        {
            "page_browser_title": "CBS Access Requests",
            "page_title": "CBS Access Requests",
            "page_description": "CBS request approval chain, approved documents, assignment, resolution, and closure.",
            "clear_url": reverse("support_cbs_access_requests"),
            "show_assigned_to_filter": True,
            "selected_department": filters["department"],
            "selected_branch": filters["branch"],
            "support_department_options": _support_department_filter_options(filters["department"]),
            "support_branch_options": _support_branch_filter_options(filters["branch"], filters["department"]),
            "tickets": tickets,
            "query": filters["q"],
            "selected_status": filters["status"],
            "selected_priority": filters["priority"],
            "selected_status_group": filters["status_group"],
            "created_by_username": filters["created_by_username"],
            "assigned_to_username": filters["assigned_to_username"],
            "selected_assignment_scope": filters["assignment_scope"],
            "date_from": filters["date_from"],
            "date_to": filters["date_to"],
            "has_active_filters": _has_active_support_filters(filters),
        },
    )


@login_required
@user_passes_test(_is_support_user)
def support_incident_tickets(request):
    filters = _get_support_filters(request)
    tickets = _apply_support_filters(
        Ticket.objects.select_related("created_by", "assigned_to", "incident_report")
        .filter(request_type="incident")
        .order_by("-created_at"),
        filters,
        include_special_requests=True,
    )
    tickets = _attach_support_ticket_display_flags(tickets, request.user)
    return render(
        request,
        "tickets/support_queue.html",
        {
            "page_browser_title": "Incident Tickets",
            "page_title": "Incident Tickets",
            "page_description": "Incident tickets, response reports, sign-off status, assignment, resolution, and closure.",
            "clear_url": reverse("support_incident_tickets"),
            "show_assigned_to_filter": True,
            "selected_department": filters["department"],
            "selected_branch": filters["branch"],
            "support_department_options": _support_department_filter_options(filters["department"]),
            "support_branch_options": _support_branch_filter_options(filters["branch"], filters["department"]),
            "tickets": tickets,
            "query": filters["q"],
            "selected_status": filters["status"],
            "selected_priority": filters["priority"],
            "selected_status_group": filters["status_group"],
            "created_by_username": filters["created_by_username"],
            "assigned_to_username": filters["assigned_to_username"],
            "selected_assignment_scope": filters["assignment_scope"],
            "date_from": filters["date_from"],
            "date_to": filters["date_to"],
            "has_active_filters": _has_active_support_filters(filters),
        },
    )


@login_required
@user_passes_test(_is_support_user)
def support_department_tickets(request):
    filters = _get_support_filters(request)
    filters["assignment_scope"] = ""
    filters["assigned_to_username"] = ""
    tickets = _apply_support_filters(_department_ticket_support_queryset(request.user), filters)
    solved_statuses = {"resolved", "closed", "cancelled_duplicate"}
    unassigned_tickets = _attach_support_ticket_display_flags(
        tickets.filter(assigned_to__isnull=True).exclude(status__in=solved_statuses),
        request.user,
    )
    assigned_tickets = _attach_support_ticket_display_flags(
        tickets.filter(assigned_to__isnull=False).exclude(status__in=solved_statuses),
        request.user,
    )
    solved_tickets = _attach_support_ticket_display_flags(
        tickets.filter(status__in=solved_statuses),
        request.user,
    )
    return render(
        request,
        "tickets/support_department_tickets.html",
        {
            "page_browser_title": "Department Tickets",
            "page_title": "Department Tickets",
            "page_description": "Tickets for your department, split into unassigned queue items, assigned work, and solved history.",
            "clear_url": reverse("support_department_tickets"),
            "show_assigned_to_filter": False,
            "unassigned_tickets": unassigned_tickets,
            "assigned_tickets": assigned_tickets,
            "solved_tickets": solved_tickets,
            "query": filters["q"],
            "selected_status": filters["status"],
            "selected_priority": filters["priority"],
            "selected_status_group": filters["status_group"],
            "created_by_username": filters["created_by_username"],
            "assigned_to_username": "",
            "selected_assignment_scope": "",
            "date_from": filters["date_from"],
            "date_to": filters["date_to"],
            "has_active_filters": _has_active_support_filters(filters),
        },
    )


@csrf_exempt
@login_required
def ticket_update(request, ticket_id):
    if request.method == "POST" and request.content_type and request.content_type.startswith("multipart/form-data"):
        _use_request_only_upload_handlers(request)
    return _ticket_update_protected(request, ticket_id)


@csrf_protect
def _ticket_update_protected(request, ticket_id):
    ticket = get_object_or_404(Ticket, id=ticket_id)
    remote_access_approval = _get_remote_access_approval(ticket)
    cbs_access_support_workflow = (
        _approval_request_kind(ticket) == "CBS Access"
        and remote_access_approval is not None
        and remote_access_approval.status == RemoteAccessApproval.STATUS_APPROVED
    )
    if remote_access_approval is not None and not cbs_access_support_workflow:
        messages.error(request, "Approval requests can be updated through the support workflow only after final CBS approval.")
        return redirect("ticket_detail", ticket_id=ticket.id)
    can_manage = _is_support_user(request.user) or ticket.assigned_to_id == request.user.id
    if not can_manage:
        messages.error(request, "You do not have permission to update this ticket.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    is_support = _is_support_user(request.user)
    FormClass = TicketUpdateForm if is_support else TicketAssigneeUpdateForm
    if request.method == "POST":
        previous_assigned_to_id = ticket.assigned_to_id
        previous_effective_assigned_to = ticket.display_assignee
        previous_effective_assigned_to_id = getattr(previous_effective_assigned_to, "id", None)
        previous_status = ticket.status
        ticket._assignment_actor_id = request.user.id
        form = FormClass(request.POST, request.FILES, instance=ticket, user=request.user)
        if form.is_valid():
            new_status = form.cleaned_data.get("status")
            if previous_status == "resolved" and new_status not in {"resolved", "closed"}:
                messages.error(request, "Resolved tickets cannot be reopened from the ticket panel. Use the admin panel if a superadmin must change it.")
                return render(request, "tickets/ticket_update.html", {"ticket": ticket, "form": form})
            if previous_status == "closed" and new_status != "closed":
                messages.error(request, "Closed tickets cannot be reopened from the ticket panel. Use the admin panel if a superadmin must change it.")
                return render(request, "tickets/ticket_update.html", {"ticket": ticket, "form": form})

            if new_status == "resolved":
                if not previous_assigned_to_id:
                    messages.error(request, "Assign this ticket before marking it as resolved.")
                    return render(request, "tickets/ticket_update.html", {"ticket": ticket, "form": form})

                if previous_assigned_to_id != request.user.id:
                    messages.error(request, "Only the assigned agent can mark this ticket as resolved.")
                    return render(request, "tickets/ticket_update.html", {"ticket": ticket, "form": form})
                if ticket.request_type == "incident":
                    incident_resolution_blockers = _incident_report_resolution_blockers(ticket)
                    if incident_resolution_blockers:
                        messages.error(request, " ".join(incident_resolution_blockers))
                        return render(request, "tickets/ticket_update.html", {"ticket": ticket, "form": form})
            if new_status == "closed" and previous_status != "resolved":
                messages.error(request, "A ticket can only be closed after it has been resolved.")
                return render(request, "tickets/ticket_update.html", {"ticket": ticket, "form": form})
            can_close_ticket = _is_admin_user(request.user) or previous_effective_assigned_to_id == request.user.id
            if new_status == "closed" and not can_close_ticket:
                messages.error(request, "Only an admin user or the ticket assignee can mark this ticket as closed.")
                return render(request, "tickets/ticket_update.html", {"ticket": ticket, "form": form})

            status_note = (form.cleaned_data.get("status_note") or "").strip()
            status_email_uploads = form.cleaned_data.get("status_email_attachments") or []
            status_cc_emails = form.cleaned_data.get("status_cc_emails")
            ticket = form.save(commit=False)
            if previous_status != ticket.status and ticket.status == "resolved":
                ticket.resolved_note = status_note
                ticket.resolved_by = request.user
                if form.add_prefix("status_cc_emails") in form.data:
                    ticket.cc_emails = status_cc_emails
            elif previous_status != ticket.status and ticket.status == "closed":
                ticket.closed_note = status_note
            if previous_status != ticket.status and ticket.status == "closed":
                ticket.closed_by = request.user
            ticket.save()

            if previous_assigned_to_id != ticket.assigned_to_id and ticket.assigned_to_id:
                _notify_user(
                    ticket.assigned_to_id,
                    {
                        "kind": "ticket_assigned",
                        "level": "info",
                        "title": "Ticket assigned",
                        "message": f"{ticket.ticket_id}: {ticket.subject}",
                        "url": reverse("ticket_detail", args=[ticket.id]),
                        "ticket_id": ticket.id,
                        "ticket_code": ticket.ticket_id,
                        "assigned_by": request.user.get_username(),
                    },
                )

            if previous_status != ticket.status and ticket.created_by_id and ticket.created_by_id != request.user.id:
                level = "info"
                if ticket.status in {"in_progress", "waiting_on_user", "waiting_on_third_party"}:
                    level = "warning"
                elif ticket.status in {"resolved", "closed"}:
                    level = "success"

                _notify_user(
                    ticket.created_by_id,
                    {
                        "kind": "ticket_status",
                        "level": level,
                        "title": "Ticket status updated",
                        "message": f"{ticket.ticket_id} is now {ticket.get_status_display()}",
                        "url": reverse("ticket_detail", args=[ticket.id]),
                        "ticket_id": ticket.id,
                        "ticket_code": ticket.ticket_id,
                        "status": ticket.status,
                        "updated_by": request.user.get_username(),
                    },
                )

            if previous_status != ticket.status and ticket.status == "resolved":
                requester_email = (getattr(ticket.created_by, "email", "") or "").strip()
                if requester_email:
                    resolved_at = ticket.resolved_at
                    if resolved_at:
                        resolved_at = timezone.localtime(resolved_at).strftime("%b %d, %Y %H:%M")
                    else:
                        resolved_at = "-"

                    close_token = _make_ticket_close_token(ticket)
                    close_url = request.build_absolute_uri(
                        reverse("ticket_close_via_email", args=[ticket.id, close_token])
                    )
                    ticket_url = request.build_absolute_uri(reverse("ticket_detail", args=[ticket.id]))
                    auto_close_days = int(getattr(settings, "TICKET_AUTO_CLOSE_DAYS", 10))
                    status_email_attachments = _build_email_attachments(status_email_uploads)

                    mail_lines = [
                        f"Hello {ticket.created_by.get_full_name() or ticket.created_by.username},",
                        "",
                        "Your helpdesk ticket has been marked as Resolved.",
                        "",
                        f"Ticket Number: {ticket.ticket_id}",
                        f"Subject: {ticket.subject}",
                        f"Request Type: {ticket.get_request_type_display()}",
                        f"Department: {ticket.department or '-'}",
                        f"Impact: {ticket.get_impact_display()}",
                        f"Urgency: {ticket.get_urgency_display()}",
                        f"Priority: {ticket.get_priority_display()}",
                        f"Status: {ticket.get_status_display()}",
                        f"Resolved At: {resolved_at}",
                        f"Resolved By: {request.user.username} ({request.user.email or '-'})",
                        f"Resolution Details:\n{ticket.resolved_note or '-'}",
                    ]
                    if status_email_attachments:
                        attachment_count = len(status_email_attachments)
                        noun = "attachment" if attachment_count == 1 else "attachments"
                        verb = "is" if attachment_count == 1 else "are"
                        mail_lines.extend(["", f"{attachment_count} {noun} {verb} included with this email."])
                    mail_lines.extend(
                        [
                            "",
                            f"Close Ticket Link (Requester Confirmation):\n{close_url}",
                            f"Note: If you do not confirm closure, the ticket will be auto-closed after {auto_close_days} days.",
                            "",
                            f"Ticket Link:\n{ticket_url}",
                            "",
                            "If the issue is not resolved, please reply back or contact IT support.",
                        ]
                    )

                    mail_subject = f"Ticket Resolved: {ticket.ticket_id}"
                    mail_body = "\n".join(mail_lines)
                    try:
                        _send_email_message(
                            mail_subject,
                            mail_body,
                            [requester_email],
                            cc_list=ticket.cc_email_list,
                            email_attachments=status_email_attachments,
                        )
                    except Exception:
                        messages.warning(request, "Ticket resolved, but requester email could not be sent.")
                else:
                    messages.warning(request, "Ticket resolved, but the requester has no email set.")

            if is_support and previous_assigned_to_id != ticket.assigned_to_id and ticket.assigned_to_id:
                _send_assignment_email(request, ticket, request.user, "Ticket updated")
            messages.success(request, "Ticket updated successfully.")
            if is_support:
                return redirect("support_queue")
            return redirect("ticket_detail", ticket_id=ticket.id)
    else:
        form = FormClass(instance=ticket, user=request.user)

    return render(request, "tickets/ticket_update.html", {"ticket": ticket, "form": form})


@login_required
def ticket_close_via_email(request, ticket_id, token):
    ticket = get_object_or_404(Ticket, id=ticket_id)
    if request.user.id != ticket.created_by_id:
        messages.error(request, "Only the ticket requester can close this ticket.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    max_age_seconds = int(getattr(settings, "TICKET_CLOSE_LINK_MAX_AGE_SECONDS", 60 * 60 * 24 * 14))
    try:
        token_ok = _validate_ticket_close_token(ticket, token, max_age_seconds=max_age_seconds)
    except SignatureExpired:
        messages.error(request, "This close link has expired. Please contact IT support.")
        return redirect("ticket_detail", ticket_id=ticket.id)
    except BadSignature:
        messages.error(request, "Invalid close link.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    if not token_ok:
        messages.error(request, "Invalid close link.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    if ticket.status == "closed":
        messages.info(request, "Ticket is already closed.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    if ticket.status == "cancelled_duplicate":
        messages.info(request, "This ticket has been cancelled/marked as duplicate.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    if ticket.status != "resolved":
        messages.warning(request, "This ticket is not marked as resolved yet.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    ticket.status = "closed"
    ticket.closed_by = request.user
    ticket.save()
    messages.success(request, "Ticket closed. Thank you for confirming.")
    return redirect("ticket_detail", ticket_id=ticket.id)
