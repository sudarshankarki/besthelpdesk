import base64
import hashlib
import hmac
import json
import mimetypes
import os
import time
from datetime import date
from io import BytesIO
from urllib.parse import urlencode, urlsplit

from django.contrib.auth.decorators import login_required
from django.contrib.auth.decorators import user_passes_test
from django.contrib import messages
from django.conf import settings
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.core.files.uploadhandler import FileUploadHandler
from django.core.mail import EmailMessage, send_mail
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.http import FileResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.db.models import Count, Max, Q
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_POST

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .chat_rules import is_ticket_chat_locked, ticket_chat_locked_message
from .minio import get_minio_config, get_s3_client
from accounts.models import CustomUser
from accounts.utils import get_outgoing_from_email
from .models import (
    TechnicalDocument,
    Ticket,
    TicketChatReadState,
    TicketMessage,
    TicketMessageAttachment,
    can_access_ticket_chat,
    can_manage_ticket_chat_privacy,
)
from .forms import TicketAssigneeUpdateForm, TicketChatPrivacyForm, TicketForm, TicketUpdateForm
from .notifications import build_chat_notification_payload, get_chat_notification_target_ids
from .purge import _try_delete_minio_objects

try:
    from botocore.exceptions import ClientError  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    ClientError = None  # type: ignore


TICKET_CHAT_ATTACHMENT_MAX_FILES = 5
SUPPORT_STATUS_GROUPS = {
    "new": ("new", "acknowledged"),
    "in_progress": ("in_progress", "waiting_on_user", "waiting_on_third_party"),
    "resolved": ("resolved",),
    "closed": ("closed", "cancelled_duplicate"),
}


class RequestOnlyMemoryFileUploadHandler(FileUploadHandler):
    """Keep uploaded files in memory so close-email attachments are never persisted on disk."""

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


def _get_support_filters(request):
    filters = {
        "q": _clean_query_value(request.GET.get("q")),
        "status": _clean_query_value(request.GET.get("status")),
        "status_group": _clean_query_value(request.GET.get("status_group")),
        "created_by_username": _clean_query_value(request.GET.get("created_by_username")),
        "assigned_to_username": _clean_query_value(request.GET.get("assigned_to_username")),
        "assignment_scope": _clean_query_value(request.GET.get("assignment_scope")),
        "date_from": _clean_query_value(request.GET.get("date_from")),
        "date_to": _clean_query_value(request.GET.get("date_to")),
    }
    if filters["status_group"] not in SUPPORT_STATUS_GROUPS:
        filters["status_group"] = ""
    if filters["assignment_scope"] not in {"", "unassigned", "assigned"}:
        filters["assignment_scope"] = ""
    date_from = _parse_filter_date(filters["date_from"])
    date_to = _parse_filter_date(filters["date_to"])
    if date_from and date_to and date_from > date_to:
        filters["date_from"], filters["date_to"] = filters["date_to"], filters["date_from"]
    return filters


def _apply_support_filters(queryset, filters):
    if filters["status_group"]:
        queryset = queryset.filter(status__in=SUPPORT_STATUS_GROUPS[filters["status_group"]])
    if filters["status"]:
        queryset = queryset.filter(status=filters["status"])
    if filters["q"]:
        queryset = queryset.filter(ticket_id__icontains=filters["q"])
    if filters["created_by_username"]:
        queryset = queryset.filter(created_by__username__icontains=filters["created_by_username"])
    if filters["assignment_scope"] == "unassigned":
        queryset = queryset.filter(assigned_to__isnull=True)
    elif filters["assignment_scope"] == "assigned":
        queryset = queryset.filter(assigned_to__isnull=False)
    elif filters["assigned_to_username"]:
        queryset = queryset.filter(assigned_to__username__icontains=filters["assigned_to_username"])

    date_from = _parse_filter_date(filters["date_from"])
    if date_from:
        queryset = queryset.filter(created_at__date__gte=date_from)

    date_to = _parse_filter_date(filters["date_to"])
    if date_to:
        queryset = queryset.filter(created_at__date__lte=date_to)

    return queryset


def _has_active_support_filters(filters):
    return any(filters.values())


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
    return Q(department__iexact=department) & (
        Q(branch__iexact=branch) | (Q(branch="") & Q(created_by__branch__iexact=branch))
    )


def _is_department_ticket_member(user, ticket):
    if not getattr(user, "is_authenticated", False):
        return False
    user_department = _normalize_department(_user_department_name(user))
    ticket_department = _normalize_department(getattr(ticket, "department", ""))
    user_branch = _normalize_branch(_user_branch_name(user))
    ticket_branch = _normalize_branch(_ticket_branch_name(ticket))
    return bool(
        user_department
        and ticket_department
        and user_branch
        and ticket_branch
        and user_department == ticket_department
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
    if ticket.created_by_id == user.id:
        return False
    return True


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
    return (
        f"Hello {assignee_name},\n\n"
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
        f"Open Ticket:\n{_ticket_detail_url(request, ticket)}\n"
    )


def _build_new_ticket_email_body(request, ticket):
    assigned_to = getattr(ticket.assigned_to, "username", "") or "Unassigned"
    description = (ticket.description or "").strip() or "-"
    return (
        f"Hello,\n\n"
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


def _send_email_message(subject, body, recipient_list, email_attachments=None):
    email = EmailMessage(
        subject=subject,
        body=body,
        from_email=get_outgoing_from_email(),
        to=recipient_list,
    )
    for attachment in email_attachments or []:
        email.attach(*attachment)
    email.send(fail_silently=False)


def _send_assignment_email(request, ticket, assigned_by, action_label, email_attachments=None):
    assignee_email = (getattr(ticket.assigned_to, "email", "") or "").strip()
    if not assignee_email:
        messages.warning(request, "Ticket assigned, but the assignee has no email set.")
        return

    mail_subject = f"Ticket Assigned: {ticket.ticket_id}"
    mail_body = _build_assignment_email_body(request, ticket, assigned_by)
    try:
        _send_email_message(mail_subject, mail_body, [assignee_email], email_attachments=email_attachments)
    except Exception:
        messages.warning(request, f"{action_label}, but assignment email could not be sent.")


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


def _can_view_tech_doc(user, document: TechnicalDocument) -> bool:
    if _is_support_user(user):
        return True

    visibility = getattr(document, "visibility", TechnicalDocument.VISIBILITY_PUBLIC)
    if visibility == TechnicalDocument.VISIBILITY_PUBLIC:
        return True
    if visibility == TechnicalDocument.VISIBILITY_SUPPORT_ONLY:
        return False
    if visibility == TechnicalDocument.VISIBILITY_RESTRICTED:
        return document.allowed_users.filter(id=user.id).exists()

    return False


def _is_ticket_participant(user, ticket):
    return (
        user.is_staff
        or user.is_superuser
        or user.is_itsupport
        or ticket.created_by_id == user.id
        or ticket.assigned_to_id == user.id
        or _is_department_ticket_member(user, ticket)
    )


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


def _build_ticket_detail_context(request, ticket, chat_privacy_form=None):
    can_update_ticket = _is_support_user(request.user) or ticket.assigned_to_id == request.user.id
    can_view_chat = can_access_ticket_chat(request.user, ticket)
    webrtc_ice_servers = getattr(settings, "WEBRTC_ICE_SERVERS", []) or _build_same_host_webrtc_ice_servers(request)
    webrtc_ice_servers = _with_runtime_turn_credentials(webrtc_ice_servers, request.user)

    if can_view_chat:
        _mark_ticket_chat_seen(ticket, request.user)
        chat_messages = TicketMessage.objects.filter(ticket=ticket).select_related("author", "attachment")
    else:
        chat_messages = TicketMessage.objects.none()

    if chat_privacy_form is None and can_manage_ticket_chat_privacy(request.user, ticket):
        chat_privacy_form = TicketChatPrivacyForm(ticket=ticket, user=request.user)

    return {
        'ticket': ticket,
        'chat_messages': chat_messages,
        'assignment_logs': ticket.assignment_logs.all().select_related("assigned_to", "assigned_by"),
        'can_claim_ticket': _can_claim_department_ticket(request.user, ticket),
        'can_update_ticket': can_update_ticket,
        'can_view_requester_info': can_update_ticket or _is_department_ticket_member(request.user, ticket),
        'chat_locked': is_ticket_chat_locked(ticket),
        'chat_locked_message': ticket_chat_locked_message(ticket),
        'can_view_chat': can_view_chat,
        'can_manage_chat_privacy': can_manage_ticket_chat_privacy(request.user, ticket),
        'chat_privacy_form': chat_privacy_form,
        'chat_attachment_batch_limit': TICKET_CHAT_ATTACHMENT_MAX_FILES,
        'webrtc_ice_servers_json': json.dumps(webrtc_ice_servers),
    }


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


@login_required
def create_ticket(request):
    if request.method == 'POST':
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
                    return render(request, "tickets/create_ticket.html", {"form": form})

            ticket = form.save(commit=False)
            ticket.created_by = request.user
            assign_user_id = getattr(form, "_assign_user_id", None)
            if assign_user_id:
                ticket.assigned_to_id = assign_user_id
                ticket._assignment_actor_id = request.user.id
            ticket.save()

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
                    email_attachments=email_attachments,
                )
            except Exception:
                messages.warning(request, "Ticket created, but notification email could not be sent.")
            messages.success(request, 'Ticket created successfully!')
            return redirect('ticket_list')
    else:
        form = TicketForm(user=request.user)

    return render(request, 'tickets/create_ticket.html', {'form': form})


@login_required
def ticket_list(request):
    query = _clean_query_value(request.GET.get("q"))
    status = _clean_query_value(request.GET.get("status"))
    scope = _clean_query_value(request.GET.get("scope"))
    date_from = _clean_query_value(request.GET.get("date_from"))
    date_to = _clean_query_value(request.GET.get("date_to"))
    allowed_statuses = {value for value, _label in Ticket.TICKET_STATUS}
    if status not in allowed_statuses:
        status = ""
    parsed_date_from = _parse_filter_date(date_from)
    parsed_date_to = _parse_filter_date(date_to)
    if parsed_date_from and parsed_date_to and parsed_date_from > parsed_date_to:
        date_from, date_to = date_to, date_from
    base_queryset = Ticket.objects.select_related("created_by", "assigned_to")
    if _is_support_user(request.user):
        tickets = base_queryset.all()
    else:
        tickets = base_queryset.filter(
            Q(created_by=request.user) | Q(assigned_to=request.user) | _department_ticket_q(request.user)
        ).distinct()

    if scope == "created_by_me":
        tickets = tickets.filter(created_by=request.user)

    if query:
        tickets = tickets.filter(ticket_id__icontains=query)
    if status:
        tickets = tickets.filter(status=status)
    if date_from:
        parsed = _parse_filter_date(date_from)
        if parsed:
            tickets = tickets.filter(created_at__date__gte=parsed)
    if date_to:
        parsed = _parse_filter_date(date_to)
        if parsed:
            tickets = tickets.filter(created_at__date__lte=parsed)
    tickets = _attach_ticket_chat_flags(tickets.order_by("-created_at"), request.user)
    for ticket in tickets:
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
            'status_choices': Ticket.TICKET_STATUS,
            'date_from': date_from,
            'date_to': date_to,
            'has_active_filters': bool(query or status or scope or date_from or date_to),
        },
    )


@login_required
def tech_docs(request):
    documents = TechnicalDocument.objects.select_related("uploaded_by").order_by("-created_at")
    if not _is_support_user(request.user):
        documents = documents.filter(
            Q(visibility=TechnicalDocument.VISIBILITY_PUBLIC)
            | Q(
                visibility=TechnicalDocument.VISIBILITY_RESTRICTED,
                allowed_users=request.user,
            )
        ).distinct()
    return render(
        request,
        "docs/tech_docs.html",
        {"documents": documents, "can_upload": _is_support_user(request.user)},
    )


@login_required
@user_passes_test(_is_support_user)
def tech_docs_upload(request):
    if request.method == "POST":
        uploads = request.FILES.getlist("files")
        titles = [(value or "").strip() for value in request.POST.getlist("titles")]
        descriptions = [(value or "").strip() for value in request.POST.getlist("descriptions")]
        visibility = (request.POST.get("visibility") or TechnicalDocument.VISIBILITY_PUBLIC).strip()
        allowed_raw = request.POST.get("allowed_users") or ""

        allowed_visibility_values = {
            TechnicalDocument.VISIBILITY_PUBLIC,
            TechnicalDocument.VISIBILITY_RESTRICTED,
            TechnicalDocument.VISIBILITY_SUPPORT_ONLY,
        }
        if visibility not in allowed_visibility_values:
            visibility = TechnicalDocument.VISIBILITY_PUBLIC

        if not uploads:
            messages.error(request, "Please select at least one PDF file to upload.")
            return render(request, "docs/tech_docs_upload.html")

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
                return render(request, "docs/tech_docs_upload.html")

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
                return render(request, "docs/tech_docs_upload.html")

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
            if ext != ".pdf":
                errors.append(f"Only PDF files are allowed: {filename}")
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
            return render(request, "docs/tech_docs_upload.html")

        try:
            minio_cfg = get_minio_config()
            s3 = get_s3_client()
        except Exception:
            messages.error(request, "Document storage is not configured.")
            return render(request, "docs/tech_docs_upload.html")

        created_count = 0
        for upload, title, description in zip(
            uploads, normalized_titles, normalized_descriptions, strict=True
        ):
            object_key = TechnicalDocument.build_object_key(getattr(upload, "name", "document.pdf"))
            filename = os.path.basename(getattr(upload, "name", "") or "document.pdf")
            content_type = getattr(upload, "content_type", "") or "application/pdf"

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
            created_count += 1

        messages.success(request, f"Uploaded {created_count} document(s).")
        return redirect("tech_docs")

    return render(request, "docs/tech_docs_upload.html")


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
        content_type=document.content_type or "application/octet-stream",
    )
    filename = (document.filename or "document.pdf").replace('"', "")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    if "ContentLength" in obj:
        response["Content-Length"] = str(obj["ContentLength"])
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
    ticket = get_object_or_404(Ticket, id=ticket_id)
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
    recent_tickets = _attach_ticket_chat_flags(tickets.order_by("-created_at")[:10], request.user)
    agent_workload = list(
        tickets.filter(assigned_to__isnull=False)
        .values("assigned_to__id", "assigned_to__username")
        .annotate(total=Count("id"))
        .order_by("-total", "assigned_to__username")[:5]
    )
    for item in agent_workload:
        item["queue_url"] = _build_support_url(
            "support_queue",
            filters,
            assigned_to_username=item["assigned_to__username"],
        )
    context = {
        "query": filters["q"],
        "selected_status": filters["status"],
        "selected_status_group": filters["status_group"],
        "created_by_username": filters["created_by_username"],
        "assigned_to_username": filters["assigned_to_username"],
        "selected_assignment_scope": filters["assignment_scope"],
        "date_from": filters["date_from"],
        "date_to": filters["date_to"],
        "has_active_filters": _has_active_support_filters(filters),
        "support_queue_url": _build_support_url("support_queue", filters),
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
        "my_assigned_tickets": tickets.filter(assigned_to=request.user).count(),
        "recent_tickets": recent_tickets,
        "agent_workload": agent_workload,
    }
    return render(request, "tickets/support_dashboard.html", context)


@login_required
@user_passes_test(_is_support_user)
def support_queue(request):
    filters = _get_support_filters(request)
    tickets = _apply_support_filters(
        Ticket.objects.select_related("created_by", "assigned_to").order_by("-created_at"),
        filters,
    )
    tickets = _attach_ticket_chat_flags(tickets, request.user)
    return render(
        request,
        "tickets/support_queue.html",
        {
            "tickets": tickets,
            "query": filters["q"],
            "selected_status": filters["status"],
            "selected_status_group": filters["status_group"],
            "created_by_username": filters["created_by_username"],
            "assigned_to_username": filters["assigned_to_username"],
            "selected_assignment_scope": filters["assignment_scope"],
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
    can_manage = _is_support_user(request.user) or ticket.assigned_to_id == request.user.id
    if not can_manage:
        messages.error(request, "You do not have permission to update this ticket.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    is_support = _is_support_user(request.user)
    FormClass = TicketUpdateForm if is_support else TicketAssigneeUpdateForm
    if request.method == "POST":
        previous_assigned_to_id = ticket.assigned_to_id
        previous_status = ticket.status
        ticket._assignment_actor_id = request.user.id
        form = FormClass(request.POST, request.FILES, instance=ticket, user=request.user)
        if form.is_valid():
            new_status = form.cleaned_data.get("status")
            if new_status == "resolved":
                new_assigned_to_id = ticket.assigned_to_id
                if "assigned_to" in getattr(form, "cleaned_data", {}):
                    assignee = form.cleaned_data.get("assigned_to")
                    new_assigned_to_id = getattr(assignee, "id", None)

                if not new_assigned_to_id:
                    messages.error(request, "Assign this ticket before marking it as resolved.")
                    return render(request, "tickets/ticket_update.html", {"ticket": ticket, "form": form})

                if new_assigned_to_id != request.user.id:
                    messages.error(request, "Only the assigned agent can mark this ticket as resolved.")
                    return render(request, "tickets/ticket_update.html", {"ticket": ticket, "form": form})
            if new_status == "closed" and previous_status != "resolved":
                messages.error(request, "A ticket can only be closed after it has been resolved.")
                return render(request, "tickets/ticket_update.html", {"ticket": ticket, "form": form})

            status_note = (form.cleaned_data.get("status_note") or "").strip()
            close_email_uploads = form.cleaned_data.get("close_email_attachments") or []
            ticket = form.save(commit=False)
            if previous_status != ticket.status and ticket.status == "resolved":
                ticket.resolved_note = status_note
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

                    mail_subject = f"Ticket Resolved: {ticket.ticket_id}"
                    mail_body = (
                        f"Hello {ticket.created_by.get_full_name() or ticket.created_by.username},\n\n"
                        f"Your helpdesk ticket has been marked as Resolved.\n\n"
                        f"Ticket Number: {ticket.ticket_id}\n"
                        f"Subject: {ticket.subject}\n"
                        f"Request Type: {ticket.get_request_type_display()}\n"
                        f"Department: {ticket.department or '-'}\n"
                        f"Impact: {ticket.get_impact_display()}\n"
                        f"Urgency: {ticket.get_urgency_display()}\n"
                        f"Priority: {ticket.get_priority_display()}\n"
                        f"Status: {ticket.get_status_display()}\n"
                        f"Resolved At: {resolved_at}\n"
                        f"Resolved By: {request.user.username} ({request.user.email or '-'})\n"
                        f"Resolution Details:\n{ticket.resolved_note or '-'}\n\n"
                        f"Close Ticket Link (Requester Confirmation):\n{close_url}\n"
                        f"Note: If you do not confirm closure, the ticket will be auto-closed after {auto_close_days} days.\n\n"
                        f"Ticket Link:\n{ticket_url}\n\n"
                        f"If the issue is not resolved, please reply back or contact IT support.\n"
                    )
                    try:
                        send_mail(
                            subject=mail_subject,
                            message=mail_body,
                            from_email=get_outgoing_from_email(),
                            recipient_list=[requester_email],
                            fail_silently=False,
                        )
                    except Exception:
                        messages.warning(request, "Ticket resolved, but requester email could not be sent.")
                else:
                    messages.warning(request, "Ticket resolved, but the requester has no email set.")

            if previous_status != ticket.status and ticket.status == "closed":
                requester_email = (getattr(ticket.created_by, "email", "") or "").strip()
                if requester_email:
                    closed_at = ticket.closed_at
                    if closed_at:
                        closed_at = timezone.localtime(closed_at).strftime("%b %d, %Y %H:%M")
                    else:
                        closed_at = "-"

                    ticket_url = request.build_absolute_uri(reverse("ticket_detail", args=[ticket.id]))
                    close_email_attachments = _build_email_attachments(close_email_uploads)
                    mail_lines = [
                        f"Hello {ticket.created_by.get_full_name() or ticket.created_by.username},",
                        "",
                        "Your helpdesk ticket has been marked as Closed.",
                        "",
                        f"Ticket Number: {ticket.ticket_id}",
                        f"Subject: {ticket.subject}",
                        f"Request Type: {ticket.get_request_type_display()}",
                        f"Department: {ticket.department or '-'}",
                        f"Impact: {ticket.get_impact_display()}",
                        f"Urgency: {ticket.get_urgency_display()}",
                        f"Priority: {ticket.get_priority_display()}",
                        f"Status: {ticket.get_status_display()}",
                        f"Closed At: {closed_at}",
                        f"Closed By: {request.user.username} ({request.user.email or '-'})",
                    ]
                    if ticket.closed_note:
                        mail_lines.extend(["", f"Closure Details:\n{ticket.closed_note}"])
                    if close_email_attachments:
                        attachment_count = len(close_email_attachments)
                        noun = "attachment" if attachment_count == 1 else "attachments"
                        verb = "is" if attachment_count == 1 else "are"
                        mail_lines.extend(["", f"{attachment_count} {noun} {verb} included with this email."])
                    mail_lines.extend(["", f"Ticket Link:\n{ticket_url}"])

                    mail_subject = f"Ticket Closed: {ticket.ticket_id}"
                    mail_body = "\n".join(mail_lines)
                    try:
                        _send_email_message(
                            mail_subject,
                            mail_body,
                            [requester_email],
                            email_attachments=close_email_attachments,
                        )
                    except Exception:
                        messages.warning(request, "Ticket closed, but requester email could not be sent.")
                else:
                    messages.warning(request, "Ticket closed, but the requester has no email set.")

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
