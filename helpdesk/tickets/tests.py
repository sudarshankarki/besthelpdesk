import base64
import hashlib
import hmac
import json
from datetime import timedelta
from io import BytesIO, StringIO
from urllib.parse import urlparse
from unittest.mock import Mock, patch
import zipfile

from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core import mail
from django.core.management import call_command
from django.http import HttpResponse
from django.test import Client, TestCase
from django.test.utils import override_settings
from django.utils import timezone
from django.urls import reverse

from accounts.models import AuthenticationSettings, Branch, Department
from .forms import TicketForm
from .models import (
    GroupMailboxEmail,
    IncidentReport,
    IncidentReportAttachment,
    IncidentReportSignoff,
    PortalFlashAnnouncement,
    RemoteAccessApproval,
    TechnicalDocument,
    Ticket,
    TicketAssignmentLog,
    TicketChatReadState,
    TicketReminderSummaryLog,
    TicketMessageAttachment,
    TicketMessage,
)
from .notifications import (
    build_call_notification_payload,
    build_chat_notification_payload,
    get_call_notification_target_ids,
    get_chat_notification_target_ids,
)
from .views import _build_incident_response_template_docx, _build_ticket_incident_report_docx


class _MockS3Body:
    def __init__(self, payload: bytes):
        self._buffer = BytesIO(payload)

    def read(self, size=-1):
        return self._buffer.read(size)

    def close(self):
        self._buffer.close()


def _excel_test_column_label(column_index: int) -> str:
    label = []
    while column_index > 0:
        column_index, remainder = divmod(column_index - 1, 26)
        label.append(chr(ord("A") + remainder))
    return "".join(reversed(label)) or "A"


def _build_test_xlsx(sheet_map):
    workbook = BytesIO()
    with zipfile.ZipFile(workbook, "w") as archive:
        sheet_entries = []
        relationship_entries = []
        for sheet_index, (sheet_name, rows) in enumerate(sheet_map, start=1):
            row_entries = []
            for row_index, row_values in enumerate(rows, start=1):
                cell_entries = []
                for column_index, value in enumerate(row_values, start=1):
                    if value in {"", None}:
                        continue
                    cell_ref = f"{_excel_test_column_label(column_index)}{row_index}"
                    if isinstance(value, bool):
                        cell_entries.append(
                            f'<c r="{cell_ref}" t="b"><v>{1 if value else 0}</v></c>'
                        )
                    elif isinstance(value, (int, float)):
                        cell_entries.append(f'<c r="{cell_ref}"><v>{value}</v></c>')
                    else:
                        text_value = (
                            str(value)
                            .replace("&", "&amp;")
                            .replace("<", "&lt;")
                            .replace(">", "&gt;")
                        )
                        cell_entries.append(
                            f'<c r="{cell_ref}" t="inlineStr"><is><t>{text_value}</t></is></c>'
                        )
                row_entries.append(f'<row r="{row_index}">{"".join(cell_entries)}</row>')

            archive.writestr(
                f"xl/worksheets/sheet{sheet_index}.xml",
                (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    f'<sheetData>{"".join(row_entries)}</sheetData>'
                    "</worksheet>"
                ),
            )
            sheet_entries.append(
                f'<sheet name="{sheet_name}" sheetId="{sheet_index}" r:id="rId{sheet_index}"/>'
            )
            relationship_entries.append(
                (
                    f'<Relationship Id="rId{sheet_index}" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                    f'Target="worksheets/sheet{sheet_index}.xml"/>'
                )
            )

        archive.writestr(
            "xl/workbook.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f'<sheets>{"".join(sheet_entries)}</sheets>'
                "</workbook>"
            ),
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'{"".join(relationship_entries)}'
                "</Relationships>"
            ),
        )
    return workbook.getvalue()


class PruneTicketMessagesCommandTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="retention_user",
            password="testpass123",
        )
        self.ticket = Ticket.objects.create(
            created_by=self.user,
            subject="Retention Test",
            description="Testing message retention command",
            priority="low",
            status="new",
        )

    def test_dry_run_does_not_delete_messages(self):
        old_message = TicketMessage.objects.create(
            ticket=self.ticket,
            author=self.user,
            body="old message",
        )
        TicketMessage.objects.filter(pk=old_message.pk).update(
            created_at=timezone.now() - timedelta(days=200)
        )

        out = StringIO()
        call_command("prune_ticket_messages", "--days", "180", "--dry-run", stdout=out)

        self.assertEqual(TicketMessage.objects.count(), 1)
        self.assertIn("would be deleted", out.getvalue())

    def test_command_deletes_only_messages_older_than_cutoff(self):
        old_message = TicketMessage.objects.create(
            ticket=self.ticket,
            author=self.user,
            body="old message",
        )
        new_message = TicketMessage.objects.create(
            ticket=self.ticket,
            author=self.user,
            body="new message",
        )
        TicketMessage.objects.filter(pk=old_message.pk).update(
            created_at=timezone.now() - timedelta(days=200)
        )
        TicketMessage.objects.filter(pk=new_message.pk).update(
            created_at=timezone.now() - timedelta(days=10)
        )

        call_command("prune_ticket_messages", "--days", "180")

        remaining_ids = set(TicketMessage.objects.values_list("id", flat=True))
        self.assertSetEqual(remaining_ids, {new_message.id})


class TicketCloseKeepsMessagesTests(TestCase):
    def test_closing_ticket_keeps_message_history(self):
        user = get_user_model().objects.create_user(
            username="close_test_user",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=user,
            subject="Close deletes messages",
            description="Close should delete chat history",
            priority="low",
            status="new",
        )
        TicketMessage.objects.create(ticket=ticket, author=user, body="hello")
        TicketMessage.objects.create(ticket=ticket, author=user, body="world")
        self.assertEqual(TicketMessage.objects.filter(ticket=ticket).count(), 2)

        ticket.status = "closed"
        ticket.save()

        self.assertEqual(TicketMessage.objects.filter(ticket=ticket).count(), 2)


class TicketResolveKeepsMessagesTests(TestCase):
    def test_resolving_ticket_keeps_message_history(self):
        user = get_user_model().objects.create_user(
            username="resolve_test_user",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=user,
            subject="Resolve deletes messages",
            description="Resolve should delete chat history",
            priority="low",
            status="new",
        )
        TicketMessage.objects.create(ticket=ticket, author=user, body="hello")
        TicketMessage.objects.create(ticket=ticket, author=user, body="world")
        self.assertEqual(TicketMessage.objects.filter(ticket=ticket).count(), 2)

        ticket.status = "resolved"
        ticket.save()

        self.assertEqual(TicketMessage.objects.filter(ticket=ticket).count(), 2)


class PurgeClosedTicketConversationsCommandTests(TestCase):
    def test_closed_ticket_older_than_cutoff_gets_purged(self):
        user = get_user_model().objects.create_user(
            username="closed_retention_user",
            password="testpass123",
        )
        old_ticket = Ticket.objects.create(
            created_by=user,
            subject="Old closed ticket",
            description="Should be purged after retention window",
            priority="low",
            status="closed",
        )
        recent_ticket = Ticket.objects.create(
            created_by=user,
            subject="Recent closed ticket",
            description="Should not be purged yet",
            priority="low",
            status="closed",
        )
        TicketMessage.objects.create(ticket=old_ticket, author=user, body="old closed message")
        TicketMessage.objects.create(ticket=recent_ticket, author=user, body="recent closed message")
        Ticket.objects.filter(pk=old_ticket.pk).update(closed_at=timezone.now() - timedelta(days=11))
        Ticket.objects.filter(pk=recent_ticket.pk).update(closed_at=timezone.now() - timedelta(days=3))

        call_command("purge_closed_ticket_conversations", "--days", "10")

        self.assertEqual(TicketMessage.objects.filter(ticket=old_ticket).count(), 0)
        self.assertEqual(TicketMessage.objects.filter(ticket=recent_ticket).count(), 1)


class PruneOpenTicketConversationsCommandTests(TestCase):
    def test_open_ticket_older_than_cutoff_gets_purged(self):
        user = get_user_model().objects.create_user(
            username="open_retention_user",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=user,
            subject="Open retention test",
            description="Testing open ticket conversation retention",
            priority="low",
            status="new",
        )
        Ticket.objects.filter(pk=ticket.pk).update(created_at=timezone.now() - timedelta(days=11))
        TicketMessage.objects.create(ticket=ticket, author=user, body="hello")

        call_command("prune_open_ticket_conversations", "--days", "10")

        self.assertEqual(TicketMessage.objects.filter(ticket=ticket).count(), 0)

class TicketAdminReportTests(TestCase):
    def test_admin_report_download_csv(self):
        admin_user = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@bestfinance.com.np",
            password="adminpass123",
        )
        normal_user = get_user_model().objects.create_user(
            username="report_user",
            email="report_user@bestfinance.com.np",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=normal_user,
            subject="Report Test",
            description="Report ticket",
            priority="low",
            status="resolved",
            resolved_at=timezone.now(),
        )
        self.client.force_login(admin_user)

        url = reverse("admin:tickets_ticket_report")
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        body = response.content.decode("utf-8")
        self.assertIn("Summary", body)
        self.assertIn(ticket.ticket_id, body)

    def test_admin_report_uses_resolved_at_for_time_to_solve_when_ticket_is_closed_later(self):
        admin_user = get_user_model().objects.create_superuser(
            username="admin_resolved_first",
            email="admin_resolved_first@bestfinance.com.np",
            password="adminpass123",
        )
        normal_user = get_user_model().objects.create_user(
            username="report_user_resolved_first",
            email="report_user_resolved_first@bestfinance.com.np",
            password="testpass123",
        )
        created_at = timezone.now() - timedelta(days=7)
        resolved_at = created_at + timedelta(days=2, hours=4)
        closed_at = resolved_at + timedelta(days=3)
        ticket = Ticket.objects.create(
            created_by=normal_user,
            subject="Resolved before close report test",
            description="Report should use resolved timestamp for TTR.",
            priority="low",
            status="closed",
            resolved_at=resolved_at,
            closed_at=closed_at,
        )
        Ticket.objects.filter(pk=ticket.pk).update(created_at=created_at)
        ticket.refresh_from_db()
        self.client.force_login(admin_user)

        response = self.client.get(reverse("admin:tickets_ticket_report"))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn(ticket.ticket_id, body)
        self.assertIn("2d 4h 0m", body)
        self.assertNotIn("5d 4h 0m", body)


class TicketTimeToResolveTests(TestCase):
    def test_formatted_ttr_uses_resolved_time_even_if_ticket_is_closed_later(self):
        requester = get_user_model().objects.create_user(
            username="ttr_requester",
            password="testpass123",
        )
        created_at = timezone.now() - timedelta(days=6, hours=7, minutes=3)
        resolved_at = created_at + timedelta(days=1, hours=2, minutes=5)
        closed_at = resolved_at + timedelta(days=4)
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="TTR resolved timestamp",
            description="Time to resolve should stop at resolved.",
            priority="medium",
            status="closed",
            resolved_at=resolved_at,
            closed_at=closed_at,
        )
        Ticket.objects.filter(pk=ticket.pk).update(created_at=created_at)
        ticket.refresh_from_db()

        self.assertEqual(ticket.resolution_duration, resolved_at - created_at)
        self.assertEqual(ticket.formatted_ttr(), "1d 2h 5m")


class TicketCallNotificationTests(TestCase):
    def setUp(self):
        self.requester = get_user_model().objects.create_user(
            username="call_requester",
            first_name="Ram",
            password="testpass123",
        )
        self.assignee = get_user_model().objects.create_user(
            username="call_assignee",
            password="testpass123",
        )
        self.ticket = Ticket.objects.create(
            created_by=self.requester,
            assigned_to=self.assignee,
            subject="Audio call test",
            description="Test incoming call popup",
            priority="medium",
            status="new",
        )

    def test_call_notification_targets_other_primary_participant(self):
        self.assertEqual(
            get_call_notification_target_ids(self.ticket, self.requester.id),
            [self.assignee.id],
        )
        self.assertEqual(
            get_call_notification_target_ids(self.ticket, self.assignee.id),
            [self.requester.id],
        )

    def test_call_notification_payload_contains_ticket_link(self):
        payload = build_call_notification_payload(self.ticket, self.requester)

        self.assertEqual(payload["kind"], "incoming_call")
        self.assertEqual(payload["level"], "warning")
        self.assertEqual(payload["title"], "Incoming audio call")
        self.assertEqual(payload["url"], reverse("ticket_detail", args=[self.ticket.id]))
        self.assertEqual(
            payload["answer_url"],
            f'{reverse("ticket_detail", args=[self.ticket.id])}?autocall=1&callmode=answer#ticket-chat',
        )
        self.assertEqual(payload["ticket_id"], self.ticket.id)
        self.assertEqual(payload["ticket_code"], self.ticket.ticket_id)
        self.assertEqual(payload["caller"], self.requester.username)
        self.assertEqual(payload["delay"], 20000)
        self.assertIn("Ram", payload["message"])
        self.assertIn(self.ticket.ticket_id, payload["message"])


class TicketChatNotificationTests(TestCase):
    def setUp(self):
        self.requester = get_user_model().objects.create_user(
            username="chat_requester",
            first_name="Hari",
            password="testpass123",
        )
        self.assignee = get_user_model().objects.create_user(
            username="chat_assignee",
            password="testpass123",
        )
        self.ticket = Ticket.objects.create(
            created_by=self.requester,
            assigned_to=self.assignee,
            subject="Chat toast test",
            description="Test chat toast popup data",
            priority="medium",
            status="new",
        )

    def test_chat_notification_targets_other_primary_participant(self):
        self.assertEqual(
            get_chat_notification_target_ids(self.ticket, self.requester.id),
            [self.assignee.id],
        )
        self.assertEqual(
            get_chat_notification_target_ids(self.ticket, self.assignee.id),
            [self.requester.id],
        )

    def test_chat_notification_payload_contains_ticket_link_and_preview(self):
        payload = build_chat_notification_payload(
            self.ticket,
            self.requester,
            "Please restart the printer service on your machine before testing again.",
        )

        self.assertEqual(payload["kind"], "chat_message")
        self.assertEqual(payload["level"], "info")
        self.assertEqual(payload["title"], "New chat message")
        self.assertEqual(payload["url"], reverse("ticket_detail", args=[self.ticket.id]))
        self.assertEqual(payload["ticket_id"], self.ticket.id)
        self.assertEqual(payload["ticket_code"], self.ticket.ticket_id)
        self.assertEqual(payload["sender"], self.requester.username)
        self.assertEqual(payload["delay"], 8000)
        self.assertIn("Hari", payload["message"])
        self.assertIn(self.ticket.ticket_id, payload["message"])
        self.assertIn("Please restart the printer service", payload["message"])

    def test_private_chat_notifications_only_include_requester_and_assignee(self):
        self.ticket.chat_is_private = True
        self.ticket.save()

        self.assertEqual(
            get_chat_notification_target_ids(self.ticket, self.requester.id),
            [self.assignee.id],
        )
        self.assertEqual(
            get_call_notification_target_ids(self.ticket, self.assignee.id),
            [self.requester.id],
        )


class TechnicalDocsVisibilityTests(TestCase):
    def setUp(self):
        self.kathmandu, _created = Branch.objects.get_or_create(
            name="Kathmandu",
            defaults={"branch_id": "TST-KTM"},
        )
        self.pokhara, _created = Branch.objects.get_or_create(
            name="Pokhara",
            defaults={"branch_id": "TST-PKR"},
        )
        self.butwal, _created = Branch.objects.get_or_create(
            name="Butwal",
            defaults={"branch_id": "TST-BTW"},
        )
        self.hr, _created = Department.objects.update_or_create(name="HR", defaults={})
        self.finance, _created = Department.objects.update_or_create(name="Finance", defaults={})

        self.alice = get_user_model().objects.create_user(
            username="alice",
            password="testpass123",
            department="HR",
            branch="Kathmandu",
        )
        self.bob = get_user_model().objects.create_user(
            username="bob",
            password="testpass123",
            department="HR",
            branch="Pokhara",
        )
        self.carol = get_user_model().objects.create_user(
            username="carol",
            password="testpass123",
            department="Finance",
            branch="Kathmandu",
        )
        self.dan = get_user_model().objects.create_user(
            username="dan",
            password="testpass123",
            department="Finance",
            branch="Butwal",
        )
        self.agent = get_user_model().objects.create_user(
            username="agent",
            password="testpass123",
            is_itsupport=True,
        )

        self.public_doc = TechnicalDocument.objects.create(
            title="Public Doc",
            description="Visible to everyone",
            visibility=TechnicalDocument.VISIBILITY_PUBLIC,
            object_key="tech_docs/public.pdf",
            filename="public.pdf",
            content_type="application/pdf",
            size=123,
            uploaded_by=self.agent,
        )
        self.restricted_doc = TechnicalDocument.objects.create(
            title="Restricted Doc",
            description="Only for Alice",
            visibility=TechnicalDocument.VISIBILITY_RESTRICTED,
            object_key="tech_docs/restricted.pdf",
            filename="restricted.pdf",
            content_type="application/pdf",
            size=123,
            uploaded_by=self.agent,
        )
        self.restricted_doc.allowed_users.add(self.alice)
        self.support_doc = TechnicalDocument.objects.create(
            title="Support Doc",
            description="Only for IT",
            visibility=TechnicalDocument.VISIBILITY_SUPPORT_ONLY,
            object_key="tech_docs/support.pdf",
            filename="support.pdf",
            content_type="application/pdf",
            size=123,
            uploaded_by=self.agent,
        )
        self.branch_doc = TechnicalDocument.objects.create(
            title="Scoped Branch PDF",
            description="Visible to two branches",
            visibility=TechnicalDocument.VISIBILITY_BRANCH,
            object_key="tech_docs/branch.pdf",
            filename="branch.pdf",
            content_type="application/pdf",
            size=123,
            uploaded_by=self.agent,
        )
        self.branch_doc.allowed_branches.add(self.kathmandu, self.pokhara)
        self.all_branch_doc = TechnicalDocument.objects.create(
            title="All Branches PDF",
            description="Visible to all branches",
            visibility=TechnicalDocument.VISIBILITY_BRANCH,
            object_key="tech_docs/all-branch.pdf",
            filename="all-branch.pdf",
            content_type="application/pdf",
            size=123,
            uploaded_by=self.agent,
        )
        self.department_doc = TechnicalDocument.objects.create(
            title="Department Doc",
            description="Visible to selected departments in Kathmandu",
            visibility=TechnicalDocument.VISIBILITY_DEPARTMENT,
            object_key="tech_docs/department.pdf",
            filename="department.pdf",
            content_type="application/pdf",
            size=123,
            uploaded_by=self.agent,
        )
        self.department_doc.allowed_departments.add(self.hr, self.finance)
        self.department_doc.allowed_branches.add(self.kathmandu)
        self.department_all_branches_doc = TechnicalDocument.objects.create(
            title="Department All Branches Doc",
            description="Visible to HR in all branches",
            visibility=TechnicalDocument.VISIBILITY_DEPARTMENT,
            object_key="tech_docs/department-all.pdf",
            filename="department-all.pdf",
            content_type="application/pdf",
            size=123,
            uploaded_by=self.agent,
        )
        self.department_all_branches_doc.allowed_departments.add(self.hr)

    def test_docs_list_filters_by_visibility(self):
        url = reverse("tech_docs")

        self.client.force_login(self.alice)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.public_doc.title)
        self.assertContains(response, self.restricted_doc.title)
        self.assertContains(response, self.branch_doc.title)
        self.assertContains(response, self.all_branch_doc.title)
        self.assertContains(response, self.department_doc.title)
        self.assertContains(response, self.department_all_branches_doc.title)
        self.assertNotContains(response, self.support_doc.title)

        self.client.force_login(self.bob)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.public_doc.title)
        self.assertContains(response, self.branch_doc.title)
        self.assertContains(response, self.all_branch_doc.title)
        self.assertContains(response, self.department_all_branches_doc.title)
        self.assertNotContains(response, self.restricted_doc.title)
        self.assertNotContains(response, self.department_doc.title)
        self.assertNotContains(response, self.support_doc.title)

        self.client.force_login(self.carol)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.public_doc.title)
        self.assertContains(response, self.branch_doc.title)
        self.assertContains(response, self.all_branch_doc.title)
        self.assertContains(response, self.department_doc.title)
        self.assertNotContains(response, self.restricted_doc.title)
        self.assertNotContains(response, self.department_all_branches_doc.title)
        self.assertNotContains(response, self.support_doc.title)

        self.client.force_login(self.dan)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.public_doc.title)
        self.assertContains(response, self.all_branch_doc.title)
        self.assertNotContains(response, self.branch_doc.title)
        self.assertNotContains(response, self.restricted_doc.title)
        self.assertNotContains(response, self.department_doc.title)
        self.assertNotContains(response, self.support_doc.title)

        self.client.force_login(self.agent)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.public_doc.title)
        self.assertContains(response, self.restricted_doc.title)
        self.assertContains(response, self.support_doc.title)
        self.assertContains(response, self.branch_doc.title)
        self.assertContains(response, self.all_branch_doc.title)
        self.assertContains(response, self.department_doc.title)
        self.assertContains(response, self.department_all_branches_doc.title)

    def test_doc_view_forbidden_for_unlisted_user(self):
        url = reverse("tech_doc_view", args=[self.restricted_doc.id])

        self.client.force_login(self.bob)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_branch_and_department_scoped_docs_are_forbidden_when_user_scope_does_not_match(self):
        self.client.force_login(self.dan)
        branch_response = self.client.get(reverse("tech_doc_view", args=[self.branch_doc.id]))
        self.assertEqual(branch_response.status_code, 403)

        self.client.force_login(self.bob)
        department_response = self.client.get(reverse("tech_doc_view", args=[self.department_doc.id]))
        self.assertEqual(department_response.status_code, 403)

    @patch("tickets.views.get_s3_client")
    @patch("tickets.views.get_minio_config")
    def test_support_user_can_upload_department_doc_for_multiple_departments_and_branches(
        self,
        mock_get_minio_config,
        mock_get_s3_client,
    ):
        mock_get_minio_config.return_value = Mock(bucket="tech-docs")
        mock_s3 = Mock()
        mock_get_s3_client.return_value = mock_s3

        self.client.force_login(self.agent)
        response = self.client.post(
            reverse("tech_docs_upload"),
            data={
                "files": [
                    SimpleUploadedFile("policy.pdf", b"%PDF-1.4", content_type="application/pdf"),
                ],
                "titles": ["Policy"],
                "descriptions": ["Department scoped"],
                "visibility": TechnicalDocument.VISIBILITY_DEPARTMENT,
                "departments": [str(self.hr.id), str(self.finance.id)],
                "branches": [self.kathmandu.branch_id, self.pokhara.branch_id],
            },
        )

        self.assertEqual(response.status_code, 302)
        document = TechnicalDocument.objects.get(title="Policy")
        self.assertEqual(document.visibility, TechnicalDocument.VISIBILITY_DEPARTMENT)
        self.assertCountEqual(
            document.allowed_departments.values_list("name", flat=True),
            ["HR", "Finance"],
        )
        self.assertCountEqual(
            document.allowed_branches.values_list("name", flat=True),
            ["Kathmandu", "Pokhara"],
        )
        self.assertTrue(mock_s3.upload_fileobj.called)

    @patch("tickets.views.get_s3_client")
    @patch("tickets.views.get_minio_config")
    def test_support_user_can_leave_branch_blank_to_target_all_branches(
        self,
        mock_get_minio_config,
        mock_get_s3_client,
    ):
        mock_get_minio_config.return_value = Mock(bucket="tech-docs")
        mock_s3 = Mock()
        mock_get_s3_client.return_value = mock_s3

        self.client.force_login(self.agent)
        response = self.client.post(
            reverse("tech_docs_upload"),
            data={
                "files": [
                    SimpleUploadedFile("hr-guide.pdf", b"%PDF-1.4", content_type="application/pdf"),
                ],
                "titles": ["HR Guide"],
                "descriptions": ["All branches"],
                "visibility": TechnicalDocument.VISIBILITY_DEPARTMENT,
                "departments": [str(self.hr.id)],
            },
        )

        self.assertEqual(response.status_code, 302)
        document = TechnicalDocument.objects.get(title="HR Guide")
        self.assertEqual(document.visibility, TechnicalDocument.VISIBILITY_DEPARTMENT)
        self.assertCountEqual(
            document.allowed_departments.values_list("name", flat=True),
            ["HR"],
        )
        self.assertFalse(document.allowed_branches.exists())
        self.assertTrue(mock_s3.upload_fileobj.called)

    @patch("tickets.views.get_s3_client")
    @patch("tickets.views.get_minio_config")
    def test_support_user_can_upload_excel_technical_document(
        self,
        mock_get_minio_config,
        mock_get_s3_client,
    ):
        mock_get_minio_config.return_value = Mock(bucket="tech-docs")
        mock_s3 = Mock()
        mock_get_s3_client.return_value = mock_s3

        self.client.force_login(self.agent)
        response = self.client.post(
            reverse("tech_docs_upload"),
            data={
                "files": [
                    SimpleUploadedFile(
                        "it-checklist.xlsx",
                        b"excel-bytes",
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    ),
                ],
                "titles": ["IT Checklist"],
                "descriptions": ["Spreadsheet checklist"],
                "visibility": TechnicalDocument.VISIBILITY_PUBLIC,
            },
        )

        self.assertEqual(response.status_code, 302)
        document = TechnicalDocument.objects.get(title="IT Checklist")
        self.assertEqual(document.filename, "it-checklist.xlsx")
        self.assertEqual(
            document.content_type,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertTrue(mock_s3.upload_fileobj.called)

    @patch("tickets.views.get_s3_client")
    @patch("tickets.views.get_minio_config")
    def test_xlsx_doc_view_renders_all_workbook_sheets_in_browser(
        self,
        mock_get_minio_config,
        mock_get_s3_client,
    ):
        workbook_bytes = _build_test_xlsx(
            [
                ("Hardware Checklist", [["Device", "Status"], ["Router A", "Ready"]]),
                ("Contacts", [["Name", "Extension"], ["Nikki", 2001], ["Sudarshan", 2002]]),
            ]
        )
        document = TechnicalDocument.objects.create(
            title="Network Workbook",
            description="Spreadsheet preview",
            visibility=TechnicalDocument.VISIBILITY_PUBLIC,
            object_key="tech_docs/network.xlsx",
            filename="network.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size=len(workbook_bytes),
            uploaded_by=self.agent,
        )
        mock_get_minio_config.return_value = Mock(bucket="tech-docs")
        mock_s3 = Mock()
        mock_s3.get_object.side_effect = lambda **kwargs: {
            "Body": _MockS3Body(workbook_bytes),
            "ContentLength": len(workbook_bytes),
        }
        mock_get_s3_client.return_value = mock_s3

        self.client.force_login(self.alice)
        response = self.client.get(reverse("tech_doc_view", args=[document.id]))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "docs/tech_doc_excel_preview.html")
        self.assertContains(response, "Hardware Checklist")
        self.assertContains(response, "Contacts")
        self.assertContains(response, "Router A")
        self.assertContains(response, "Nikki")
        self.assertContains(response, reverse("tech_doc_download", args=[document.id]))

    @patch("tickets.views.get_s3_client")
    @patch("tickets.views.get_minio_config")
    def test_xlsx_doc_download_still_returns_attachment(
        self,
        mock_get_minio_config,
        mock_get_s3_client,
    ):
        workbook_bytes = _build_test_xlsx([("Contacts", [["Name"], ["Nikki"]])])
        document = TechnicalDocument.objects.create(
            title="Contacts Workbook",
            description="Downloadable spreadsheet",
            visibility=TechnicalDocument.VISIBILITY_PUBLIC,
            object_key="tech_docs/contacts.xlsx",
            filename="contacts.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size=len(workbook_bytes),
            uploaded_by=self.agent,
        )
        mock_get_minio_config.return_value = Mock(bucket="tech-docs")
        mock_s3 = Mock()
        mock_s3.get_object.side_effect = lambda **kwargs: {
            "Body": _MockS3Body(workbook_bytes),
            "ContentLength": len(workbook_bytes),
        }
        mock_get_s3_client.return_value = mock_s3

        self.client.force_login(self.alice)
        response = self.client.get(reverse("tech_doc_download", args=[document.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="contacts.xlsx"',
        )
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertEqual(b"".join(response.streaming_content), workbook_bytes)

    @patch("tickets.views.get_s3_client")
    @patch("tickets.views.get_minio_config")
    def test_technical_document_upload_rejects_unsupported_file_types(
        self,
        mock_get_minio_config,
        mock_get_s3_client,
    ):
        mock_get_minio_config.return_value = Mock(bucket="tech-docs")
        mock_s3 = Mock()
        mock_get_s3_client.return_value = mock_s3

        self.client.force_login(self.agent)
        response = self.client.post(
            reverse("tech_docs_upload"),
            data={
                "files": [
                    SimpleUploadedFile("script.docx", b"word-bytes", content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                ],
                "titles": ["Script"],
                "descriptions": ["Should fail"],
                "visibility": TechnicalDocument.VISIBILITY_PUBLIC,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(TechnicalDocument.objects.filter(title="Script").exists())
        self.assertContains(response, "Only PDF and Excel files are allowed")


class PortalFlashAnnouncementTests(TestCase):
    def _flash_window_form_data(self):
        starts_at = timezone.localtime(timezone.now()).replace(second=0, microsecond=0)
        ends_at = starts_at + timedelta(days=1)
        return {
            "category": PortalFlashAnnouncement.CATEGORY_IT,
            "starts_at": starts_at.strftime("%Y-%m-%dT%H:%M"),
            "ends_at": ends_at.strftime("%Y-%m-%dT%H:%M"),
        }

    def tearDown(self):
        for announcement in PortalFlashAnnouncement.objects.all():
            if announcement.image:
                announcement.image.delete(save=False)
        super().tearDown()

    def test_support_user_can_upload_jpeg_login_flash(self):
        support_user = get_user_model().objects.create_user(
            username="portal_flash_support",
            password="testpass123",
            is_itsupport=True,
        )
        self.client.force_login(support_user)

        response = self.client.post(
            reverse("portal_flash_upload"),
            data={
                "title": "Important login notice",
                "message": "Please read this flash image.",
                **self._flash_window_form_data(),
                "image": SimpleUploadedFile("notice.jpg", b"jpegdata", content_type="image/jpeg"),
            },
        )

        self.assertEqual(response.status_code, 302)
        announcement = PortalFlashAnnouncement.objects.get()
        self.assertEqual(announcement.category, PortalFlashAnnouncement.CATEGORY_IT)
        self.assertEqual(announcement.title, "Important login notice")
        self.assertEqual(announcement.message, "Please read this flash image.")
        self.assertEqual(announcement.uploaded_by_id, support_user.id)
        self.assertTrue((announcement.image.name or "").endswith(".jpg"))

    def test_portal_flash_upload_rejects_non_jpeg_files(self):
        support_user = get_user_model().objects.create_user(
            username="portal_flash_support_invalid",
            password="testpass123",
            is_itsupport=True,
        )
        self.client.force_login(support_user)

        response = self.client.post(
            reverse("portal_flash_upload"),
            data={
                "title": "Invalid flash",
                **self._flash_window_form_data(),
                "image": SimpleUploadedFile("notice.pdf", b"%PDF-1.4", content_type="application/pdf"),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(PortalFlashAnnouncement.objects.count(), 0)
        self.assertContains(response, "Only JPEG images are allowed")

    def test_login_flash_popup_is_included_once_for_first_page_after_login(self):
        requester = get_user_model().objects.create_user(
            username="portal_flash_requester",
            password="testpass123",
        )
        announcement = PortalFlashAnnouncement.objects.create(
            title="System maintenance notice",
            message="Please review the login flash image.",
            image=SimpleUploadedFile("maintenance.jpg", b"jpegdata", content_type="image/jpeg"),
        )

        self.client.force_login(requester)
        first_response = self.client.get(reverse("ticket_list"))

        self.assertEqual(first_response.status_code, 200)
        first_announcements = first_response.context.get("login_flash_announcements", [])
        self.assertEqual(len(first_announcements), 1)
        self.assertEqual(first_announcements[0]["title"], announcement.title)
        self.assertEqual(
            first_announcements[0]["image_url"],
            reverse("portal_flash_image_view", args=[announcement.id]),
        )

        second_response = self.client.get(reverse("ticket_list"))
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(second_response.context.get("login_flash_announcements", []), [])

    def test_active_flash_is_rendered_in_news_banner(self):
        requester = get_user_model().objects.create_user(
            username="portal_flash_banner_user",
            password="testpass123",
        )
        announcement = PortalFlashAnnouncement.objects.create(
            category=PortalFlashAnnouncement.CATEGORY_BANK,
            title="Branch banking downtime",
            message="Core banking maintenance starts tonight at 10 PM.",
            image=SimpleUploadedFile("banner.jpg", b"jpegdata", content_type="image/jpeg"),
            starts_at=timezone.now() - timedelta(hours=1),
            ends_at=timezone.now() + timedelta(days=2),
        )

        self.client.force_login(requester)
        response = self.client.get(reverse("ticket_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Updates")
        self.assertContains(response, 'aria-label="Portal announcements"', html=False)
        self.assertContains(response, announcement.title)
        self.assertContains(response, announcement.message)
        self.assertContains(response, announcement.get_category_display())
        self.assertContains(response, reverse("portal_flash_image_view", args=[announcement.id]))

    def test_admin_user_can_delete_flash_from_admin_panel(self):
        admin_user = get_user_model().objects.create_superuser(
            username="portal_flash_admin",
            email="portal_flash_admin@bestfinance.com.np",
            password="adminpass123",
        )
        announcement = PortalFlashAnnouncement.objects.create(
            title="Delete from admin",
            message="This flash should be removable from the admin page.",
            image=SimpleUploadedFile("delete-me.jpg", b"jpegdata", content_type="image/jpeg"),
        )
        image_name = announcement.image.name

        self.client.force_login(admin_user)
        changelist_response = self.client.get(reverse("admin:tickets_portalflashannouncement_changelist"))

        self.assertEqual(changelist_response.status_code, 200)
        self.assertContains(changelist_response, announcement.title)
        self.assertTrue(announcement.image.storage.exists(image_name))

        delete_response = self.client.post(
            reverse("admin:tickets_portalflashannouncement_delete", args=[announcement.pk]),
            {"post": "yes"},
            follow=True,
        )

        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(PortalFlashAnnouncement.objects.count(), 0)
        self.assertFalse(announcement.image.storage.exists(image_name))


class TicketIdGenerationTests(TestCase):
    def test_ticket_id_is_random_and_unique(self):
        user = get_user_model().objects.create_user(
            username="ticketid_user",
            password="testpass123",
        )
        ticket1 = Ticket.objects.create(
            created_by=user,
            subject="Ticket 1",
            description="Ticket 1",
            priority="low",
            status="new",
        )
        ticket2 = Ticket.objects.create(
            created_by=user,
            subject="Ticket 2",
            description="Ticket 2",
            priority="low",
            status="new",
        )

        self.assertTrue(ticket1.ticket_id.startswith("BFC-"))
        self.assertTrue(ticket2.ticket_id.startswith("BFC-"))
        self.assertNotEqual(ticket1.ticket_id, ticket2.ticket_id)


class TicketDepartmentFieldTests(TestCase):
    def test_ticket_department_is_saved(self):
        user = get_user_model().objects.create_user(
            username="dept_user",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=user,
            subject="Dept test",
            department="HR",
            description="Dept test",
            priority="low",
            status="new",
        )
        self.assertEqual(ticket.department, "HR")


class TicketDepartmentFromNotifyEmailTests(TestCase):
    def test_department_auto_populates_from_group_notify_email(self):
        dept, _created = Department.objects.update_or_create(name="HR", defaults={})
        GroupMailboxEmail.objects.update_or_create(
            email="hr@bestfinance.com.np",
            defaults={"department": dept},
        )
        user = get_user_model().objects.create_user(
            username="group_dept_user",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=user,
            subject="Dept from email",
            notify_email="hr@bestfinance.com.np",
            description="Test",
            priority="low",
            status="new",
        )
        self.assertEqual(ticket.department, "HR")

    def test_department_is_not_overwritten_when_set(self):
        dept, _created = Department.objects.update_or_create(name="HR", defaults={})
        GroupMailboxEmail.objects.update_or_create(
            email="hr@bestfinance.com.np",
            defaults={"department": dept},
        )
        user = get_user_model().objects.create_user(
            username="group_dept_user_2",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=user,
            subject="Explicit dept wins",
            notify_email="hr@bestfinance.com.np",
            department="Finance",
            description="Test",
            priority="low",
            status="new",
        )
        self.assertEqual(ticket.department, "Finance")

    def test_non_group_notify_email_does_not_set_department(self):
        user = get_user_model().objects.create_user(
            username="non_group_dept_user",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=user,
            subject="Non-group email",
            notify_email="assignee@bestfinance.com.np",
            description="Test",
            priority="low",
            status="new",
        )
        self.assertEqual(ticket.department, "")


class TicketFormDepartmentChoicesTests(TestCase):
    def test_department_choices_are_populated_from_db(self):
        Department.objects.update_or_create(name="IT", defaults={})
        form = TicketForm()
        values = [value for value, _label in form.fields["department"].choices]
        self.assertIn("IT", values)

    def test_branch_choices_are_populated_and_default_to_request_user_branch(self):
        Branch.objects.get_or_create(name="NewRoad", defaults={"branch_id": "991"})
        user = get_user_model().objects.create_user(
            username="branch_form_user",
            password="testpass123",
            branch="NewRoad",
        )

        form = TicketForm(user=user)
        values = [value for value, _label in form.fields["branch"].choices]

        self.assertIn("NewRoad", values)
        self.assertEqual(form.fields["branch"].initial, "NewRoad")

    def test_it_department_branch_choices_only_allow_head_office(self):
        Department.objects.update_or_create(name="IT", defaults={})
        Branch.objects.get_or_create(name="Pokhara", defaults={"branch_id": "992"})
        Branch.objects.get_or_create(name="Head Office", defaults={"branch_id": "993"})

        form = TicketForm(data={"department": "IT"})
        values = [value for value, _label in form.fields["branch"].choices]

        self.assertEqual(values, ["", "Head Office"])
        self.assertEqual(form.restricted_branch_by_department["IT"], "Head Office")
        self.assertEqual(form.default_notify_email_by_department["IT"], "it@bestfinance.com.np")

    def test_hr_department_branch_choices_only_allow_head_office(self):
        Department.objects.update_or_create(name="HR", defaults={})
        Branch.objects.get_or_create(name="Pokhara", defaults={"branch_id": "999"})
        Branch.objects.get_or_create(name="Head Office", defaults={"branch_id": "993"})

        form = TicketForm(data={"department": "HR"})
        values = [value for value, _label in form.fields["branch"].choices]

        self.assertEqual(values, ["", "Head Office"])
        self.assertEqual(form.restricted_branch_by_department["HR"], "Head Office")
        self.assertEqual(form.default_notify_email_by_department["HR"], "hr@bestfinance.com.np")

    def test_assign_email_suggestions_are_grouped_by_department_and_branch(self):
        Department.objects.update_or_create(name="HR", defaults={})
        Department.objects.update_or_create(name="IT", defaults={})
        Branch.objects.get_or_create(name="NewRoad", defaults={"branch_id": "994"})
        Branch.objects.get_or_create(name="Pokhara", defaults={"branch_id": "995"})
        get_user_model().objects.create_user(
            username="hr_email_user",
            email="hr_email_user@bestfinance.com.np",
            password="testpass123",
            department="HR",
            branch="NewRoad",
        )
        get_user_model().objects.create_user(
            username="it_email_user",
            email="it_email_user@bestfinance.com.np",
            password="testpass123",
            department="IT",
            branch="Pokhara",
        )

        form = TicketForm()

        self.assertEqual(form.fields["assign_email"].widget.attrs.get("list"), "assign-email-suggestions")
        self.assertEqual(
            form.assignable_emails_by_department_and_branch["HR"]["NewRoad"],
            ["hr_email_user@bestfinance.com.np"],
        )
        self.assertEqual(
            form.assignable_emails_by_department_and_branch["IT"]["Pokhara"],
            ["it_email_user@bestfinance.com.np"],
        )

    def test_notify_email_suggestions_keep_department_group_mailboxes_separate(self):
        hr_department, _created = Department.objects.update_or_create(name="HR", defaults={})
        Branch.objects.get_or_create(name="NewRoad", defaults={"branch_id": "996"})
        get_user_model().objects.create_user(
            username="hr_notify_user",
            email="hr_notify_user@bestfinance.com.np",
            password="testpass123",
            department="HR",
            branch="NewRoad",
        )
        GroupMailboxEmail.objects.update_or_create(
            email="hr@bestfinance.com.np",
            defaults={"department": hr_department},
        )

        form = TicketForm()

        self.assertEqual(
            form.notify_emails_by_department_and_branch["HR"]["group_mailboxes"],
            ["hr@bestfinance.com.np"],
        )
        self.assertEqual(
            form.notify_emails_by_department_and_branch["HR"]["branches"]["NewRoad"],
            ["hr_notify_user@bestfinance.com.np"],
        )

    def test_notify_email_suggestions_fallback_to_department_branch_users_when_no_group_mailbox(self):
        Department.objects.update_or_create(name="Finance", defaults={})
        Department.objects.update_or_create(name="HR", defaults={})
        Branch.objects.get_or_create(name="NewRoad", defaults={"branch_id": "997"})
        Branch.objects.get_or_create(name="Pokhara", defaults={"branch_id": "998"})
        get_user_model().objects.create_user(
            username="finance_notify_newroad",
            email="finance_notify_newroad@bestfinance.com.np",
            password="testpass123",
            department="Finance",
            branch="NewRoad",
        )
        get_user_model().objects.create_user(
            username="finance_notify_pokhara",
            email="finance_notify_pokhara@bestfinance.com.np",
            password="testpass123",
            department="Finance",
            branch="Pokhara",
        )
        get_user_model().objects.create_user(
            username="hr_notify_newroad",
            email="hr_notify_newroad@bestfinance.com.np",
            password="testpass123",
            department="HR",
            branch="NewRoad",
        )

        form = TicketForm()

        self.assertEqual(
            form.notify_emails_by_department_and_branch["Finance"]["group_mailboxes"],
            [],
        )
        self.assertEqual(
            form.notify_emails_by_department_and_branch["Finance"]["branches"]["NewRoad"],
            ["finance_notify_newroad@bestfinance.com.np"],
        )
        self.assertEqual(
            form.notify_emails_by_department_and_branch["Finance"]["branches"]["Pokhara"],
            ["finance_notify_pokhara@bestfinance.com.np"],
        )
        self.assertNotIn(
            "hr_notify_newroad@bestfinance.com.np",
            form.notify_emails_by_department_and_branch["Finance"]["branches"]["NewRoad"],
        )


class TicketNotifyEmailAssignmentTests(TestCase):
    def test_notify_email_assigns_ticket_when_user_exists_and_not_group(self):
        creator = get_user_model().objects.create_user(
            username="creator",
            email="creator@bestfinance.com.np",
            password="testpass123",
        )
        assignee = get_user_model().objects.create_user(
            username="assignee",
            email="assignee@bestfinance.com.np",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=creator,
            subject="Notify email assignment",
            notify_email="assignee@bestfinance.com.np",
            description="Test",
            priority="low",
            status="new",
            assigned_to=assignee,
        )
        ticket._assignment_actor_id = creator.id
        ticket.save()

        self.assertEqual(ticket.assigned_to_id, assignee.id)
        self.assertEqual(TicketAssignmentLog.objects.filter(ticket=ticket).count(), 1)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    IT_SUPPORT_EMAIL="it-support@bestfinance.com.np",
)
class CreateTicketRoutingTests(TestCase):
    def setUp(self):
        Department.objects.update_or_create(name="HR", defaults={})
        Department.objects.update_or_create(name="Finance", defaults={})
        Department.objects.update_or_create(name="IT", defaults={})
        Branch.objects.get_or_create(name="Head Office", defaults={"branch_id": "996"})
        Branch.objects.get_or_create(name="Kathmandu", defaults={"branch_id": "997"})
        Branch.objects.get_or_create(name="Pokhara", defaults={"branch_id": "998"})
        self.creator = get_user_model().objects.create_user(
            username="creator_form",
            email="creator_form@bestfinance.com.np",
            password="testpass123",
            branch="Kathmandu",
        )
        self.assignee = get_user_model().objects.create_user(
            username="assignee_form",
            email="assignee_form@bestfinance.com.np",
            password="testpass123",
            department="HR",
            branch="Kathmandu",
        )
        self.same_department_other_branch_assignee = get_user_model().objects.create_user(
            username="assignee_other_branch_form",
            email="assignee_other_branch_form@bestfinance.com.np",
            password="testpass123",
            department="HR",
            branch="Pokhara",
        )
        self.other_department_assignee = get_user_model().objects.create_user(
            username="finance_assignee_form",
            email="finance_assignee_form@bestfinance.com.np",
            password="testpass123",
            department="Finance",
        )
        self.finance_other_branch_assignee = get_user_model().objects.create_user(
            username="finance_other_branch_form",
            email="finance_other_branch_form@bestfinance.com.np",
            password="testpass123",
            department="Finance",
            branch="Pokhara",
        )
        self.client.force_login(self.creator)

    def _ticket_payload(self, **overrides):
        payload = {
            "subject": "Create ticket routing",
            "request_type": "incident",
            "department": "",
            "branch": "",
            "assign_email": "",
            "notify_email": "",
            "cc_emails": "",
            "description": "Routing test ticket",
            "impact": "single_user",
            "urgency": "medium",
        }
        payload.update(overrides)
        return payload

    def test_create_ticket_defaults_request_type_to_service_request(self):
        response = self.client.get(reverse("create_ticket"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            '<option value="service" selected>Service Request</option>',
            html=True,
        )

    def test_create_ticket_assigns_person_and_notifies_group_mailbox(self):
        dept, _created = Department.objects.update_or_create(name="HR", defaults={})
        GroupMailboxEmail.objects.update_or_create(
            email="hr@bestfinance.com.np",
            defaults={"department": dept},
        )

        response = self.client.post(
            reverse("create_ticket"),
            data=self._ticket_payload(
                subject="HR routed ticket",
                branch="Kathmandu",
                assign_email=self.assignee.email,
                notify_email="hr@bestfinance.com.np",
            ),
        )

        self.assertEqual(response.status_code, 302)
        ticket = Ticket.objects.get(subject="HR routed ticket")
        self.assertEqual(ticket.assigned_to_id, self.assignee.id)
        self.assertEqual(ticket.notify_email, "hr@bestfinance.com.np")
        self.assertEqual(ticket.department, "HR")
        self.assertEqual(ticket.branch, "Kathmandu")
        self.assertEqual(TicketAssignmentLog.objects.filter(ticket=ticket).count(), 1)
        self.assertEqual(len(mail.outbox), 2)
        self.assertTrue(
            any(
                message.subject.startswith("New Helpdesk Ticket:")
                and message.to == ["hr@bestfinance.com.np"]
                for message in mail.outbox
            )
        )
        self.assertTrue(
            any(
                message.subject.startswith("Ticket Assigned:")
                and message.to == [self.assignee.email]
                for message in mail.outbox
            )
        )
        assignment_message = next(
            message for message in mail.outbox
            if message.subject.startswith("Ticket Assigned:") and message.to == [self.assignee.email]
        )
        self.assertIn(f"Dear {self.assignee.first_name or self.assignee.username},", assignment_message.body)
        self.assertIn("I hope you are doing well.", assignment_message.body)

    def test_create_incident_ticket_creates_incident_report_template(self):
        response = self.client.post(
            reverse("create_ticket"),
            data=self._ticket_payload(
                subject="CBS outage reported",
                request_type="incident",
                branch="Kathmandu",
                department="Finance",
                description="CBS is unavailable for branch users.",
            ),
        )

        self.assertEqual(response.status_code, 302)
        ticket = Ticket.objects.get(subject="CBS outage reported")
        incident_report = IncidentReport.objects.get(ticket=ticket)
        self.assertEqual(incident_report.incident_title, "CBS outage reported")
        self.assertEqual(incident_report.incident_id, ticket.ticket_id)
        self.assertEqual(incident_report.reported_by, self.creator.username)
        self.assertEqual(incident_report.branch_impacted, "Kathmandu")
        self.assertEqual(incident_report.impact_branch_department, "Kathmandu / Finance")
        self.assertEqual(incident_report.summary_what_happened, "CBS is unavailable for branch users.")
        self.assertEqual(incident_report.evidence_ticket_case, ticket.ticket_id)
        self.assertEqual(incident_report.created_by_id, self.creator.id)
        self.assertEqual(incident_report.updated_by_id, self.creator.id)

    def test_build_incident_response_template_docx_succeeds(self):
        cleaned_data = {
            'date_of_report': timezone.now(),
            'reporting_employee_name': 'Alice',
            'reporting_employee_designation': 'Operations',
            'reporting_employee_email': 'alice@example.com',
            'reporting_employee_contact': '+977 9800000000',
            'incident_id': 'INC-2026-001',
            'date_time_of_occurrence': 'Apr 07, 2026 14:00',
            'date_time_of_detection': 'Apr 07, 2026 14:30',
            'source_of_incident': 'Monitoring alert',
            'incident_location_ip': '10.0.0.1',
            'incident_description': 'System outage',
            'unit_or_department_impacted': 'IT Operations',
            'systems_impacted': 'CBS',
            'network_impacted': 'WAN',
            'operations_impacted': 'Branch banking',
            'severity_choice': 'critical',
            'evidence_attachments': 'screenshot.png',
        }
        payload = _build_incident_response_template_docx(cleaned_data)
        self.assertIsInstance(payload, bytes)
        self.assertGreater(len(payload), 0)

        with zipfile.ZipFile(BytesIO(payload), 'r') as docx_file:
            self.assertIn('word/document.xml', docx_file.namelist())
            document_xml = docx_file.read('word/document.xml').decode('utf-8')
            package_xml = "\n".join(
                docx_file.read(name).decode("utf-8", errors="ignore")
                for name in docx_file.namelist()
                if name.startswith("word/") and name.endswith(".xml") and name != "word/document.xml"
            )
            self.assertIn('INC-2026-001', document_xml)
            self.assertIn('Alice', document_xml)
            self.assertIn('System(s) Impacted:', document_xml)
            self.assertIn('Network Impacted:', document_xml)
            self.assertNotIn('☑', document_xml)
            self.assertNotIn('☐', document_xml)
            self.assertNotIn('[Title]', document_xml + package_xml)
            self.assertNotIn('<w:t>Critical</w:t>', document_xml)
            self.assertNotIn('<w:t>High</w:t>', document_xml)
            self.assertNotIn('<w:t>Medium</w:t>', document_xml)
            self.assertNotIn('<w:t>Low</w:t>', document_xml)
            self.assertNotIn('May 07, 2026 22:47', document_xml)
            self.assertIn("INCIDENT REPORT INFORMATION", document_xml)
            self.assertIn("<w:b", document_xml)
            self.assertIn('w:ascii="Times New Roman"', document_xml)

    def test_create_service_ticket_does_not_create_incident_report_template(self):
        response = self.client.post(
            reverse("create_ticket"),
            data=self._ticket_payload(
                subject="Shared folder access",
                request_type="service",
                branch="Kathmandu",
                department="Finance",
            ),
        )

        self.assertEqual(response.status_code, 302)
        ticket = Ticket.objects.get(subject="Shared folder access")
        self.assertFalse(IncidentReport.objects.filter(ticket=ticket).exists())

    def test_create_ticket_rejects_group_mailbox_in_assign_email(self):
        dept, _created = Department.objects.update_or_create(name="HR", defaults={})
        GroupMailboxEmail.objects.update_or_create(
            email="hr@bestfinance.com.np",
            defaults={"department": dept},
        )

        response = self.client.post(
            reverse("create_ticket"),
            data=self._ticket_payload(
                subject="Invalid group assignment",
                assign_email="hr@bestfinance.com.np",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Ticket.objects.filter(subject="Invalid group assignment").exists())
        self.assertIn("assign_email", response.context["form"].errors)
        self.assertContains(response, "Group mailboxes belong in Notify Email")

    def test_create_ticket_notify_email_does_not_auto_assign(self):
        response = self.client.post(
            reverse("create_ticket"),
            data=self._ticket_payload(
                subject="Notify only ticket",
                notify_email=self.assignee.email,
            ),
        )

        self.assertEqual(response.status_code, 302)
        ticket = Ticket.objects.get(subject="Notify only ticket")
        self.assertIsNone(ticket.assigned_to_id)
        self.assertEqual(ticket.notify_email, self.assignee.email)
        self.assertEqual(TicketAssignmentLog.objects.filter(ticket=ticket).count(), 0)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.assignee.email])
        self.assertIn("Dear Support Team,", mail.outbox[0].body)
        self.assertIn("I hope you are doing well.", mail.outbox[0].body)
        self.assertIn("Open Ticket:", mail.outbox[0].body)
        self.assertIn("has raised the following ticket for service.", mail.outbox[0].body)

    def test_create_ticket_notification_supports_multiple_cc_recipients(self):
        response = self.client.post(
            reverse("create_ticket"),
            data=self._ticket_payload(
                subject="Notify with cc ticket",
                notify_email=self.assignee.email,
                cc_emails="manager@bestfinance.com.np; Audit@bestfinance.com.np, manager@bestfinance.com.np",
            ),
        )

        self.assertEqual(response.status_code, 302)
        ticket = Ticket.objects.get(subject="Notify with cc ticket")
        self.assertEqual(
            ticket.cc_emails,
            "manager@bestfinance.com.np, audit@bestfinance.com.np",
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.assignee.email])
        self.assertEqual(
            mail.outbox[0].cc,
            ["manager@bestfinance.com.np", "audit@bestfinance.com.np"],
        )

    def test_create_ticket_rejects_invalid_cc_email(self):
        response = self.client.post(
            reverse("create_ticket"),
            data=self._ticket_payload(
                subject="Invalid cc ticket",
                notify_email=self.assignee.email,
                cc_emails="manager@bestfinance.com.np, not-an-email",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Ticket.objects.filter(subject="Invalid cc ticket").exists())
        self.assertIn("cc_emails", response.context["form"].errors)
        self.assertContains(response, "Enter valid CC email addresses only")

    def test_ticket_detail_shows_notify_email_from_creation(self):
        dept, _created = Department.objects.update_or_create(name="HR", defaults={})
        GroupMailboxEmail.objects.update_or_create(
            email="hr@bestfinance.com.np",
            defaults={"department": dept},
        )

        response = self.client.post(
            reverse("create_ticket"),
            data=self._ticket_payload(
                subject="Detail page saved emails",
                branch="Kathmandu",
                notify_email="hr@bestfinance.com.np",
            ),
        )

        self.assertEqual(response.status_code, 302)
        ticket = Ticket.objects.get(subject="Detail page saved emails")

        detail_response = self.client.get(reverse("ticket_detail", args=[ticket.id]))

        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(
            detail_response,
            '<div class="ticket-meta">Notify Email</div><div class="fw-semibold"><a class="text-decoration-none" href="mailto:hr@bestfinance.com.np">hr@bestfinance.com.np</a></div>',
            html=True,
        )

    def test_ticket_detail_shows_cc_emails_from_creation(self):
        response = self.client.post(
            reverse("create_ticket"),
            data=self._ticket_payload(
                subject="Detail page saved cc emails",
                cc_emails="manager@bestfinance.com.np, audit@bestfinance.com.np",
            ),
        )

        self.assertEqual(response.status_code, 302)
        ticket = Ticket.objects.get(subject="Detail page saved cc emails")

        detail_response = self.client.get(reverse("ticket_detail", args=[ticket.id]))

        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "CC Emails")
        self.assertContains(detail_response, 'mailto:manager@bestfinance.com.np')
        self.assertContains(detail_response, 'mailto:audit@bestfinance.com.np')

    @patch("tickets.views.get_s3_client")
    @patch("tickets.views.get_minio_config")
    def test_create_ticket_attachments_are_sent_only_with_initial_ticket_emails(
        self,
        mock_get_minio_config,
        mock_get_s3_client,
    ):
        dept, _created = Department.objects.update_or_create(name="HR", defaults={})
        GroupMailboxEmail.objects.update_or_create(
            email="hr@bestfinance.com.np",
            defaults={"department": dept},
        )
        mock_get_minio_config.return_value = Mock(bucket="ticket-files")
        mock_s3 = Mock()
        mock_get_s3_client.return_value = mock_s3
        upload = SimpleUploadedFile("evidence.txt", b"important ticket attachment", content_type="text/plain")

        response = self.client.post(
            reverse("create_ticket"),
            data={
                **self._ticket_payload(
                    subject="Attachment routed ticket",
                    assign_email=self.assignee.email,
                    notify_email="hr@bestfinance.com.np",
                ),
                "attachments": [upload],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 2)
        self.assertTrue(mock_s3.upload_fileobj.called)
        for message in mail.outbox:
            self.assertTrue(message.attachments)
            self.assertEqual(message.attachments[0][0], "evidence.txt")
        ticket = Ticket.objects.get(subject="Attachment routed ticket")
        self.assertEqual(TicketMessageAttachment.objects.filter(ticket=ticket).count(), 1)

    def test_create_ticket_rejects_self_assign_email(self):
        response = self.client.post(
            reverse("create_ticket"),
            data=self._ticket_payload(
                subject="Self assign create",
                assign_email=self.creator.email,
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Ticket.objects.filter(subject="Self assign create").exists())
        self.assertIn("assign_email", response.context["form"].errors)
        self.assertContains(response, "You cannot assign a ticket to yourself.")

    def test_create_ticket_rejects_assign_email_outside_selected_department(self):
        response = self.client.post(
            reverse("create_ticket"),
            data=self._ticket_payload(
                subject="Wrong department assignee",
                department="Finance",
                branch="Kathmandu",
                assign_email=self.assignee.email,
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Ticket.objects.filter(subject="Wrong department assignee").exists())
        self.assertIn("assign_email", response.context["form"].errors)
        self.assertContains(response, "must belong to the Finance department")

    def test_create_ticket_rejects_assign_email_outside_selected_branch(self):
        response = self.client.post(
            reverse("create_ticket"),
            data=self._ticket_payload(
                subject="Wrong branch assignee",
                department="Finance",
                branch="Kathmandu",
                assign_email=self.finance_other_branch_assignee.email,
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Ticket.objects.filter(subject="Wrong branch assignee").exists())
        self.assertIn("assign_email", response.context["form"].errors)
        self.assertContains(response, "must belong to the Kathmandu branch")

    def test_create_ticket_page_renders_department_email_suggestion_data(self):
        department, _created = Department.objects.update_or_create(name="HR", defaults={})
        GroupMailboxEmail.objects.update_or_create(
            email="hr@bestfinance.com.np",
            defaults={"department": department},
        )

        response = self.client.get(reverse("create_ticket"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="assign-email-suggestions"')
        self.assertContains(response, 'id="notify-email-suggestions"')
        self.assertContains(response, 'name="branch"')
        self.assertContains(response, self.assignee.email)
        self.assertContains(response, "hr@bestfinance.com.np")
        self.assertContains(response, 'name="submission_token"')

    def test_create_ticket_duplicate_submission_token_reuses_existing_ticket(self):
        form_response = self.client.get(reverse("create_ticket"))
        submission_token = form_response.context["submission_token"]

        first_response = self.client.post(
            reverse("create_ticket"),
            data={
                **self._ticket_payload(subject="Duplicate guarded ticket"),
                "submission_token": submission_token,
            },
        )
        second_response = self.client.post(
            reverse("create_ticket"),
            data={
                **self._ticket_payload(subject="Duplicate guarded ticket"),
                "submission_token": submission_token,
            },
        )

        ticket = Ticket.objects.get(subject="Duplicate guarded ticket")
        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(Ticket.objects.filter(subject="Duplicate guarded ticket").count(), 1)
        self.assertEqual(second_response.url, reverse("ticket_detail", args=[ticket.id]))

    def test_create_ticket_uses_selected_branch(self):
        response = self.client.post(
            reverse("create_ticket"),
            data=self._ticket_payload(
                subject="Selected branch ticket",
                branch="Pokhara",
            ),
        )

        self.assertEqual(response.status_code, 302)
        ticket = Ticket.objects.get(subject="Selected branch ticket")
        self.assertEqual(ticket.branch, "Pokhara")

    def test_create_ticket_for_it_department_defaults_branch_to_head_office(self):
        response = self.client.post(
            reverse("create_ticket"),
            data=self._ticket_payload(
                subject="IT default branch ticket",
                department="IT",
                branch="",
            ),
        )

        self.assertEqual(response.status_code, 302)
        ticket = Ticket.objects.get(subject="IT default branch ticket")
        self.assertEqual(ticket.department, "IT")
        self.assertEqual(ticket.branch, "Head Office")
        self.assertEqual(ticket.notify_email, "it@bestfinance.com.np")

    def test_create_ticket_for_hr_department_defaults_branch_to_head_office(self):
        response = self.client.post(
            reverse("create_ticket"),
            data=self._ticket_payload(
                subject="HR default branch ticket",
                department="HR",
                branch="",
                notify_email="",
            ),
        )

        self.assertEqual(response.status_code, 302)
        ticket = Ticket.objects.get(subject="HR default branch ticket")
        self.assertEqual(ticket.department, "HR")
        self.assertEqual(ticket.branch, "Head Office")
        self.assertEqual(ticket.notify_email, "hr@bestfinance.com.np")

    def test_create_ticket_rejects_non_head_office_branch_for_it_department(self):
        response = self.client.post(
            reverse("create_ticket"),
            data=self._ticket_payload(
                subject="IT wrong branch ticket",
                department="IT",
                branch="Pokhara",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Ticket.objects.filter(subject="IT wrong branch ticket").exists())
        self.assertIn("branch", response.context["form"].errors)
        self.assertContains(response, "IT department can only be routed to the Head Office branch")

    def test_create_ticket_rejects_non_head_office_branch_for_hr_department(self):
        response = self.client.post(
            reverse("create_ticket"),
            data=self._ticket_payload(
                subject="HR wrong branch ticket",
                department="HR",
                branch="Pokhara",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Ticket.objects.filter(subject="HR wrong branch ticket").exists())
        self.assertIn("branch", response.context["form"].errors)
        self.assertContains(response, "HR department can only be routed to the Head Office branch")

    def test_create_ticket_rejects_notify_email_outside_selected_department(self):
        response = self.client.post(
            reverse("create_ticket"),
            data=self._ticket_payload(
                subject="Wrong department notify user",
                department="HR",
                notify_email=self.other_department_assignee.email,
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Ticket.objects.filter(subject="Wrong department notify user").exists())
        self.assertIn("notify_email", response.context["form"].errors)
        self.assertContains(response, "must belong to the HR department")

    def test_create_ticket_rejects_group_notify_email_outside_selected_department(self):
        finance_department, _created = Department.objects.update_or_create(name="Finance", defaults={})
        GroupMailboxEmail.objects.update_or_create(
            email="finance@bestfinance.com.np",
            defaults={"department": finance_department},
        )

        response = self.client.post(
            reverse("create_ticket"),
            data=self._ticket_payload(
                subject="Wrong department notify mailbox",
                department="HR",
                notify_email="finance@bestfinance.com.np",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Ticket.objects.filter(subject="Wrong department notify mailbox").exists())
        self.assertIn("notify_email", response.context["form"].errors)
        self.assertContains(response, "must belong to the HR department")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class RemoteAccessRequestViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="remote_access_user",
            email="remote_access_user@bestfinance.com.np",
            password="testpass123",
            branch="Kathmandu",
        )
        self.recommender = get_user_model().objects.create_user(
            username="remote_access_recommender",
            email="remote_access_recommender@bestfinance.com.np",
            password="testpass123",
            department="Operations",
            branch="Kathmandu",
        )
        self.second_recommender = get_user_model().objects.create_user(
            username="remote_access_second_recommender",
            email="remote_access_second_recommender@bestfinance.com.np",
            password="testpass123",
            department="Operations",
            branch="Kathmandu",
        )
        self.approver = get_user_model().objects.create_user(
            username="remote_access_approver",
            email="remote_access_approver@bestfinance.com.np",
            password="testpass123",
            department="Finance",
            branch="Kathmandu",
        )
        self.other_user = get_user_model().objects.create_user(
            username="remote_access_other",
            email="remote_access_other@bestfinance.com.np",
            password="testpass123",
            branch="Pokhara",
        )
        self.client.force_login(self.user)

    def _request_payload(self, **overrides):
        payload = {
            "subject": "Something else",
            "details": "Vendor ABC needs temporary remote access to troubleshoot the finance server.",
            "recommender": "",
            "approver": str(self.approver.id),
        }
        payload.update(overrides)
        return payload

    def _create_request(self, **overrides):
        return self.client.post(
            reverse("remote_access_request"),
            data=self._request_payload(**overrides),
        )

    def _signature_upload(self, name="signature.png"):
        return SimpleUploadedFile(
            name,
            base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII="),
            content_type="image/png",
        )

    def _ensure_cbs_signature_users(self):
        for user, name in (
            (self.user, "requester-signature.png"),
            (self.recommender, "first-recommender-signature.png"),
            (self.second_recommender, "second-recommender-signature.png"),
            (self.approver, "approver-signature.png"),
        ):
            user.signature_image = self._signature_upload(name)
            user.save(update_fields=["signature_image"])

    def _cbs_branch_payload(self, **overrides):
        payload = {
            "subject": "CBS Access Request",
            "name": "Branch User",
            "designation": "Officer",
            "department": "Kathmandu / Operations",
            "employee_id": "EMP-001",
            "access_user": str(self.user.id),
            "user_type": "new",
            "old_user_id": "",
            "user_groups": ["A", "K"],
            "amendment_reason": "",
            "recommender": str(self.recommender.id),
            "second_recommender": str(self.second_recommender.id),
            "approver": str(self.approver.id),
            "requested_by_name": "Branch User",
            "requested_by_designation": "Officer",
            "requested_by_date": "2026-05-06",
            "recommended_by_name": "",
            "recommended_by_designation": "",
            "recommended_by_date": "",
            "branch_second_recommended_by_name": "",
            "branch_second_recommended_by_designation": "",
            "branch_second_recommended_by_date": "",
            "approved_by_name": "",
            "approved_by_designation": "",
            "approved_by_date": "",
            "endorsement": "on",
            "action": "submit",
        }
        payload.update(overrides)
        return payload

    def test_ticket_list_shows_remote_access_request_menu_link(self):
        response = self.client.get(reverse("ticket_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("remote_access_request"))
        self.assertContains(response, "Remote Access Request")

    def test_ticket_list_shows_incident_response_template_menu_link(self):
        response = self.client.get(reverse("ticket_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("incident_response_template"))
        self.assertContains(response, "Incident Response Template")

    def test_remote_access_request_page_renders(self):
        response = self.client.get(reverse("remote_access_request"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Remote Access Request")
        self.assertContains(response, 'value="Remote Access Request"', html=False)
        self.assertContains(response, "Recommended By (optional)")
        self.assertContains(response, "Approved By")
        self.assertContains(response, self.recommender.username)
        self.assertContains(response, self.approver.username)
        self.assertContains(response, 'name="submission_token"')

    def test_cbs_branch_access_request_page_renders_branch_format(self):
        response = self.client.get(reverse("cbs_access_branch_request"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "For Branch Office Only")
        self.assertContains(response, "Customer Service Desk")
        self.assertContains(response, "Operation In charge")
        self.assertContains(response, "Branch Manager")
        self.assertContains(response, "Second Digital Recommended By")
        self.assertNotContains(response, "Internal Audit Dept.")

    def test_cbs_branch_access_docx_uses_branch_template(self):
        from tickets.views import _build_cbs_access_docx

        payload = _build_cbs_access_docx(
            {
                "request_type": "cbs_access_branch",
                "name": "Branch User",
                "designation": "Officer",
                "department": "Kathmandu / Operations",
                "employee_id": "EMP-001",
                "request_id": "BFC-REQ123",
                "user_type": "new",
                "user_groups": ["A", "K"],
                "requested_by_name": "Requester",
                "recommended_by_name": "First Recommender",
                "branch_second_recommended_by_name": "Second Recommender",
                "approved_by_name": "Approver",
                "endorsement": True,
            }
        )
        with zipfile.ZipFile(BytesIO(payload), "r") as docx_file:
            document_xml = docx_file.read("word/document.xml").decode("utf-8")

        self.assertIn("For Branch Only", document_xml)
        self.assertIn("Request ID: BFC-REQ123", document_xml)
        self.assertIn("Branch User", document_xml)
        self.assertIn("Second Recommender", document_xml)
        self.assertIn("Kathmandu / Operations", document_xml)

    def test_cbs_access_data_uses_ticket_id_as_request_id(self):
        from tickets.views import _cbs_access_data_from_ticket

        ticket = Ticket.objects.create(
            created_by=self.user,
            subject="CBS Access Request",
            request_type="cbs_access_ho",
            description="Name: CBS User\nEmployee ID: EMP-123",
        )

        data = _cbs_access_data_from_ticket(ticket)

        self.assertEqual(data["request_id"], ticket.ticket_id)

    def test_cbs_branch_access_request_supports_second_recommender_chain(self):
        self._ensure_cbs_signature_users()
        response = self.client.post(
            reverse("cbs_access_branch_request"),
            data=self._cbs_branch_payload(),
        )
        ticket = Ticket.objects.get(subject="CBS Access Request")
        approval = RemoteAccessApproval.objects.get(ticket=ticket)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ticket.request_type, "cbs_access_branch")
        self.assertEqual(approval.recommender_id, self.recommender.id)
        self.assertEqual(approval.second_recommender_id, self.second_recommender.id)
        self.assertEqual(approval.approver_id, self.approver.id)
        self.assertEqual(approval.status, RemoteAccessApproval.STATUS_PENDING_RECOMMENDATION)
        self.assertEqual(approval.current_stage, "recommendation")
        self.assertEqual(approval.current_reviewer, self.recommender)

        mail.outbox.clear()
        recommender_client = Client()
        recommender_client.force_login(self.recommender)
        first_response = recommender_client.post(
            reverse("remote_access_approval_update", args=[ticket.id]),
            data={
                "decision": RemoteAccessApproval.STATUS_APPROVED,
                "decision_note": "First recommendation ok.",
            },
        )
        approval.refresh_from_db()

        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(approval.status, RemoteAccessApproval.STATUS_PENDING_RECOMMENDATION)
        self.assertEqual(approval.current_stage, "second_recommendation")
        self.assertEqual(approval.current_reviewer, self.second_recommender)
        self.assertEqual(approval.recommended_by_id, self.recommender.id)
        self.assertIsNone(approval.second_recommended_by_id)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.second_recommender.email])
        self.assertIn(f"CBS Access Second Recommendation Needed: {ticket.ticket_id}", mail.outbox[0].subject)

        approver_client = Client()
        approver_client.force_login(self.approver)
        early_response = approver_client.post(
            reverse("remote_access_approval_update", args=[ticket.id]),
            data={
                "decision": RemoteAccessApproval.STATUS_APPROVED,
                "decision_note": "Too early.",
            },
        )
        approval.refresh_from_db()

        self.assertEqual(early_response.status_code, 302)
        self.assertEqual(approval.status, RemoteAccessApproval.STATUS_PENDING_RECOMMENDATION)
        self.assertIsNone(approval.decided_by_id)

        mail.outbox.clear()
        second_client = Client()
        second_client.force_login(self.second_recommender)
        second_response = second_client.post(
            reverse("remote_access_approval_update", args=[ticket.id]),
            data={
                "decision": RemoteAccessApproval.STATUS_APPROVED,
                "decision_note": "Second recommendation ok.",
            },
        )
        approval.refresh_from_db()

        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(approval.status, RemoteAccessApproval.STATUS_PENDING_APPROVAL)
        self.assertEqual(approval.second_recommended_by_id, self.second_recommender.id)
        self.assertEqual(approval.current_stage, "approval")
        self.assertEqual(approval.current_reviewer, self.approver)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.approver.email])

        final_response = approver_client.post(
            reverse("remote_access_approval_update", args=[ticket.id]),
            data={
                "decision": RemoteAccessApproval.STATUS_APPROVED,
                "decision_note": "Approved.",
            },
        )
        approval.refresh_from_db()

        self.assertEqual(final_response.status_code, 302)
        self.assertEqual(approval.status, RemoteAccessApproval.STATUS_APPROVED)
        self.assertEqual(approval.decided_by_id, self.approver.id)

    def test_remote_access_request_creates_access_ticket_without_recommender(self):
        response = self._create_request()
        ticket = Ticket.objects.get(subject="Remote Access Request")
        approval = RemoteAccessApproval.objects.get(ticket=ticket)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("ticket_detail", args=[ticket.id]))
        self.assertEqual(ticket.created_by_id, self.user.id)
        self.assertEqual(ticket.request_type, "access")
        self.assertEqual(ticket.department, "")
        self.assertEqual(ticket.branch, self.user.branch)
        self.assertEqual(ticket.notify_email, "")
        self.assertEqual(ticket.impact, "single_user")
        self.assertEqual(ticket.urgency, "medium")
        self.assertIn("Vendor ABC needs temporary remote access", ticket.description)
        self.assertIsNone(approval.recommender_id)
        self.assertEqual(approval.approver_id, self.approver.id)
        self.assertEqual(approval.status, RemoteAccessApproval.STATUS_PENDING_APPROVAL)

    def test_remote_access_request_with_recommender_starts_recommendation_stage(self):
        response = self._create_request(recommender=str(self.recommender.id))
        ticket = Ticket.objects.get(subject="Remote Access Request")
        approval = RemoteAccessApproval.objects.get(ticket=ticket)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(approval.recommender_id, self.recommender.id)
        self.assertEqual(approval.approver_id, self.approver.id)
        self.assertEqual(approval.status, RemoteAccessApproval.STATUS_PENDING_RECOMMENDATION)

    def test_remote_access_request_sends_email_to_selected_recommender_first(self):
        response = self._create_request(recommender=str(self.recommender.id))
        ticket = Ticket.objects.get(subject="Remote Access Request")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.recommender.email])
        self.assertIn(f"Remote Access Recommendation Needed: {ticket.ticket_id}", mail.outbox[0].subject)
        self.assertIn(f"Dear {self.recommender.get_full_name() or self.recommender.username},", mail.outbox[0].body)
        self.assertIn("I hope you are doing well.", mail.outbox[0].body)
        self.assertIn("move forward for approval", mail.outbox[0].body.lower())
        self.assertIn("Vendor ABC needs temporary remote access", mail.outbox[0].body)

    def test_remote_access_request_without_recommender_sends_email_to_selected_approver(self):
        response = self._create_request()
        ticket = Ticket.objects.get(subject="Remote Access Request")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.approver.email])
        self.assertIn(f"Remote Access Approval Needed: {ticket.ticket_id}", mail.outbox[0].subject)
        self.assertIn(f"Dear {self.approver.get_full_name() or self.approver.username},", mail.outbox[0].body)
        self.assertIn("please review the details below", mail.outbox[0].body.lower())

    def test_remote_access_request_rejects_same_recommender_and_approver(self):
        response = self.client.post(
            reverse("remote_access_request"),
            data=self._request_payload(
                recommender=str(self.approver.id),
                approver=str(self.approver.id),
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recommended by and approved by must be different users.")
        self.assertFalse(Ticket.objects.filter(subject="Remote Access Request").exists())

    def test_remote_access_detail_hides_support_fields_and_shows_pending_status(self):
        self._create_request(recommender=str(self.recommender.id))
        ticket = Ticket.objects.get(subject="Remote Access Request")

        response = self.client.get(reverse("ticket_detail", args=[ticket.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["ticket"].display_status_label, "Pending Recommendation")
        self.assertContains(response, "Request Details")
        self.assertContains(response, "Pending Recommendation")
        self.assertNotContains(response, "Assigned To")
        self.assertNotContains(response, "Notify Email")
        self.assertNotContains(response, "CC Emails")
        self.assertNotContains(response, "Request Type")
        self.assertNotContains(response, "Impact")
        self.assertNotContains(response, "Urgency")
        self.assertNotContains(response, "Time to Resolve")

    def test_selected_recommender_and_approver_see_request_in_ticket_list(self):
        self._create_request(recommender=str(self.recommender.id))
        ticket = Ticket.objects.get(subject="Remote Access Request")

        recommender_client = Client()
        recommender_client.force_login(self.recommender)
        approver_client = Client()
        approver_client.force_login(self.approver)
        recommender_response = recommender_client.get(reverse("ticket_list"))
        approver_response = approver_client.get(reverse("ticket_list"))

        self.assertEqual(recommender_response.status_code, 200)
        self.assertEqual(approver_response.status_code, 200)
        self.assertContains(recommender_response, ticket.subject)
        self.assertContains(recommender_response, reverse("ticket_detail", args=[ticket.id]))
        self.assertContains(approver_response, ticket.subject)
        self.assertContains(approver_response, reverse("ticket_detail", args=[ticket.id]))

    def test_approver_cannot_approve_before_recommendation(self):
        create_response = self._create_request(recommender=str(self.recommender.id))
        ticket = Ticket.objects.get(subject="Remote Access Request")

        approver_client = Client()
        approver_client.force_login(self.approver)
        detail_response = approver_client.get(reverse("ticket_detail", args=[ticket.id]))

        self.assertEqual(create_response.status_code, 302)
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Remote Access Approval")
        self.assertNotContains(detail_response, "Approve Remote Access")
        self.assertContains(detail_response, "Waiting for recommendation from")

        decision_response = approver_client.post(
            reverse("remote_access_approval_update", args=[ticket.id]),
            data={
                "decision": RemoteAccessApproval.STATUS_APPROVED,
                "decision_note": "Approved for vendor troubleshooting.",
            },
        )

        approval = RemoteAccessApproval.objects.get(ticket=ticket)
        self.assertEqual(decision_response.status_code, 302)
        self.assertEqual(approval.status, RemoteAccessApproval.STATUS_PENDING_RECOMMENDATION)
        self.assertIsNone(approval.recommended_by_id)
        self.assertIsNone(approval.decided_by_id)

    def test_recommender_can_recommend_and_handoff_to_approver(self):
        self._create_request(recommender=str(self.recommender.id))
        ticket = Ticket.objects.get(subject="Remote Access Request")
        mail.outbox.clear()

        recommender_client = Client()
        recommender_client.force_login(self.recommender)
        response = recommender_client.post(
            reverse("remote_access_approval_update", args=[ticket.id]),
            data={
                "decision": RemoteAccessApproval.STATUS_APPROVED,
                "decision_note": "Recommended for vendor troubleshooting.",
            },
        )

        approval = RemoteAccessApproval.objects.get(ticket=ticket)
        approver_client = Client()
        approver_client.force_login(self.approver)
        approver_detail = approver_client.get(reverse("ticket_detail", args=[ticket.id]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(approval.status, RemoteAccessApproval.STATUS_PENDING_APPROVAL)
        self.assertEqual(approval.recommended_by_id, self.recommender.id)
        self.assertEqual(approval.recommendation_note, "Recommended for vendor troubleshooting.")
        self.assertIsNotNone(approval.recommended_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.approver.email])
        self.assertIn(f"Remote Access Approval Needed: {ticket.ticket_id}", mail.outbox[0].subject)
        self.assertIn("already been recommended", mail.outbox[0].body)
        self.assertIn("Recommended for vendor troubleshooting.", mail.outbox[0].body)
        self.assertContains(approver_detail, "Approve Remote Access")

    def test_selected_approver_can_open_ticket_and_approve_request_without_recommender(self):
        create_response = self._create_request()
        ticket = Ticket.objects.get(subject="Remote Access Request")

        approver_client = Client()
        approver_client.force_login(self.approver)
        detail_response = approver_client.get(reverse("ticket_detail", args=[ticket.id]))

        self.assertEqual(create_response.status_code, 302)
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Remote Access Approval")
        self.assertContains(detail_response, "Approve Remote Access")

        decision_response = approver_client.post(
            reverse("remote_access_approval_update", args=[ticket.id]),
            data={
                "decision": RemoteAccessApproval.STATUS_APPROVED,
                "decision_note": "Approved for vendor troubleshooting.",
            },
        )

        approval = RemoteAccessApproval.objects.get(ticket=ticket)
        self.assertEqual(decision_response.status_code, 302)
        self.assertEqual(approval.status, RemoteAccessApproval.STATUS_APPROVED)
        self.assertEqual(approval.decided_by_id, self.approver.id)
        self.assertEqual(approval.decision_note, "Approved for vendor troubleshooting.")
        self.assertIsNotNone(approval.decided_at)

    def test_assigned_user_can_open_remote_access_ticket_detail(self):
        self._create_request()
        ticket = Ticket.objects.get(subject="Remote Access Request")
        ticket.assigned_to = self.other_user
        ticket.save(update_fields=["assigned_to", "updated_at"])

        assigned_client = Client()
        assigned_client.force_login(self.other_user)
        response = assigned_client.get(reverse("ticket_detail", args=[ticket.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Remote Access Approval")

    def test_department_member_can_open_remote_access_ticket_detail(self):
        department_user = get_user_model().objects.create_user(
            username="remote_access_department_member",
            email="remote_access_department_member@bestfinance.com.np",
            password="testpass123",
            department="Operations",
            branch="Kathmandu",
        )
        self._create_request()
        ticket = Ticket.objects.get(subject="Remote Access Request")
        ticket.department = "Operations"
        ticket.save(update_fields=["department", "updated_at"])

        department_client = Client()
        department_client.force_login(department_user)
        list_response = department_client.get(reverse("ticket_list"))
        detail_response = department_client.get(reverse("ticket_detail", args=[ticket.id]))

        self.assertContains(list_response, ticket.ticket_id)
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Remote Access Approval")

    @patch("tickets.views._cbs_access_docx_to_pdf_payload")
    def test_cbs_access_download_defaults_to_pdf(self, mock_pdf_payload):
        mock_pdf_payload.return_value = b"%PDF-1.4 cbs access"
        ticket = Ticket.objects.create(
            created_by=self.user,
            subject="CBS Access Request",
            request_type="cbs_access_ho",
            description="CBS access request download test.",
            priority="medium",
            status="new",
            department="Operations",
            branch="Kathmandu",
        )
        RemoteAccessApproval.objects.create(ticket=ticket, approver=self.approver)

        response = self.client.get(reverse("cbs_access_request_download", args=[ticket.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn(".pdf", response["Content-Disposition"])
        mock_pdf_payload.assert_called_once()

    @patch("tickets.views._cbs_access_docx_to_pdf_payload")
    def test_approved_cbs_access_download_forces_pdf_even_when_docx_requested(self, mock_pdf_payload):
        mock_pdf_payload.return_value = b"%PDF-1.4 approved cbs access"
        ticket = Ticket.objects.create(
            created_by=self.user,
            subject="CBS Access Request",
            request_type="cbs_access_ho",
            description="CBS access approved PDF only test.",
            priority="medium",
            status="new",
            department="Operations",
            branch="Kathmandu",
        )
        RemoteAccessApproval.objects.create(
            ticket=ticket,
            approver=self.approver,
            status=RemoteAccessApproval.STATUS_APPROVED,
            decided_by=self.approver,
            decided_at=timezone.now(),
        )

        response = self.client.get(f"{reverse('cbs_access_request_download', args=[ticket.id])}?format=docx")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn(".pdf", response["Content-Disposition"])

    def test_resolved_approved_cbs_access_request_shows_ticket_status_in_my_tickets(self):
        ticket = Ticket.objects.create(
            created_by=self.user,
            subject="CBS Access Request",
            request_type="cbs_access_branch",
            description="Resolved CBS access should show ticket status, not only approval status.",
            priority="medium",
            status="resolved",
            resolved_at=timezone.now(),
            department="Operations",
            branch="Kathmandu",
        )
        RemoteAccessApproval.objects.create(
            ticket=ticket,
            approver=self.approver,
            status=RemoteAccessApproval.STATUS_APPROVED,
            decided_by=self.approver,
            decided_at=timezone.now(),
        )

        response = self.client.get(reverse("ticket_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ticket.ticket_id)
        self.assertContains(response, "Resolved")
        self.assertNotContains(response, ">Approved<", html=False)

    @patch("tickets.views._cbs_access_docx_to_pdf_payload")
    def test_cbs_access_email_attachment_is_pdf(self, mock_pdf_payload):
        from tickets.views import _build_cbs_access_email_attachments

        mock_pdf_payload.return_value = b"%PDF-1.4 cbs access"
        ticket = Ticket.objects.create(
            created_by=self.user,
            subject="CBS Access Request",
            request_type="cbs_access_ho",
            description="CBS access request email attachment test.",
            priority="medium",
            status="new",
            department="Operations",
            branch="Kathmandu",
        )
        approval = RemoteAccessApproval.objects.create(ticket=ticket, approver=self.approver)

        attachments = _build_cbs_access_email_attachments(ticket, approval)

        self.assertEqual(len(attachments), 1)
        filename, payload, content_type = attachments[0]
        self.assertTrue(filename.endswith(".pdf"))
        self.assertEqual(payload, b"%PDF-1.4 cbs access")
        self.assertEqual(content_type, "application/pdf")

    def test_remote_access_final_decision_emails_requester(self):
        self._create_request()
        ticket = Ticket.objects.get(subject="Remote Access Request")
        mail.outbox.clear()

        approver_client = Client()
        approver_client.force_login(self.approver)
        response = approver_client.post(
            reverse("remote_access_approval_update", args=[ticket.id]),
            data={
                "decision": RemoteAccessApproval.STATUS_REJECTED,
                "decision_note": "Remote access should not be granted.",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user.email])
        self.assertIn(f"Remote Access Rejected: {ticket.ticket_id}", mail.outbox[0].subject)
        self.assertIn(f"Dear {self.user.get_full_name() or self.user.username},", mail.outbox[0].body)
        self.assertIn("This is a courtesy update regarding your remote access request.", mail.outbox[0].body)
        self.assertIn("Decision Stage: Approval", mail.outbox[0].body)
        self.assertIn("Remote access should not be granted.", mail.outbox[0].body)

    def test_recommendation_rejection_emails_requester(self):
        self._create_request(recommender=str(self.recommender.id))
        ticket = Ticket.objects.get(subject="Remote Access Request")
        mail.outbox.clear()

        recommender_client = Client()
        recommender_client.force_login(self.recommender)
        response = recommender_client.post(
            reverse("remote_access_approval_update", args=[ticket.id]),
            data={
                "decision": RemoteAccessApproval.STATUS_REJECTED,
                "decision_note": "Recommendation denied for this vendor.",
            },
        )

        approval = RemoteAccessApproval.objects.get(ticket=ticket)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(approval.status, RemoteAccessApproval.STATUS_REJECTED)
        self.assertEqual(approval.recommended_by_id, self.recommender.id)
        self.assertIsNone(approval.decided_by_id)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user.email])
        self.assertIn(f"Remote Access Rejected: {ticket.ticket_id}", mail.outbox[0].subject)
        self.assertIn("Decision Stage: Recommendation", mail.outbox[0].body)
        self.assertIn("Recommendation denied for this vendor.", mail.outbox[0].body)

    def test_remote_access_duplicate_submission_token_reuses_existing_request(self):
        form_response = self.client.get(reverse("remote_access_request"))
        submission_token = form_response.context["submission_token"]

        first_response = self.client.post(
            reverse("remote_access_request"),
            data=self._request_payload(submission_token=submission_token),
        )
        second_response = self.client.post(
            reverse("remote_access_request"),
            data=self._request_payload(submission_token=submission_token),
        )

        ticket = Ticket.objects.get(subject="Remote Access Request")
        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(Ticket.objects.filter(subject="Remote Access Request").count(), 1)
        self.assertEqual(second_response.url, reverse("ticket_detail", args=[ticket.id]))
        self.assertEqual(len(mail.outbox), 1)

    def test_support_requester_cannot_approve_their_own_remote_access_request(self):
        support_requester = get_user_model().objects.create_user(
            username="support_remote_requester",
            email="support_remote_requester@bestfinance.com.np",
            password="testpass123",
            branch="Kathmandu",
            is_staff=True,
        )
        approver = get_user_model().objects.create_user(
            username="nikki.shrestha",
            email="nikki.shrestha@bestfinance.com.np",
            password="testpass123",
            branch="Kathmandu",
        )

        support_client = Client()
        support_client.force_login(support_requester)
        create_response = support_client.post(
            reverse("remote_access_request"),
            data={
                "subject": "Something else",
                "details": "Need remote access for a third-party vendor.",
                "recommender": "",
                "approver": str(approver.id),
            },
        )
        ticket = Ticket.objects.get(subject="Remote Access Request", created_by=support_requester)

        requester_detail = support_client.get(reverse("ticket_detail", args=[ticket.id]))
        approver_client = Client()
        approver_client.force_login(approver)
        approver_detail = approver_client.get(reverse("ticket_detail", args=[ticket.id]))

        decision_response = support_client.post(
            reverse("remote_access_approval_update", args=[ticket.id]),
            data={
                "decision": RemoteAccessApproval.STATUS_APPROVED,
                "decision_note": "This should not be allowed.",
            },
        )

        approval = RemoteAccessApproval.objects.get(ticket=ticket)
        self.assertEqual(create_response.status_code, 302)
        self.assertEqual(requester_detail.status_code, 200)
        self.assertEqual(approver_detail.status_code, 200)
        self.assertNotContains(requester_detail, "Approve Remote Access")
        self.assertContains(approver_detail, "Approve Remote Access")
        self.assertEqual(decision_response.status_code, 302)
        self.assertEqual(approval.status, RemoteAccessApproval.STATUS_PENDING_APPROVAL)
        self.assertIsNone(approval.decided_by_id)

    def test_non_approver_cannot_approve_request(self):
        self._create_request()
        ticket = Ticket.objects.get(subject="Remote Access Request")

        other_client = Client()
        other_client.force_login(self.other_user)
        response = other_client.post(
            reverse("remote_access_approval_update", args=[ticket.id]),
            data={
                "decision": RemoteAccessApproval.STATUS_REJECTED,
                "decision_note": "I should not be able to reject this.",
            },
        )

        approval = RemoteAccessApproval.objects.get(ticket=ticket)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(approval.status, RemoteAccessApproval.STATUS_PENDING_APPROVAL)
        self.assertIsNone(approval.decided_by_id)

    def test_remote_access_request_is_hidden_from_support_queue(self):
        self._create_request()
        ticket = Ticket.objects.get(subject="Remote Access Request")
        support_user = get_user_model().objects.create_user(
            username="remote_access_support",
            email="remote_access_support@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )

        support_client = Client()
        support_client.force_login(support_user)
        queue_response = support_client.get(reverse("support_queue"))
        dashboard_response = support_client.get(reverse("support_dashboard"))

        self.assertEqual(queue_response.status_code, 200)
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertNotContains(queue_response, ticket.ticket_id)
        self.assertNotContains(dashboard_response, ticket.ticket_id)


class DepartmentOwnershipTests(TestCase):
    def setUp(self):
        self.requester = get_user_model().objects.create_user(
            username="dept_requester",
            email="dept_requester@bestfinance.com.np",
            password="testpass123",
            department="Operations",
            branch="Kathmandu",
        )
        self.hr_user = get_user_model().objects.create_user(
            username="hr_owner",
            email="hr_owner@bestfinance.com.np",
            password="testpass123",
            department="HR",
            branch="Kathmandu",
        )
        self.hr_other_branch_user = get_user_model().objects.create_user(
            username="hr_other_branch",
            email="hr_other_branch@bestfinance.com.np",
            password="testpass123",
            department="HR",
            branch="Pokhara",
        )
        self.finance_user = get_user_model().objects.create_user(
            username="finance_owner",
            email="finance_owner@bestfinance.com.np",
            password="testpass123",
            department="Finance",
            branch="Kathmandu",
        )
        self.ticket = Ticket.objects.create(
            created_by=self.requester,
            subject="Department queue ticket",
            description="Ticket waiting for department ownership",
            priority="medium",
            status="new",
            department="HR",
        )

    def test_department_member_can_see_routed_ticket_in_ticket_list(self):
        self.client.force_login(self.hr_user)
        response = self.client.get(reverse("ticket_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.ticket.subject)
        self.assertContains(response, "Take Ownership")

    def test_non_department_user_cannot_open_department_ticket(self):
        self.client.force_login(self.finance_user)
        response = self.client.get(reverse("ticket_detail", args=[self.ticket.id]))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("ticket_list"), response.url)

    def test_same_department_other_branch_cannot_see_department_ticket(self):
        self.client.force_login(self.hr_other_branch_user)
        response = self.client.get(reverse("ticket_list"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.ticket.subject)

    def test_same_department_other_branch_cannot_open_department_ticket(self):
        self.client.force_login(self.hr_other_branch_user)
        response = self.client.get(reverse("ticket_detail", args=[self.ticket.id]))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("ticket_list"), response.url)

    def test_same_department_other_branch_cannot_take_ownership(self):
        self.client.force_login(self.hr_other_branch_user)
        response = self.client.post(reverse("ticket_claim", args=[self.ticket.id]), follow=True)

        self.assertEqual(response.status_code, 200)
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.assigned_to_id)

    def test_creator_still_sees_own_ticket_outside_department_queue_scope(self):
        created_ticket = Ticket.objects.create(
            created_by=self.hr_other_branch_user,
            subject="Own cross-department ticket",
            description="Creator should still see this ticket.",
            priority="medium",
            status="new",
            department="Finance",
            branch="Kathmandu",
        )

        self.client.force_login(self.hr_other_branch_user)
        response = self.client.get(reverse("ticket_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, created_ticket.subject)

    def test_assignee_still_sees_assigned_ticket_outside_department_queue_scope(self):
        assigned_ticket = Ticket.objects.create(
            created_by=self.requester,
            assigned_to=self.hr_other_branch_user,
            subject="Assigned cross-department ticket",
            description="Assignee should still see this ticket.",
            priority="medium",
            status="new",
            department="Finance",
            branch="Kathmandu",
        )

        self.client.force_login(self.hr_other_branch_user)
        response = self.client.get(reverse("ticket_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, assigned_ticket.subject)

    def test_department_visibility_uses_ticket_branch_snapshot(self):
        self.requester.branch = "Pokhara"
        self.requester.save(update_fields=["branch"])
        self.ticket.refresh_from_db()

        self.assertEqual(self.ticket.branch, "Kathmandu")

        self.client.force_login(self.hr_user)
        same_branch_response = self.client.get(reverse("ticket_detail", args=[self.ticket.id]))
        self.assertEqual(same_branch_response.status_code, 200)

        self.client.force_login(self.hr_other_branch_user)
        other_branch_response = self.client.get(reverse("ticket_detail", args=[self.ticket.id]))
        self.assertEqual(other_branch_response.status_code, 302)
        self.assertIn(reverse("ticket_list"), other_branch_response.url)

    def test_ticket_detail_shows_requester_branch_separately_from_ticket_branch(self):
        self.requester.branch = "Pokhara"
        self.requester.save(update_fields=["branch"])
        self.ticket.refresh_from_db()

        self.client.force_login(self.hr_user)
        response = self.client.get(reverse("ticket_detail", args=[self.ticket.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            '<div class="col-md-6"><div class="ticket-meta">Branch</div><div class="fw-semibold">Pokhara</div></div>',
            html=True,
        )
        self.assertContains(
            response,
            '<div class="col-md-6"><div class="ticket-meta">Responsible Branch</div><div class="fw-semibold">Kathmandu</div></div>',
            html=True,
        )

    def test_department_member_can_take_ownership(self):
        self.client.force_login(self.hr_user)
        response = self.client.post(reverse("ticket_claim", args=[self.ticket.id]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("ticket_update", args=[self.ticket.id]))
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.assigned_to_id, self.hr_user.id)
        self.assertEqual(TicketAssignmentLog.objects.filter(ticket=self.ticket).count(), 1)

    def test_requester_cannot_take_ownership_of_own_department_ticket(self):
        requester_ticket = Ticket.objects.create(
            created_by=self.requester,
            subject="Requester same department ticket",
            description="Requester should not claim own ticket",
            priority="medium",
            status="new",
            department="Operations",
        )
        self.client.force_login(self.requester)
        response = self.client.post(reverse("ticket_claim", args=[requester_ticket.id]), follow=True)

        self.assertEqual(response.status_code, 200)
        requester_ticket.refresh_from_db()
        self.assertIsNone(requester_ticket.assigned_to_id)
        self.assertContains(response, "You cannot take ownership of a ticket you created.")


class PrivateTicketChatAccessTests(TestCase):
    def setUp(self):
        self.requester = get_user_model().objects.create_user(
            username="private_requester",
            email="private_requester@bestfinance.com.np",
            password="testpass123",
            department="Operations",
            branch="Kathmandu",
        )
        self.assigned_user = get_user_model().objects.create_user(
            username="private_assignee",
            email="private_assignee@bestfinance.com.np",
            password="testpass123",
            department="HR",
            branch="Pokhara",
        )
        self.hr_peer = get_user_model().objects.create_user(
            username="private_hr_peer",
            email="private_hr_peer@bestfinance.com.np",
            password="testpass123",
            department="HR",
            branch="Kathmandu",
        )
        self.ticket = Ticket.objects.create(
            created_by=self.requester,
            assigned_to=self.assigned_user,
            subject="Private department chat",
            description="Chat should be restricted to the requester and assignee.",
            priority="medium",
            status="new",
            department="HR",
        )
        TicketMessage.objects.create(
            ticket=self.ticket,
            author=self.requester,
            body="Private troubleshooting note.",
        )

    def test_private_chat_hides_messages_from_other_department_users(self):
        self.ticket.chat_is_private = True
        self.ticket.save()

        self.client.force_login(self.hr_peer)
        response = self.client.get(reverse("ticket_detail", args=[self.ticket.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This ticket chat is private.")
        self.assertNotContains(response, "Private troubleshooting note.")
        mark_seen_response = self.client.post(reverse("ticket_chat_mark_seen", args=[self.ticket.id]))
        self.assertEqual(mark_seen_response.status_code, 403)

    def test_requester_can_enable_and_disable_private_chat(self):
        self.client.force_login(self.requester)
        enable_response = self.client.post(
            reverse("ticket_chat_privacy_update", args=[self.ticket.id]),
            {"chat_is_private": "on"},
        )

        self.assertEqual(enable_response.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertTrue(self.ticket.chat_is_private)

        disable_response = self.client.post(
            reverse("ticket_chat_privacy_update", args=[self.ticket.id]),
            {},
        )

        self.assertEqual(disable_response.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertFalse(self.ticket.chat_is_private)

    def test_private_chat_still_allows_assigned_user(self):
        self.ticket.chat_is_private = True
        self.ticket.save()

        self.client.force_login(self.assigned_user)
        response = self.client.get(reverse("ticket_detail", args=[self.ticket.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Private troubleshooting note.")

    def test_it_support_user_cannot_change_private_chat_setting(self):
        it_support_user = get_user_model().objects.create_user(
            username="private_it_support",
            password="testpass123",
            is_itsupport=True,
        )
        self.ticket.chat_is_private = True
        self.ticket.save()

        self.client.force_login(it_support_user)
        response = self.client.post(
            reverse("ticket_chat_privacy_update", args=[self.ticket.id]),
            {},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.ticket.refresh_from_db()
        self.assertTrue(self.ticket.chat_is_private)
        self.assertContains(response, "You do not have permission to manage chat privacy for this ticket.")

    def test_admin_user_can_change_private_chat_setting(self):
        admin_user = get_user_model().objects.create_user(
            username="private_admin",
            password="testpass123",
            is_staff=True,
        )
        self.ticket.chat_is_private = True
        self.ticket.save()

        self.client.force_login(admin_user)
        response = self.client.post(
            reverse("ticket_chat_privacy_update", args=[self.ticket.id]),
            {},
        )

        self.assertEqual(response.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertFalse(self.ticket.chat_is_private)


class TicketUnreadMessageIndicatorTests(TestCase):
    def setUp(self):
        self.requester = get_user_model().objects.create_user(
            username="unread_requester",
            password="testpass123",
        )
        self.agent = get_user_model().objects.create_user(
            username="unread_agent",
            password="testpass123",
            is_itsupport=True,
        )
        self.ticket = Ticket.objects.create(
            created_by=self.requester,
            assigned_to=self.agent,
            subject="Unread message test",
            description="Unread marker should appear in ticket list",
            priority="medium",
            status="new",
        )

    def test_ticket_list_shows_new_message_for_unseen_other_message(self):
        TicketMessage.objects.create(ticket=self.ticket, author=self.agent, body="Please check again.")

        self.client.force_login(self.requester)
        response = self.client.get(reverse("ticket_list"))
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.ticket.subject)
        self.assertTrue(response.context["tickets"][0].has_unread_messages)
        self.assertIn(">New message</span>", content)

    def test_opening_ticket_detail_marks_messages_as_seen(self):
        TicketMessage.objects.create(ticket=self.ticket, author=self.agent, body="Please check again.")

        self.client.force_login(self.requester)
        response = self.client.get(reverse("ticket_detail", args=[self.ticket.id]))

        self.assertEqual(response.status_code, 200)
        read_state = TicketChatReadState.objects.get(ticket=self.ticket, user=self.requester)
        self.assertIsNotNone(read_state.last_seen_at)

        response = self.client.get(reverse("ticket_list"))
        content = response.content.decode("utf-8")
        self.assertFalse(response.context["tickets"][0].has_unread_messages)
        self.assertNotIn(">New message</span>", content)

    def test_own_message_does_not_create_unread_indicator(self):
        TicketMessage.objects.create(ticket=self.ticket, author=self.requester, body="I added this myself.")

        self.client.force_login(self.requester)
        response = self.client.get(reverse("ticket_list"))
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["tickets"][0].has_unread_messages)
        self.assertNotIn(">New message</span>", content)


class ClosedTicketChatLockTests(TestCase):
    def setUp(self):
        self.requester = get_user_model().objects.create_user(
            username="closed_requester",
            password="testpass123",
        )
        self.agent = get_user_model().objects.create_user(
            username="closed_agent",
            password="testpass123",
            is_itsupport=True,
        )
        self.ticket = Ticket.objects.create(
            created_by=self.requester,
            assigned_to=self.agent,
            subject="Closed chat lock test",
            description="Closed tickets should be read-only in chat",
            priority="medium",
            status="closed",
        )

    def test_closed_ticket_detail_disables_chat_controls(self):
        self.client.force_login(self.requester)

        response = self.client.get(reverse("ticket_detail", args=[self.ticket.id]))
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Chat is disabled for closed tickets.")
        self.assertIn('id="chat-message-submit" class="btn btn-primary" type="button" disabled', content)
        self.assertIn('id="chat-file-submit" class="btn btn-outline-primary" type="button" disabled', content)

    def test_closed_ticket_attachment_upload_is_blocked(self):
        self.client.force_login(self.requester)

        response = self.client.post(
            reverse("ticket_attachment_upload", args=[self.ticket.id]),
            {"file": SimpleUploadedFile("note.txt", b"hello", content_type="text/plain")},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {"ok": False, "error": "Chat is disabled for closed tickets."},
        )
        self.assertEqual(TicketMessage.objects.filter(ticket=self.ticket).count(), 0)

    def test_closed_ticket_message_delete_is_blocked(self):
        message = TicketMessage.objects.create(
            ticket=self.ticket,
            author=self.requester,
            body="Do not delete after close.",
        )
        self.client.force_login(self.requester)

        response = self.client.post(
            reverse("ticket_chat_message_delete", args=[self.ticket.id, message.id]),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {"ok": False, "error": "Chat is disabled for closed tickets."},
        )
        self.assertTrue(TicketMessage.objects.filter(id=message.id).exists())

    @override_settings(
        WEBRTC_ICE_SERVERS=[
            {"urls": ["stun:stun.example.com:3478"]},
            {
                "urls": ["turns:turn.example.com:5349?transport=tcp"],
                "username": "turn_user",
                "credential": "turn_password",
            },
        ]
    )
    def test_ticket_detail_embeds_webrtc_ice_servers(self):
        self.client.force_login(self.requester)

        response = self.client.get(reverse("ticket_detail", args=[self.ticket.id]))
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["webrtc_ice_servers_json"], json.dumps(settings.WEBRTC_ICE_SERVERS))
        self.assertIn("turns:turn.example.com:5349?transport=tcp", response.context["webrtc_ice_servers_json"])
        self.assertIn("stun:stun.example.com:3478", content)
        self.assertIn("turn_user", content)

    @override_settings(
        WEBRTC_ICE_SERVERS=[],
        WEBRTC_USE_HOST_TURN_FALLBACK=True,
        WEBRTC_STUN_PORT=3478,
        WEBRTC_TURN_PORT=3478,
        WEBRTC_TURNS_PORT=5349,
        WEBRTC_TURN_USERNAME="turn_user",
        WEBRTC_TURN_PASSWORD="turn_password",
        WEBRTC_TURN_CREDENTIAL_TYPE="",
    )
    def test_ticket_detail_uses_same_host_for_turn_fallback(self):
        self.client.force_login(self.requester)

        response = self.client.get(
            reverse("ticket_detail", args=[self.ticket.id]),
            HTTP_HOST="192.168.0.103",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("stun:192.168.0.103:3478", response.context["webrtc_ice_servers_json"])
        self.assertIn("turn:192.168.0.103:3478?transport=udp", response.context["webrtc_ice_servers_json"])
        self.assertIn("turns:192.168.0.103:5349?transport=tcp", response.context["webrtc_ice_servers_json"])

    @override_settings(
        WEBRTC_ICE_SERVERS=[],
        WEBRTC_USE_HOST_TURN_FALLBACK=True,
        WEBRTC_STUN_PORT=3478,
        WEBRTC_TURN_PORT=3478,
        WEBRTC_TURNS_PORT=5349,
        WEBRTC_TURN_USERNAME="",
        WEBRTC_TURN_PASSWORD="",
        WEBRTC_TURN_AUTH_SECRET="shared-turn-secret",
        WEBRTC_TURN_CREDENTIAL_TTL_SECONDS=600,
        WEBRTC_TURN_CREDENTIAL_TYPE="",
    )
    @patch("tickets.views.time.time", return_value=1_700_000_000)
    def test_ticket_detail_uses_temporary_turn_credentials(self, mocked_time):
        self.client.force_login(self.requester)

        response = self.client.get(
            reverse("ticket_detail", args=[self.ticket.id]),
            HTTP_HOST="192.168.0.103",
        )

        self.assertEqual(response.status_code, 200)
        expected_username = f"1700000600:{self.requester.username}"
        expected_credential = base64.b64encode(
            hmac.new(
                b"shared-turn-secret",
                expected_username.encode("utf-8"),
                hashlib.sha1,
            ).digest()
        ).decode("ascii")
        self.assertIn(expected_username, response.context["webrtc_ice_servers_json"])
        self.assertIn(expected_credential, response.context["webrtc_ice_servers_json"])
        self.assertIn("turn:192.168.0.103:3478?transport=udp", response.context["webrtc_ice_servers_json"])


class ClosedTicketAssigneeDisplayTests(TestCase):
    def setUp(self):
        self.requester = get_user_model().objects.create_user(
            username="closed_display_requester",
            password="testpass123",
        )
        self.agent = get_user_model().objects.create_user(
            username="closed_display_agent",
            password="testpass123",
            is_itsupport=True,
        )
        self.support_viewer = get_user_model().objects.create_user(
            username="closed_display_support",
            password="testpass123",
            is_itsupport=True,
        )
        self.ticket = Ticket.objects.create(
            created_by=self.requester,
            assigned_to=self.agent,
            subject="Closed assignee display",
            description="Closed tickets should still show the last handler.",
            priority="medium",
            status="new",
        )
        self.ticket.status = "resolved"
        self.ticket.save()
        self.ticket.status = "closed"
        self.ticket.save()
        self.ticket.refresh_from_db()

    def test_closed_ticket_display_assignee_uses_assignment_history(self):
        self.assertIsNone(self.ticket.assigned_to_id)
        self.assertEqual(self.ticket.display_assignee, self.agent)

    def test_active_unassigned_ticket_does_not_show_previous_assignee(self):
        ticket = Ticket.objects.create(
            created_by=self.requester,
            assigned_to=self.agent,
            subject="Active unassigned ticket",
            description="Active tickets should remain visibly unassigned.",
            priority="low",
            status="new",
        )
        ticket.assigned_to = None
        ticket.save()
        ticket.refresh_from_db()

        self.assertIsNone(ticket.display_assignee)

    def test_ticket_detail_shows_last_assignee_for_closed_ticket(self):
        self.client.force_login(self.requester)
        response = self.client.get(reverse("ticket_detail", args=[self.ticket.id]))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.ticket.resolved_by_id)
        self.assertEqual(response.context["ticket"].display_assignee, self.agent)
        self.assertEqual(response.context["ticket"].display_resolved_by, self.agent)
        self.assertContains(response, f"Assigned: {self.agent.username}")
        self.assertNotContains(response, "Assigned: Unassigned")

    def test_ticket_list_shows_last_assignee_for_closed_ticket(self):
        self.client.force_login(self.requester)
        response = self.client.get(reverse("ticket_list"), {"status": "closed"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([ticket.id for ticket in response.context["tickets"]], [self.ticket.id])
        self.assertEqual(response.context["tickets"][0].display_assignee, self.agent)
        self.assertContains(response, f"Assigned: {self.agent.username}")
        self.assertNotContains(response, "Assigned: Unassigned")

    def test_support_queue_shows_last_assignee_for_closed_ticket(self):
        self.client.force_login(self.support_viewer)
        response = self.client.get(reverse("support_queue"), {"status_group": "closed"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([ticket.id for ticket in response.context["tickets"]], [self.ticket.id])
        self.assertEqual(response.context["tickets"][0].display_assignee, self.agent)
        self.assertContains(response, self.agent.username)

    def test_support_dashboard_shows_last_assignee_for_closed_ticket(self):
        self.client.force_login(self.support_viewer)
        response = self.client.get(reverse("support_dashboard"), {"status_group": "closed"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([ticket.id for ticket in response.context["recent_tickets"]], [self.ticket.id])
        self.assertEqual(response.context["recent_tickets"][0].display_assignee, self.agent)
        self.assertContains(response, self.agent.username)


class TicketChatAttachmentUploadTests(TestCase):
    def setUp(self):
        self.requester = get_user_model().objects.create_user(
            username="chat_attachment_requester",
            password="testpass123",
        )
        self.agent = get_user_model().objects.create_user(
            username="chat_attachment_agent",
            password="testpass123",
            is_itsupport=True,
        )
        self.ticket = Ticket.objects.create(
            created_by=self.requester,
            assigned_to=self.agent,
            subject="Chat attachment upload",
            description="Chat attachments should support small batches.",
            priority="medium",
            status="in_progress",
        )
        self.client.force_login(self.requester)

    @patch("tickets.views.get_s3_client")
    @patch("tickets.views.get_minio_config")
    def test_ticket_attachment_upload_accepts_multiple_files_up_to_limit(
        self,
        mock_get_minio_config,
        mock_get_s3_client,
    ):
        mock_get_minio_config.return_value = Mock(bucket="ticket-files")
        mock_s3 = Mock()
        mock_get_s3_client.return_value = mock_s3

        response = self.client.post(
            reverse("ticket_attachment_upload", args=[self.ticket.id]),
            {
                "file": [
                    SimpleUploadedFile("one.txt", b"one", content_type="text/plain"),
                    SimpleUploadedFile("two.txt", b"two", content_type="text/plain"),
                ]
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["events"]), 2)
        self.assertEqual(mock_s3.upload_fileobj.call_count, 2)
        self.assertEqual(TicketMessage.objects.filter(ticket=self.ticket).count(), 2)
        self.assertEqual(TicketMessageAttachment.objects.filter(ticket=self.ticket).count(), 2)
        filenames = [event["attachment"]["filename"] for event in payload["events"]]
        self.assertEqual(filenames, ["one.txt", "two.txt"])

    def test_ticket_attachment_upload_rejects_more_than_five_files(self):
        response = self.client.post(
            reverse("ticket_attachment_upload", args=[self.ticket.id]),
            {
                "file": [
                    SimpleUploadedFile(f"file-{index}.txt", b"x", content_type="text/plain")
                    for index in range(6)
                ]
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {"ok": False, "error": "You can upload up to 5 attachments at once."},
        )
        self.assertEqual(TicketMessage.objects.filter(ticket=self.ticket).count(), 0)
        self.assertEqual(TicketMessageAttachment.objects.filter(ticket=self.ticket).count(), 0)


class TicketChatMessageDeleteTests(TestCase):
    def setUp(self):
        self.requester = get_user_model().objects.create_user(
            username="chat_delete_requester",
            password="testpass123",
        )
        self.agent = get_user_model().objects.create_user(
            username="chat_delete_agent",
            password="testpass123",
            is_itsupport=True,
        )
        self.ticket = Ticket.objects.create(
            created_by=self.requester,
            assigned_to=self.agent,
            subject="Chat delete test",
            description="Own messages should be deletable.",
            priority="medium",
            status="in_progress",
        )

    def test_author_can_delete_own_chat_message(self):
        message = TicketMessage.objects.create(
            ticket=self.ticket,
            author=self.requester,
            body="Please remove this message.",
        )
        self.client.force_login(self.requester)

        response = self.client.post(
            reverse("ticket_chat_message_delete", args=[self.ticket.id, message.id]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "deleted_message_id": message.id})
        self.assertFalse(TicketMessage.objects.filter(id=message.id).exists())

    def test_user_cannot_delete_other_users_chat_message(self):
        message = TicketMessage.objects.create(
            ticket=self.ticket,
            author=self.agent,
            body="Support reply should stay.",
        )
        self.client.force_login(self.requester)

        response = self.client.post(
            reverse("ticket_chat_message_delete", args=[self.ticket.id, message.id]),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"ok": False, "error": "You can delete only your own chat messages."},
        )
        self.assertTrue(TicketMessage.objects.filter(id=message.id).exists())

    @patch("tickets.views._try_delete_minio_objects")
    def test_deleting_attachment_message_removes_attachment_record(self, mocked_delete_objects):
        message = TicketMessage.objects.create(
            ticket=self.ticket,
            author=self.requester,
            body="Attachment uploaded: receipt.pdf",
        )
        attachment = TicketMessageAttachment.objects.create(
            ticket=self.ticket,
            message=message,
            uploaded_by=self.requester,
            object_key="tickets/test/receipt.pdf",
            filename="receipt.pdf",
            content_type="application/pdf",
            size=123,
        )
        self.client.force_login(self.requester)

        response = self.client.post(
            reverse("ticket_chat_message_delete", args=[self.ticket.id, message.id]),
        )

        self.assertEqual(response.status_code, 200)
        mocked_delete_objects.assert_called_once_with([attachment.object_key])
        self.assertFalse(TicketMessage.objects.filter(id=message.id).exists())
        self.assertFalse(TicketMessageAttachment.objects.filter(id=attachment.id).exists())

    def test_ticket_detail_shows_delete_button_only_for_own_messages(self):
        own_message = TicketMessage.objects.create(
            ticket=self.ticket,
            author=self.requester,
            body="This one is mine.",
        )
        other_message = TicketMessage.objects.create(
            ticket=self.ticket,
            author=self.agent,
            body="This one is not mine.",
        )
        self.client.force_login(self.requester)

        response = self.client.get(reverse("ticket_detail", args=[self.ticket.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'data-chat-delete-id="{own_message.id}"')
        self.assertNotContains(response, f'data-chat-delete-id="{other_message.id}"')


class TicketResolvedEmailTests(TestCase):
    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_resolving_ticket_sends_email_to_requester(self):
        requester = get_user_model().objects.create_user(
            username="requester",
            email="requester@bestfinance.com.np",
            password="testpass123",
        )
        agent = get_user_model().objects.create_user(
            username="agent",
            email="agent@bestfinance.com.np",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Resolve email test",
            description="Test ticket",
            priority="low",
            status="new",
            assigned_to=agent,
        )

        self.client.force_login(agent)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={"status": "resolved", "status_note": "Issue fixed and verified."},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(ticket.ticket_id, mail.outbox[0].subject)
        self.assertIn(requester.email, mail.outbox[0].to)
        self.assertIn("/close/", mail.outbox[0].body)
        self.assertIn("Issue fixed and verified.", mail.outbox[0].body)

        ticket.refresh_from_db()
        self.assertEqual(ticket.resolved_note, "Issue fixed and verified.")

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_resolving_ticket_sends_cc_emails(self):
        requester = get_user_model().objects.create_user(
            username="requester_cc",
            email="requester_cc@bestfinance.com.np",
            password="testpass123",
        )
        agent = get_user_model().objects.create_user(
            username="agent_cc",
            email="agent_cc@bestfinance.com.np",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Resolve email cc test",
            description="Test ticket",
            priority="low",
            status="new",
            assigned_to=agent,
            cc_emails="manager@bestfinance.com.np, audit@bestfinance.com.np",
        )

        self.client.force_login(agent)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={"status": "resolved", "status_note": "Issue fixed and cc copied."},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [requester.email])
        self.assertEqual(
            mail.outbox[0].cc,
            ["manager@bestfinance.com.np", "audit@bestfinance.com.np"],
        )

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_resolving_ticket_can_update_cc_emails_from_resolve_form(self):
        requester = get_user_model().objects.create_user(
            username="requester_cc_update",
            email="requester_cc_update@bestfinance.com.np",
            password="testpass123",
        )
        agent = get_user_model().objects.create_user(
            username="agent_cc_update",
            email="agent_cc_update@bestfinance.com.np",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Resolve email cc update test",
            description="Test ticket",
            priority="low",
            status="new",
            assigned_to=agent,
            cc_emails="oldmanager@bestfinance.com.np",
        )

        self.client.force_login(agent)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={
                "status": "resolved",
                "status_note": "Issue fixed and updated cc copied.",
                "status_cc_emails": "manager@bestfinance.com.np; audit@bestfinance.com.np",
            },
        )

        ticket.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ticket.cc_emails, "manager@bestfinance.com.np, audit@bestfinance.com.np")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].cc, ["manager@bestfinance.com.np", "audit@bestfinance.com.np"])

    def test_resolving_ticket_rejects_invalid_status_cc_email(self):
        requester = get_user_model().objects.create_user(
            username="requester_invalid_cc",
            email="requester_invalid_cc@bestfinance.com.np",
            password="testpass123",
        )
        agent = get_user_model().objects.create_user(
            username="agent_invalid_cc",
            email="agent_invalid_cc@bestfinance.com.np",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Resolve invalid cc test",
            description="Test ticket",
            priority="low",
            status="new",
            assigned_to=agent,
        )

        self.client.force_login(agent)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={
                "status": "resolved",
                "status_note": "Issue fixed but cc is invalid.",
                "status_cc_emails": "manager@bestfinance.com.np, not-an-email",
            },
        )

        ticket.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertIn("status_cc_emails", response.context["form"].errors)
        self.assertEqual(ticket.status, "new")
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_resolving_ticket_with_csrf_enabled_does_not_server_error(self):
        requester = get_user_model().objects.create_user(
            username="requester_csrf",
            email="requester_csrf@bestfinance.com.np",
            password="testpass123",
        )
        agent = get_user_model().objects.create_user(
            username="agent_csrf",
            email="agent_csrf@bestfinance.com.np",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Resolve csrf test",
            description="Test ticket",
            priority="low",
            status="new",
            assigned_to=agent,
        )

        client = Client(enforce_csrf_checks=True)
        client.force_login(agent)
        get_response = client.get(reverse("ticket_update", args=[ticket.id]))
        self.assertEqual(get_response.status_code, 200)

        csrf_token = client.cookies["csrftoken"].value
        response = client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={"status": "resolved", "status_note": "Resolved with csrf enabled."},
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        self.assertEqual(response.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "resolved")
        self.assertEqual(ticket.resolved_note, "Resolved with csrf enabled.")

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_resolving_ticket_sends_multiple_uploaded_attachments_in_email(self):
        requester = get_user_model().objects.create_user(
            username="resolve_requester_multi",
            email="resolve_requester_multi@bestfinance.com.np",
            password="testpass123",
        )
        agent = get_user_model().objects.create_user(
            username="resolve_agent_multi",
            email="resolve_agent_multi@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Resolve email attachment test",
            description="Test ticket",
            priority="low",
            status="new",
            assigned_to=agent,
        )

        self.client.force_login(agent)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={
                "status": "resolved",
                "priority": "low",
                "assigned_to": agent.id,
                "status_note": "Please review the attached resolution files.",
                "status_email_attachments": [
                    SimpleUploadedFile("resolution-summary.txt", b"summary", content_type="text/plain"),
                    SimpleUploadedFile("resolution-checklist.pdf", b"%PDF-1.4", content_type="application/pdf"),
                ],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("2 attachments are included with this email.", mail.outbox[0].body)
        attachment_names = [attachment[0] for attachment in mail.outbox[0].attachments]
        self.assertCountEqual(
            attachment_names,
            ["resolution-summary.txt", "resolution-checklist.pdf"],
        )

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    @patch("tickets.views._use_request_only_upload_handlers")
    def test_resolving_ticket_uses_request_only_upload_handler_for_email_attachments(self, mocked_upload_handlers):
        requester = get_user_model().objects.create_user(
            username="resolve_requester_memory",
            email="resolve_requester_memory@bestfinance.com.np",
            password="testpass123",
        )
        agent = get_user_model().objects.create_user(
            username="resolve_agent_memory",
            email="resolve_agent_memory@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Resolve email memory upload test",
            description="Test ticket",
            priority="low",
            status="new",
            assigned_to=agent,
        )

        self.client.force_login(agent)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={
                "status": "resolved",
                "priority": "low",
                "assigned_to": agent.id,
                "status_note": "Memory-only resolved email.",
                "status_email_attachments": [
                    SimpleUploadedFile("memory-only.txt", b"memory", content_type="text/plain"),
                ],
            },
        )

        self.assertEqual(response.status_code, 302)
        mocked_upload_handlers.assert_called_once()


class TicketAssignmentHistoryTests(TestCase):
    def test_assignment_log_stops_at_ticket_resolved_time_when_unassigned_at_is_missing(self):
        requester = get_user_model().objects.create_user(
            username="assignment_requester",
            password="testpass123",
        )
        assignee = get_user_model().objects.create_user(
            username="assignment_assignee",
            password="testpass123",
            is_itsupport=True,
        )
        assigned_at = timezone.now() - timedelta(hours=3)
        resolved_at = assigned_at + timedelta(hours=2, minutes=15)
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Historical assignment duration",
            description="Duration should stop at resolved time.",
            priority="low",
            status="resolved",
            resolved_at=resolved_at,
            assigned_to=None,
        )
        log = TicketAssignmentLog.objects.create(
            ticket=ticket,
            assigned_to=assignee,
            assigned_by=assignee,
            assigned_at=assigned_at,
            unassigned_at=None,
        )

        self.assertEqual(log.effective_unassigned_at, resolved_at)
        self.assertEqual(log.duration, resolved_at - assigned_at)
        self.assertEqual(log.formatted_duration(), "2h 15m")
        self.assertEqual(log.history_status, "resolved")

    def test_ticket_detail_shows_effective_unassigned_time_in_assignment_history(self):
        requester = get_user_model().objects.create_user(
            username="assignment_detail_requester",
            password="testpass123",
        )
        assignee = get_user_model().objects.create_user(
            username="assignment_detail_assignee",
            password="testpass123",
        )
        assigned_at = timezone.now() - timedelta(hours=2)
        resolved_at = assigned_at + timedelta(hours=1, minutes=10)
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Assignment history detail view",
            description="Assignment history should not show the active marker after resolve.",
            priority="low",
            status="new",
            assigned_to=assignee,
        )
        log = TicketAssignmentLog.objects.get(ticket=ticket)
        Ticket.objects.filter(pk=ticket.pk).update(
            status="resolved",
            resolved_at=resolved_at,
            assigned_to_id=None,
        )
        TicketAssignmentLog.objects.filter(pk=log.pk).update(
            assigned_at=assigned_at,
            unassigned_at=None,
        )

        self.client.force_login(requester)
        response = self.client.get(reverse("ticket_detail", args=[ticket.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<th>Status</th>", html=True)
        self.assertNotContains(response, "Current")
        self.assertEqual(response.context["assignment_logs"][0].effective_unassigned_at, resolved_at)
        self.assertEqual(response.context["assignment_logs"][0].history_status, "resolved")

    def test_assignment_log_status_updates_for_active_assignment(self):
        requester = get_user_model().objects.create_user(
            username="assignment_status_requester",
            password="testpass123",
        )
        assignee = get_user_model().objects.create_user(
            username="assignment_status_assignee",
            password="testpass123",
            is_itsupport=True,
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Assignment log status sync",
            description="Active assignment history should reflect the latest status.",
            priority="low",
            status="new",
            assigned_to=assignee,
        )
        log = TicketAssignmentLog.objects.get(ticket=ticket)

        ticket.status = "waiting_on_user"
        ticket.save()
        log.refresh_from_db()

        self.assertEqual(log.status, "waiting_on_user")
        self.assertEqual(log.history_status, "waiting_on_user")

    def test_legacy_closed_assignment_row_does_not_inherit_current_ticket_status(self):
        requester = get_user_model().objects.create_user(
            username="assignment_legacy_requester",
            password="testpass123",
        )
        assignee = get_user_model().objects.create_user(
            username="assignment_legacy_assignee",
            password="testpass123",
            is_itsupport=True,
        )
        assigned_at = timezone.now() - timedelta(hours=2)
        unassigned_at = assigned_at + timedelta(minutes=20)
        resolved_at = assigned_at + timedelta(hours=1, minutes=30)
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Legacy assignment status row",
            description="Older rows should not inherit the ticket's final status.",
            priority="low",
            status="resolved",
            resolved_at=resolved_at,
            assigned_to=None,
        )
        log = TicketAssignmentLog.objects.create(
            ticket=ticket,
            assigned_to=assignee,
            assigned_by=assignee,
            assigned_at=assigned_at,
            unassigned_at=unassigned_at,
            status="",
        )

        self.assertEqual(log.history_status, "")
        self.assertEqual(log.history_status_display, "")

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_resolving_ticket_unassigns_and_closes_assignment_log(self):
        requester = get_user_model().objects.create_user(
            username="resolve_history_requester",
            email="resolve_history_requester@bestfinance.com.np",
            password="testpass123",
        )
        assignee = get_user_model().objects.create_user(
            username="resolve_history_assignee",
            email="resolve_history_assignee@bestfinance.com.np",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Resolve history ticket",
            description="Assignment should close on resolve.",
            priority="low",
            status="new",
            assigned_to=assignee,
        )

        self.client.force_login(assignee)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={"status": "resolved", "status_note": "Resolved and unassigned."},
        )

        self.assertEqual(response.status_code, 302)
        ticket.refresh_from_db()
        log = TicketAssignmentLog.objects.get(ticket=ticket)
        self.assertEqual(ticket.status, "resolved")
        self.assertIsNone(ticket.assigned_to_id)
        self.assertEqual(log.unassigned_at, ticket.resolved_at)
        self.assertEqual(log.status, "resolved")

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_closing_resolved_ticket_unassigns_historical_open_assignment_at_resolved_time(self):
        requester = get_user_model().objects.create_user(
            username="close_history_requester",
            email="close_history_requester@bestfinance.com.np",
            password="testpass123",
        )
        assignee = get_user_model().objects.create_user(
            username="close_history_assignee",
            email="close_history_assignee@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        resolved_at = timezone.now() - timedelta(hours=1)
        assigned_at = resolved_at - timedelta(hours=4, minutes=30)
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Close historical assignment",
            description="Historical open assignment should stop at resolve time.",
            priority="low",
            status="new",
            assigned_to=assignee,
        )
        log = TicketAssignmentLog.objects.get(ticket=ticket)
        Ticket.objects.filter(pk=ticket.pk).update(
            status="resolved",
            resolved_at=resolved_at,
            assigned_to_id=assignee.id,
        )
        TicketAssignmentLog.objects.filter(pk=log.pk).update(
            assigned_at=assigned_at,
            unassigned_at=None,
        )
        ticket.refresh_from_db()
        log.refresh_from_db()

        self.client.force_login(assignee)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={
                "status": "closed",
                "priority": "low",
                "assigned_to": assignee.id,
                "status_note": "Closed after confirmation.",
            },
        )

        self.assertEqual(response.status_code, 302)
        ticket.refresh_from_db()
        log.refresh_from_db()
        self.assertEqual(ticket.status, "closed")
        self.assertIsNone(ticket.assigned_to_id)
        self.assertEqual(log.unassigned_at, resolved_at)
        self.assertEqual(log.duration, resolved_at - assigned_at)
        self.assertEqual(log.status, "resolved")


class TicketOverdueAttentionTests(TestCase):
    def test_overdue_attention_level_flags_unresolved_critical_after_one_day(self):
        requester = get_user_model().objects.create_user(
            username="overdue_requester_critical",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Critical overdue",
            description="Critical unresolved ticket",
            priority="critical",
            status="in_progress",
        )
        Ticket.objects.filter(pk=ticket.pk).update(created_at=timezone.now() - timedelta(days=2))
        ticket.refresh_from_db()

        self.assertEqual(ticket.overdue_attention_level, "critical")
        self.assertEqual(ticket.overdue_attention_label, "Critical overdue")

    def test_overdue_attention_level_flags_unresolved_high_after_three_days(self):
        requester = get_user_model().objects.create_user(
            username="overdue_requester_high",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="High overdue",
            description="High unresolved ticket",
            priority="high",
            status="waiting_on_user",
        )
        Ticket.objects.filter(pk=ticket.pk).update(created_at=timezone.now() - timedelta(days=4))
        ticket.refresh_from_db()

        self.assertEqual(ticket.overdue_attention_level, "high")
        self.assertEqual(ticket.overdue_attention_label, "High overdue")

        ticket.status = "resolved"
        ticket.save()
        self.assertEqual(ticket.overdue_attention_level, "")

    def test_support_queue_renders_overdue_alerts_for_critical_and_high_tickets(self):
        support_user = get_user_model().objects.create_user(
            username="overdue_support",
            password="testpass123",
            is_itsupport=True,
        )
        requester = get_user_model().objects.create_user(
            username="overdue_requester_queue",
            password="testpass123",
        )
        critical_ticket = Ticket.objects.create(
            created_by=requester,
            subject="Critical queue alert",
            description="Should flash as critical overdue.",
            priority="critical",
            status="in_progress",
        )
        high_ticket = Ticket.objects.create(
            created_by=requester,
            subject="High queue alert",
            description="Should flash as high overdue.",
            priority="high",
            status="waiting_on_third_party",
        )
        Ticket.objects.filter(pk=critical_ticket.pk).update(created_at=timezone.now() - timedelta(days=2))
        Ticket.objects.filter(pk=high_ticket.pk).update(created_at=timezone.now() - timedelta(days=4))

        self.client.force_login(support_user)
        response = self.client.get(reverse("support_queue"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ticket-overdue-critical")
        self.assertContains(response, "ticket-overdue-high")
        self.assertContains(response, "Critical overdue")
        self.assertContains(response, "High overdue")

    def test_overdue_attention_level_flags_unresolved_medium_and_low_after_five_days(self):
        requester = get_user_model().objects.create_user(
            username="overdue_requester_medium_low",
            password="testpass123",
        )
        medium_ticket = Ticket.objects.create(
            created_by=requester,
            subject="Medium overdue",
            description="Medium unresolved ticket",
            priority="medium",
            status="in_progress",
        )
        low_ticket = Ticket.objects.create(
            created_by=requester,
            subject="Low overdue",
            description="Low unresolved ticket",
            priority="low",
            status="new",
        )
        Ticket.objects.filter(pk=medium_ticket.pk).update(created_at=timezone.now() - timedelta(days=6))
        Ticket.objects.filter(pk=low_ticket.pk).update(created_at=timezone.now() - timedelta(days=6))
        medium_ticket.refresh_from_db()
        low_ticket.refresh_from_db()

        self.assertEqual(medium_ticket.overdue_attention_level, "medium")
        self.assertEqual(medium_ticket.overdue_attention_label, "Medium overdue")
        self.assertEqual(low_ticket.overdue_attention_level, "low")
        self.assertEqual(low_ticket.overdue_attention_label, "Low overdue")

    def test_support_queue_renders_overdue_alerts_for_medium_and_low_tickets(self):
        support_user = get_user_model().objects.create_user(
            username="overdue_support_medium_low",
            password="testpass123",
            is_itsupport=True,
        )
        requester = get_user_model().objects.create_user(
            username="overdue_requester_queue_medium_low",
            password="testpass123",
        )
        medium_ticket = Ticket.objects.create(
            created_by=requester,
            subject="Medium queue alert",
            description="Should flash as medium overdue.",
            priority="medium",
            status="waiting_on_user",
        )
        low_ticket = Ticket.objects.create(
            created_by=requester,
            subject="Low queue alert",
            description="Should flash as low overdue.",
            priority="low",
            status="new",
        )
        Ticket.objects.filter(pk=medium_ticket.pk).update(created_at=timezone.now() - timedelta(days=6))
        Ticket.objects.filter(pk=low_ticket.pk).update(created_at=timezone.now() - timedelta(days=6))

        self.client.force_login(support_user)
        response = self.client.get(reverse("support_queue"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ticket-overdue-medium")
        self.assertContains(response, "ticket-overdue-low")
        self.assertContains(response, "Medium overdue")
        self.assertContains(response, "Low overdue")


class TicketCloseViaEmailLinkTests(TestCase):
    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_requester_can_close_ticket_via_email_link_after_resolved(self):
        requester = get_user_model().objects.create_user(
            username="requester2",
            email="requester2@bestfinance.com.np",
            password="testpass123",
        )
        agent = get_user_model().objects.create_user(
            username="agent2",
            email="agent2@bestfinance.com.np",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Close via email link",
            description="Test ticket",
            priority="low",
            status="new",
            assigned_to=agent,
        )

        self.client.force_login(agent)
        self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={"status": "resolved", "status_note": "Resolved; please confirm and close."},
        )
        self.assertEqual(len(mail.outbox), 1)

        match = None
        for line in mail.outbox[0].body.splitlines():
            if "/close/" in line:
                match = line.strip()
                break
        self.assertIsNotNone(match)

        close_path = urlparse(match).path
        self.client.force_login(requester)
        response = self.client.get(close_path, follow=True)
        self.assertEqual(response.status_code, 200)

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "closed")
        self.assertEqual(ticket.closed_by_id, requester.id)
        self.assertEqual(ticket.resolved_by_id, agent.id)
        self.assertEqual(response.context["ticket"].resolved_by_id, agent.id)
        self.assertContains(response, "Resolved By")


class TicketResolvePermissionTests(TestCase):
    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_only_assigned_agent_can_mark_ticket_resolved(self):
        requester = get_user_model().objects.create_user(
            username="requester3",
            email="requester3@bestfinance.com.np",
            password="testpass123",
        )
        assignee = get_user_model().objects.create_user(
            username="assignee3",
            email="assignee3@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        other_support = get_user_model().objects.create_user(
            username="support3",
            email="support3@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Resolve permission test",
            description="Test ticket",
            priority="low",
            status="new",
            assigned_to=assignee,
        )

        self.client.force_login(other_support)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={
                "status": "resolved",
                "priority": "low",
                "assigned_to": assignee.id,
                "status_note": "Attempted resolve by non-assignee.",
            },
        )

        self.assertEqual(response.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "new")
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_admin_user_cannot_mark_other_users_ticket_resolved(self):
        requester = get_user_model().objects.create_user(
            username="requester_admin_resolve",
            email="requester_admin_resolve@bestfinance.com.np",
            password="testpass123",
        )
        assignee = get_user_model().objects.create_user(
            username="assignee_admin_resolve",
            email="assignee_admin_resolve@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        admin_user = get_user_model().objects.create_user(
            username="admin_resolve_attempt",
            email="admin_resolve_attempt@bestfinance.com.np",
            password="testpass123",
            is_staff=True,
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Admin resolve permission test",
            description="Only the assignee should resolve the ticket.",
            priority="low",
            status="new",
            assigned_to=assignee,
        )

        self.client.force_login(admin_user)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={
                "status": "resolved",
                "priority": "low",
                "assigned_to": assignee.id,
                "status_note": "Attempted resolve by admin.",
            },
        )

        self.assertEqual(response.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "new")
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_support_user_cannot_self_assign_and_resolve_in_same_update(self):
        requester = get_user_model().objects.create_user(
            username="requester_resolve_self_assign",
            email="requester_resolve_self_assign@bestfinance.com.np",
            password="testpass123",
        )
        assignee = get_user_model().objects.create_user(
            username="assignee_resolve_self_assign",
            email="assignee_resolve_self_assign@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        other_support = get_user_model().objects.create_user(
            username="support_resolve_self_assign",
            email="support_resolve_self_assign@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Resolve self-assign permission test",
            description="Test ticket",
            priority="low",
            status="in_progress",
            assigned_to=assignee,
        )

        self.client.force_login(other_support)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={
                "status": "resolved",
                "priority": "low",
                "assigned_to": other_support.id,
                "status_note": "Trying to self-assign and resolve.",
            },
        )

        self.assertEqual(response.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "in_progress")
        self.assertEqual(ticket.assigned_to_id, assignee.id)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_unassigned_ticket_cannot_be_marked_resolved(self):
        requester = get_user_model().objects.create_user(
            username="requester_unassigned_resolve",
            email="requester_unassigned_resolve@bestfinance.com.np",
            password="testpass123",
        )
        support_user = get_user_model().objects.create_user(
            username="support_unassigned_resolve",
            email="support_unassigned_resolve@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Unassigned resolve restriction",
            description="Test ticket",
            priority="low",
            status="in_progress",
            assigned_to=None,
        )

        self.client.force_login(support_user)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={
                "status": "resolved",
                "priority": "low",
                "status_note": "Attempted resolve while unassigned.",
            },
        )

        self.assertEqual(response.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "in_progress")
        self.assertEqual(len(mail.outbox), 0)


class TicketClosedUpdateBehaviorTests(TestCase):
    def _create_resolved_ticket_with_history(self, requester, assignee, **overrides):
        ticket = Ticket.objects.create(
            created_by=requester,
            subject=overrides.get("subject", "Resolved workflow ticket"),
            description=overrides.get("description", "Ticket resolved through the normal workflow."),
            priority=overrides.get("priority", "low"),
            status="new",
            assigned_to=assignee,
        )
        ticket.status = "resolved"
        ticket.resolved_at = overrides.get("resolved_at", timezone.now())
        ticket.resolved_by = overrides.get("resolved_by", assignee)
        ticket.save()
        ticket.refresh_from_db()
        return ticket

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_resolved_ticket_update_form_prefills_last_assignee(self):
        requester = get_user_model().objects.create_user(
            username="close_requester",
            email="close_requester@bestfinance.com.np",
            password="testpass123",
        )
        agent = get_user_model().objects.create_user(
            username="close_agent",
            email="close_agent@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        ticket = self._create_resolved_ticket_with_history(
            requester,
            agent,
            subject="Close email test",
            description="Test ticket",
        )

        self.assertIsNone(ticket.assigned_to_id)

        self.client.force_login(agent)
        response = self.client.get(
            reverse("ticket_update", args=[ticket.id]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["assigned_to"], agent.id)
        status_choices = {value for value, _label in response.context["form"].fields["status"].choices}
        self.assertEqual(status_choices, {"resolved", "closed"})

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_resolved_ticket_can_be_closed_using_assignment_history(self):
        requester = get_user_model().objects.create_user(
            username="close_requester_history",
            email="close_requester_history@bestfinance.com.np",
            password="testpass123",
        )
        agent = get_user_model().objects.create_user(
            username="close_agent_history",
            email="close_agent_history@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        ticket = self._create_resolved_ticket_with_history(
            requester,
            agent,
            subject="Close using assignment history",
            description="Resolved ticket should still close with its historical owner.",
        )

        self.client.force_login(agent)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={
                "status": "closed",
                "priority": "low",
                "status_note": "Closed after confirmation.",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "closed")
        self.assertEqual(ticket.closed_note, "Closed after confirmation.")
        self.assertEqual(ticket.closed_by_id, agent.id)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_support_user_who_is_not_ticket_assignee_cannot_close_resolved_ticket(self):
        requester = get_user_model().objects.create_user(
            username="close_requester_admin",
            email="close_requester_admin@bestfinance.com.np",
            password="testpass123",
        )
        assignee = get_user_model().objects.create_user(
            username="close_assignee_admin",
            email="close_assignee_admin@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        other_support_user = get_user_model().objects.create_user(
            username="close_other_support_user",
            email="close_other_support_user@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        ticket = self._create_resolved_ticket_with_history(
            requester,
            assignee,
            subject="Close support permission test",
            description="Only the last assignee or admin should close resolved tickets.",
        )

        self.client.force_login(other_support_user)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={
                "status": "closed",
                "priority": "low",
                "status_note": "Closed by another support user.",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "resolved")
        self.assertIsNone(ticket.closed_by_id)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_admin_can_close_resolved_ticket_when_it_has_assignment_history(self):
        requester = get_user_model().objects.create_user(
            username="close_requester_admin",
            email="close_requester_admin@bestfinance.com.np",
            password="testpass123",
        )
        assignee = get_user_model().objects.create_user(
            username="close_assignee_admin",
            email="close_assignee_admin@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        admin_user = get_user_model().objects.create_user(
            username="close_admin_user",
            email="close_admin_user@bestfinance.com.np",
            password="testpass123",
            is_staff=True,
        )
        ticket = self._create_resolved_ticket_with_history(
            requester,
            assignee,
            subject="Close admin permission test",
            description="Admin should be able to close a resolved ticket.",
        )

        self.client.force_login(admin_user)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={
                "status": "closed",
                "priority": "low",
                "status_note": "Closed by admin after review.",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "closed")
        self.assertEqual(ticket.closed_by_id, admin_user.id)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_closing_ticket_without_note_keeps_plain_close_and_sends_no_email(self):
        requester = get_user_model().objects.create_user(
            username="close_requester_plain",
            email="close_requester_plain@bestfinance.com.np",
            password="testpass123",
        )
        agent = get_user_model().objects.create_user(
            username="close_agent_plain",
            email="close_agent_plain@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        ticket = self._create_resolved_ticket_with_history(
            requester,
            agent,
            subject="Close email plain test",
            description="Test ticket",
        )

        self.client.force_login(agent)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={
                "status": "closed",
                "priority": "low",
                "status_note": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "closed")
        self.assertEqual(ticket.closed_note, "")

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_support_user_cannot_reopen_resolved_ticket(self):
        requester = get_user_model().objects.create_user(
            username="resolved_reopen_requester",
            email="resolved_reopen_requester@bestfinance.com.np",
            password="testpass123",
        )
        agent = get_user_model().objects.create_user(
            username="resolved_reopen_agent",
            email="resolved_reopen_agent@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        ticket = self._create_resolved_ticket_with_history(
            requester,
            agent,
            subject="Resolved reopen permission test",
            description="Resolved ticket should not be reopened by support users.",
        )

        self.client.force_login(agent)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={
                "status": "in_progress",
                "priority": "low",
                "assigned_to": agent.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "resolved")

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_support_user_cannot_reopen_closed_ticket(self):
        requester = get_user_model().objects.create_user(
            username="closed_reopen_requester",
            email="closed_reopen_requester@bestfinance.com.np",
            password="testpass123",
        )
        support_user = get_user_model().objects.create_user(
            username="closed_reopen_support",
            email="closed_reopen_support@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Closed reopen permission test",
            description="Closed ticket should stay closed for non-admin users.",
            priority="low",
            status="closed",
            closed_at=timezone.now(),
        )

        self.client.force_login(support_user)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={
                "status": "in_progress",
                "priority": "low",
                "assigned_to": support_user.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "closed")

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_admin_cannot_reopen_closed_ticket_from_ticket_panel(self):
        requester = get_user_model().objects.create_user(
            username="closed_reopen_requester_admin",
            email="closed_reopen_requester_admin@bestfinance.com.np",
            password="testpass123",
        )
        support_user = get_user_model().objects.create_user(
            username="closed_reopen_assignee_admin",
            password="testpass123",
            is_itsupport=True,
        )
        admin_user = get_user_model().objects.create_user(
            username="closed_reopen_admin",
            email="closed_reopen_admin@bestfinance.com.np",
            password="testpass123",
            is_staff=True,
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Closed reopen by admin",
            description="Admin panel only should be able to reopen a closed ticket.",
            priority="low",
            status="closed",
            closed_at=timezone.now(),
        )

        self.client.force_login(admin_user)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={
                "status": "in_progress",
                "priority": "low",
                "assigned_to": support_user.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "closed")
        self.assertIsNone(ticket.assigned_to_id)
        self.assertIsNotNone(ticket.closed_at)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_assigned_owner_cannot_reopen_closed_ticket(self):
        requester = get_user_model().objects.create_user(
            username="closed_reopen_requester_owner",
            email="closed_reopen_requester_owner@bestfinance.com.np",
            password="testpass123",
        )
        assigned_owner = get_user_model().objects.create_user(
            username="closed_reopen_owner",
            email="closed_reopen_owner@bestfinance.com.np",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Closed reopen by assigned owner",
            description="Assigned owner should not be able to reopen a closed ticket.",
            priority="low",
            status="closed",
            closed_at=timezone.now(),
        )
        Ticket.objects.filter(pk=ticket.pk).update(assigned_to_id=assigned_owner.id)
        ticket.refresh_from_db()

        self.client.force_login(assigned_owner)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={"status": "in_progress"},
        )

        self.assertEqual(response.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "closed")
        self.assertEqual(ticket.assigned_to_id, assigned_owner.id)


class TicketSelfAssignmentBlockTests(TestCase):
    def test_ticket_creator_cannot_assign_ticket_to_self(self):
        creator = get_user_model().objects.create_user(
            username="creator_support",
            email="creator_support@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        other_support = get_user_model().objects.create_user(
            username="other_support2",
            email="other_support2@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        ticket = Ticket.objects.create(
            created_by=creator,
            subject="Self-assign block",
            description="Test ticket",
            priority="low",
            status="new",
            assigned_to=other_support,
        )

        self.client.force_login(creator)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={"status": "new", "priority": "low", "assigned_to": creator.id},
        )

        self.assertEqual(response.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.assigned_to_id, other_support.id)


class AutoCloseResolvedTicketsCommandTests(TestCase):
    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_command_auto_closes_resolved_tickets_after_cutoff(self):
        requester = get_user_model().objects.create_user(
            username="auto_close_requester",
            email="auto_close_requester@bestfinance.com.np",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Auto-close test",
            description="Test ticket",
            priority="low",
            status="resolved",
            resolved_at=timezone.now() - timedelta(days=11),
        )

        call_command("auto_close_resolved_tickets", "--days", "10")

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "closed")
        self.assertIn("Auto-closed after 10 days", ticket.closed_note)
        self.assertIsNone(ticket.closed_by_id)
        self.assertIsNotNone(ticket.closed_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(ticket.ticket_id, mail.outbox[0].subject)
        self.assertIn(requester.email, mail.outbox[0].to)


class UnresolvedPriorityReminderCommandTests(TestCase):
    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_command_sends_one_summary_for_overdue_high_and_critical_tickets_per_assignee(self):
        requester = get_user_model().objects.create_user(
            username="reminder_requester",
            email="reminder_requester@bestfinance.com.np",
            password="testpass123",
        )
        assignee = get_user_model().objects.create_user(
            username="reminder_assignee",
            email="reminder_assignee@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        critical_ticket = Ticket.objects.create(
            created_by=requester,
            assigned_to=assignee,
            subject="Critical unresolved reminder",
            description="Critical reminder should send.",
            priority="critical",
            status="in_progress",
        )
        high_ticket = Ticket.objects.create(
            created_by=requester,
            assigned_to=assignee,
            subject="High unresolved reminder",
            description="High reminder should send.",
            priority="high",
            status="waiting_on_user",
        )
        low_ticket = Ticket.objects.create(
            created_by=requester,
            assigned_to=assignee,
            subject="Low unresolved reminder",
            description="Low reminder should not send.",
            priority="low",
            status="in_progress",
        )
        resolved_ticket = Ticket.objects.create(
            created_by=requester,
            assigned_to=assignee,
            subject="Resolved reminder should not send",
            description="Resolved ticket should be ignored.",
            priority="critical",
            status="resolved",
            resolved_at=timezone.now() - timedelta(days=4),
        )
        Ticket.objects.filter(pk=critical_ticket.pk).update(created_at=timezone.now() - timedelta(days=4))
        Ticket.objects.filter(pk=high_ticket.pk).update(created_at=timezone.now() - timedelta(days=5))
        Ticket.objects.filter(pk=low_ticket.pk).update(created_at=timezone.now() - timedelta(days=6))
        Ticket.objects.filter(pk=resolved_ticket.pk).update(created_at=timezone.now() - timedelta(days=6))

        call_command("send_unresolved_priority_reminders", "--days", "3", "--repeat-days", "3")

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("2 unresolved high/critical tickets", mail.outbox[0].subject)
        self.assertIn(critical_ticket.ticket_id, mail.outbox[0].body)
        self.assertIn(high_ticket.ticket_id, mail.outbox[0].body)
        self.assertNotIn(low_ticket.ticket_id, mail.outbox[0].body)
        self.assertNotIn(resolved_ticket.ticket_id, mail.outbox[0].body)
        self.assertEqual(TicketReminderSummaryLog.objects.filter(assignee=assignee).count(), 1)
        self.assertEqual(TicketReminderSummaryLog.objects.get(assignee=assignee).ticket_count, 2)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_command_repeats_summary_only_after_repeat_window(self):
        requester = get_user_model().objects.create_user(
            username="reminder_requester_repeat",
            email="reminder_requester_repeat@bestfinance.com.np",
            password="testpass123",
        )
        assignee = get_user_model().objects.create_user(
            username="reminder_assignee_repeat",
            email="reminder_assignee_repeat@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
        )
        ticket = Ticket.objects.create(
            created_by=requester,
            assigned_to=assignee,
            subject="Repeat reminder cadence",
            description="Reminder should repeat every three days only.",
            priority="critical",
            status="in_progress",
        )
        Ticket.objects.filter(pk=ticket.pk).update(
            created_at=timezone.now() - timedelta(days=7),
        )
        TicketReminderSummaryLog.objects.create(
            assignee=assignee,
            sent_at=timezone.now() - timedelta(days=2),
            ticket_count=1,
        )

        call_command("send_unresolved_priority_reminders", "--days", "3", "--repeat-days", "3")

        self.assertEqual(len(mail.outbox), 0)

        TicketReminderSummaryLog.objects.filter(assignee=assignee).update(
            sent_at=timezone.now() - timedelta(days=3, minutes=1)
        )
        call_command("send_unresolved_priority_reminders", "--days", "3", "--repeat-days", "3")

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(TicketReminderSummaryLog.objects.filter(assignee=assignee).count(), 2)


class SupportPortalFiltersTests(TestCase):
    def setUp(self):
        self.viewer = get_user_model().objects.create_user(
            username="support_viewer",
            password="testpass123",
            is_itsupport=True,
            department="IT",
        )
        self.creator_one = get_user_model().objects.create_user(
            username="creator_one",
            password="testpass123",
        )
        self.creator_two = get_user_model().objects.create_user(
            username="creator_two",
            password="testpass123",
        )
        self.agent_one = get_user_model().objects.create_user(
            username="agent_one",
            password="testpass123",
            is_itsupport=True,
        )
        self.agent_two = get_user_model().objects.create_user(
            username="agent_two",
            password="testpass123",
            is_itsupport=True,
        )
        self.ticket_one = Ticket.objects.create(
            created_by=self.creator_one,
            assigned_to=self.agent_one,
            subject="Printer issue",
            description="Printer is offline.",
            priority="medium",
            status="new",
        )
        self.ticket_two = Ticket.objects.create(
            created_by=self.creator_two,
            assigned_to=self.agent_one,
            subject="VPN issue",
            description="VPN cannot connect.",
            priority="high",
            status="in_progress",
        )
        self.ticket_three = Ticket.objects.create(
            created_by=self.creator_one,
            assigned_to=self.agent_two,
            subject="Laptop setup",
            description="Need a laptop prepared.",
            priority="low",
            status="resolved",
        )
        self.ticket_four = Ticket.objects.create(
            created_by=self.creator_two,
            assigned_to=None,
            subject="Queue needs owner",
            description="Unassigned ticket should be filterable.",
            priority="medium",
            status="new",
        )
        self.ticket_five = Ticket.objects.create(
            created_by=self.creator_one,
            assigned_to=self.agent_two,
            subject="Ticket acknowledged",
            description="Acknowledged ticket should count with new.",
            priority="medium",
            status="acknowledged",
        )
        self.ticket_six = Ticket.objects.create(
            created_by=self.creator_two,
            assigned_to=self.agent_two,
            subject="Waiting on user",
            description="Waiting ticket should count with in progress.",
            priority="medium",
            status="waiting_on_user",
        )
        self.ticket_seven = Ticket.objects.create(
            created_by=self.creator_one,
            assigned_to=self.agent_one,
            subject="Cancelled duplicate",
            description="Cancelled ticket should count with closed.",
            priority="low",
            status="cancelled_duplicate",
        )
        base_time = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
        Ticket.objects.filter(pk=self.ticket_one.pk).update(created_at=base_time - timedelta(days=5))
        Ticket.objects.filter(pk=self.ticket_two.pk).update(created_at=base_time - timedelta(days=2))
        Ticket.objects.filter(pk=self.ticket_three.pk).update(created_at=base_time - timedelta(days=1))
        Ticket.objects.filter(pk=self.ticket_four.pk).update(created_at=base_time - timedelta(days=4))
        Ticket.objects.filter(pk=self.ticket_five.pk).update(created_at=base_time - timedelta(days=6))
        Ticket.objects.filter(pk=self.ticket_six.pk).update(created_at=base_time - timedelta(days=3))
        Ticket.objects.filter(pk=self.ticket_seven.pk).update(created_at=base_time - timedelta(days=7))
        self.ticket_one.refresh_from_db()
        self.ticket_two.refresh_from_db()
        self.ticket_three.refresh_from_db()
        self.ticket_four.refresh_from_db()
        self.ticket_five.refresh_from_db()
        self.ticket_six.refresh_from_db()
        self.ticket_seven.refresh_from_db()
        self.client.force_login(self.viewer)

    def test_support_dashboard_filters_by_same_day_range(self):
        response = self.client.get(
            reverse("support_dashboard"),
            {
                "date_from": self.ticket_two.created_at.date().isoformat(),
                "date_to": self.ticket_two.created_at.date().isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_tickets"], 1)
        self.assertEqual([ticket.id for ticket in response.context["recent_tickets"]], [self.ticket_two.id])
        self.assertContains(response, self.ticket_two.subject)
        self.assertNotContains(response, self.ticket_one.subject)
        self.assertNotContains(response, self.ticket_three.subject)

    def test_support_queue_filters_by_ticket_creator_assignee_and_date_range(self):
        response = self.client.get(
            reverse("support_queue"),
            {
                "q": self.ticket_six.ticket_id,
                "created_by_username": self.creator_two.username,
                "assigned_to_username": self.agent_two.username,
                "date_from": self.ticket_six.created_at.date().isoformat(),
                "date_to": self.ticket_six.created_at.date().isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([ticket.id for ticket in response.context["tickets"]], [self.ticket_six.id])
        self.assertContains(response, self.ticket_six.subject)
        self.assertNotContains(response, self.ticket_one.subject)
        self.assertNotContains(response, self.ticket_two.subject)

    def test_agent_workload_link_opens_tickets_assigned_to_that_agent(self):
        dashboard_response = self.client.get(reverse("support_dashboard"))

        self.assertEqual(dashboard_response.status_code, 200)
        agent_rows = dashboard_response.context["agent_workload"]
        agent_one_row = next(item for item in agent_rows if item["assigned_to__id"] == self.agent_one.id)
        self.assertIn(f"assigned_to_username={self.agent_one.username}", agent_one_row["queue_url"])

        queue_response = self.client.get(agent_one_row["queue_url"])

        self.assertEqual(queue_response.status_code, 200)
        queue_ids = [ticket.id for ticket in queue_response.context["tickets"]]
        self.assertIn(self.ticket_one.id, queue_ids)
        self.assertIn(self.ticket_two.id, queue_ids)
        self.assertNotIn(self.ticket_three.id, queue_ids)

    def test_support_dashboard_shows_all_support_agents_and_excludes_closed_resolved(self):
        all_agents = set(
            item["assigned_to__username"] for item in self.client.get(reverse("support_dashboard")).context["agent_workload"]
        )
        self.assertIn(self.agent_one.username, all_agents)
        self.assertIn(self.agent_two.username, all_agents)

        dashboard = self.client.get(reverse("support_dashboard"))
        item_agent_two = next(item for item in dashboard.context["agent_workload"] if item["assigned_to__username"] == self.agent_two.username)
        self.assertEqual(item_agent_two["total"], 2)  # acknowledged + in_progress, resolved excluded

        item_agent_one = next(item for item in dashboard.context["agent_workload"] if item["assigned_to__username"] == self.agent_one.username)
        self.assertEqual(item_agent_one["total"], 2)  # new + in_progress

    def test_support_dashboard_includes_non_itsupport_assigned_agent(self):
        regular_agent = get_user_model().objects.create_user(
            username="regular_agent",
            password="testpass123",
        )
        Ticket.objects.create(
            created_by=self.creator_one,
            assigned_to=regular_agent,
            subject="External agent ticket",
            description="A non-IT-support agent should appear in workload.",
            priority="low",
            status="new",
        )

        all_agents = set(
            item["assigned_to__username"] for item in self.client.get(reverse("support_dashboard")).context["agent_workload"]
        )
        self.assertIn(regular_agent.username, all_agents)

    def test_ticket_update_can_reassign_non_support_user_and_workload_updates(self):
        regular_agent_one = get_user_model().objects.create_user(
            username="regular_agent_one",
            password="testpass123",
        )
        regular_agent_two = get_user_model().objects.create_user(
            username="regular_agent_two",
            password="testpass123",
        )
        ticket = Ticket.objects.create(
            created_by=self.creator_one,
            assigned_to=regular_agent_one,
            subject="Reassign regular agent ticket",
            description="Workload should move when the assignee changes.",
            priority="low",
            status="new",
        )

        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={
                "status": "new",
                "priority": "low",
                "assigned_to": regular_agent_two.id,
            },
        )

        self.assertEqual(response.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.assigned_to_id, regular_agent_two.id)
        self.assertEqual(TicketAssignmentLog.objects.filter(ticket=ticket).count(), 2)

        dashboard = self.client.get(reverse("support_dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        workload_by_username = {
            item["assigned_to__username"]: item["total"] for item in dashboard.context["agent_workload"]
        }
        self.assertEqual(workload_by_username.get(regular_agent_two.username), 1)
        self.assertNotIn(regular_agent_one.username, workload_by_username)

    def test_ticket_update_moves_workload_between_support_agents(self):
        response = self.client.post(
            reverse("ticket_update", args=[self.ticket_two.id]),
            data={
                "status": self.ticket_two.status,
                "priority": self.ticket_two.priority,
                "assigned_to": self.agent_two.id,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.ticket_two.refresh_from_db()
        self.assertEqual(self.ticket_two.assigned_to_id, self.agent_two.id)

        dashboard = self.client.get(reverse("support_dashboard"))

        self.assertEqual(dashboard.status_code, 200)
        workload_by_username = {
            item["assigned_to__username"]: item["total"] for item in dashboard.context["agent_workload"]
        }
        self.assertEqual(workload_by_username.get(self.agent_one.username), 1)
        self.assertEqual(workload_by_username.get(self.agent_two.username), 3)

    def test_support_dashboard_counts_resolved_ticket_for_resolver(self):
        resolved_ticket = Ticket.objects.create(
            created_by=self.creator_one,
            assigned_to=self.agent_two,
            subject="Resolved workload ticket",
            description="Resolved tickets should count for the resolver.",
            priority="medium",
            status="in_progress",
        )
        resolved_ticket.status = "resolved"
        resolved_ticket.resolved_by = self.agent_two
        resolved_ticket.save()

        dashboard = self.client.get(reverse("support_dashboard"))

        self.assertEqual(dashboard.status_code, 200)
        item_agent_two = next(
            item for item in dashboard.context["agent_workload"] if item["assigned_to__username"] == self.agent_two.username
        )
        self.assertEqual(item_agent_two["total"], 3)  # acknowledged + waiting_on_user + resolved

    def test_support_dashboard_recent_tickets_include_chat_and_call_actions(self):
        response = self.client.get(reverse("support_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{reverse("ticket_detail", args=[self.ticket_two.id])}#ticket-chat"')
        self.assertContains(response, f'href="{reverse("ticket_detail", args=[self.ticket_two.id])}?autocall=1&callmode=start#ticket-chat"')

    def test_support_dashboard_hides_chat_and_call_actions_for_private_ticket_without_access(self):
        private_ticket = Ticket.objects.create(
            created_by=self.creator_one,
            assigned_to=self.agent_one,
            subject="Private dashboard chat ticket",
            description="Only the requester and assignee should see dashboard chat actions.",
            priority="medium",
            status="in_progress",
            chat_is_private=True,
        )

        response = self.client.get(reverse("support_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, private_ticket.subject)
        self.assertNotContains(response, f'href="{reverse("ticket_detail", args=[private_ticket.id])}#ticket-chat"')
        self.assertNotContains(response, f'href="{reverse("ticket_detail", args=[private_ticket.id])}?autocall=1&callmode=start#ticket-chat"')

    def test_agent_workload_link_includes_resolved_ticket_for_resolver(self):
        resolved_ticket = Ticket.objects.create(
            created_by=self.creator_one,
            assigned_to=self.agent_two,
            subject="Resolved workload queue ticket",
            description="Agent workload link should include resolved tickets owned by the resolver.",
            priority="medium",
            status="in_progress",
        )
        resolved_ticket.status = "resolved"
        resolved_ticket.resolved_by = self.agent_two
        resolved_ticket.save()

        dashboard_response = self.client.get(reverse("support_dashboard"))

        self.assertEqual(dashboard_response.status_code, 200)
        agent_two_row = next(
            item for item in dashboard_response.context["agent_workload"] if item["assigned_to__id"] == self.agent_two.id
        )

        queue_response = self.client.get(agent_two_row["queue_url"])

        self.assertEqual(queue_response.status_code, 200)
        queue_ids = [ticket.id for ticket in queue_response.context["tickets"]]
        self.assertIn(resolved_ticket.id, queue_ids)

    def test_support_dashboard_grouped_status_counts_match_total(self):
        response = self.client.get(reverse("support_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["new_assigned_tickets"], 2)
        self.assertEqual(response.context["new_unassigned_tickets"], 1)
        self.assertEqual(response.context["in_progress_tickets"], 2)
        self.assertEqual(response.context["resolved_tickets"], 1)
        self.assertEqual(response.context["closed_tickets"], 1)
        self.assertEqual(
            response.context["total_tickets"],
            response.context["new_assigned_tickets"]
            + response.context["new_unassigned_tickets"]
            + response.context["in_progress_tickets"]
            + response.context["resolved_tickets"]
            + response.context["closed_tickets"],
        )
        self.assertIn("status_group=new", response.context["new_assigned_tickets_url"])
        self.assertIn("assignment_scope=assigned", response.context["new_assigned_tickets_url"])
        self.assertIn("status_group=new", response.context["new_unassigned_tickets_url"])
        self.assertIn("assignment_scope=unassigned", response.context["new_unassigned_tickets_url"])
        self.assertIn("status_group=in_progress", response.context["in_progress_tickets_url"])
        self.assertIn("status_group=closed", response.context["closed_tickets_url"])

    def test_new_unassigned_card_links_to_filtered_queue(self):
        dashboard_response = self.client.get(reverse("support_dashboard"))

        self.assertEqual(dashboard_response.status_code, 200)
        self.assertEqual(
            dashboard_response.context["new_unassigned_tickets_url"],
            f'{reverse("support_queue")}?status_group=new&assignment_scope=unassigned',
        )
        self.assertContains(
            dashboard_response,
            f'href="{reverse("support_queue")}?status_group=new&amp;assignment_scope=unassigned"',
        )

    def test_support_queue_filters_new_unassigned_tickets(self):
        response = self.client.get(
            reverse("support_queue"),
            {"status_group": "new", "assignment_scope": "unassigned"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_status_group"], "new")
        self.assertEqual(response.context["selected_assignment_scope"], "unassigned")
        self.assertEqual([ticket.id for ticket in response.context["tickets"]], [self.ticket_four.id])
        self.assertContains(response, self.ticket_four.subject)
        self.assertNotContains(response, self.ticket_one.subject)
        self.assertNotContains(response, self.ticket_five.subject)
        self.assertNotContains(response, self.ticket_two.subject)
        self.assertNotContains(response, self.ticket_three.subject)

    def test_support_queue_filters_status_group(self):
        response = self.client.get(
            reverse("support_queue"),
            {"status_group": "new"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_status_group"], "new")
        queue_ids = [ticket.id for ticket in response.context["tickets"]]
        self.assertIn(self.ticket_one.id, queue_ids)
        self.assertIn(self.ticket_four.id, queue_ids)
        self.assertIn(self.ticket_five.id, queue_ids)
        self.assertNotIn(self.ticket_two.id, queue_ids)
        self.assertNotIn(self.ticket_six.id, queue_ids)

    def test_support_queue_filters_by_priority(self):
        response = self.client.get(
            reverse("support_queue"),
            {"priority": "high"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_priority"], "high")
        queue_ids = [ticket.id for ticket in response.context["tickets"]]
        self.assertEqual(queue_ids, [self.ticket_two.id])

    def test_support_queue_filters_by_department(self):
        it_ticket = Ticket.objects.create(
            created_by=self.creator_one,
            assigned_to=self.agent_one,
            subject="IT department queue ticket",
            description="Should be visible when filtering by IT.",
            priority="medium",
            status="new",
            department="IT",
            branch="Head Office",
        )
        hr_ticket = Ticket.objects.create(
            created_by=self.creator_two,
            assigned_to=self.agent_two,
            subject="HR department queue ticket",
            description="Should stay out when filtering by IT.",
            priority="medium",
            status="new",
            department="HR",
        )

        response = self.client.get(
            reverse("support_queue"),
            {"department": "IT"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_department"], "IT")
        self.assertEqual(response.context["selected_branch"], "Head Office")
        queue_ids = [ticket.id for ticket in response.context["tickets"]]
        self.assertIn(it_ticket.id, queue_ids)
        self.assertNotIn(hr_ticket.id, queue_ids)
        self.assertContains(response, "All Departments")
        self.assertContains(response, "IT")
        self.assertContains(response, "Head Office")

    def test_support_department_filter_uses_canonical_department_options(self):
        Department.objects.update_or_create(name="IT", defaults={})
        Department.objects.update_or_create(name="CENTRAL OPERATION DEPARTMENT", defaults={})
        legacy_department_name = "LEGACY OPS TEST ONLY"
        Ticket.objects.create(
            created_by=self.creator_one,
            assigned_to=self.agent_one,
            subject="Legacy operation ticket",
            description="Uses an old ticket-only department value.",
            priority="medium",
            status="new",
            department=legacy_department_name,
        )
        Ticket.objects.create(
            created_by=self.creator_two,
            assigned_to=self.agent_two,
            subject="Mixed case IT ticket",
            description="Uses a ticket-only case variant.",
            priority="medium",
            status="new",
            department="it",
            branch="Head Office",
        )

        response = self.client.get(reverse("support_queue"))

        self.assertEqual(response.status_code, 200)
        options = response.context["support_department_options"]
        self.assertIn("IT", options)
        self.assertIn("CENTRAL OPERATION DEPARTMENT", options)
        self.assertNotIn(legacy_department_name, options)
        self.assertNotIn("it", options)

    def test_support_branch_filter_uses_canonical_branch_options(self):
        legacy_branch_name = "LEGACY BRANCH TEST ONLY"
        canonical_branch_name = "CANONICAL BRANCH TEST"
        Branch.objects.get_or_create(name=canonical_branch_name, defaults={"branch_id": "TB001"})
        Ticket.objects.create(
            created_by=self.creator_one,
            assigned_to=self.agent_one,
            subject="Legacy branch ticket",
            description="Uses an old ticket-only branch value.",
            priority="medium",
            status="new",
            branch=legacy_branch_name,
        )

        response = self.client.get(reverse("support_queue"))

        self.assertEqual(response.status_code, 200)
        options = response.context["support_branch_options"]
        self.assertIn("Head Office", options)
        self.assertIn(canonical_branch_name, options)
        self.assertNotIn(legacy_branch_name, options)

    def test_support_queue_filters_by_branch(self):
        kathmandu_ticket = Ticket.objects.create(
            created_by=self.creator_one,
            assigned_to=self.agent_one,
            subject="Kathmandu branch queue ticket",
            description="Should be visible when filtering by Kathmandu.",
            priority="medium",
            status="new",
            branch="Kathmandu",
        )
        pokhara_ticket = Ticket.objects.create(
            created_by=self.creator_two,
            assigned_to=self.agent_two,
            subject="Pokhara branch queue ticket",
            description="Should stay out when filtering by Kathmandu.",
            priority="medium",
            status="new",
            branch="Pokhara",
        )

        response = self.client.get(
            reverse("support_queue"),
            {"branch": "Kathmandu"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_branch"], "Kathmandu")
        queue_ids = [ticket.id for ticket in response.context["tickets"]]
        self.assertIn(kathmandu_ticket.id, queue_ids)
        self.assertNotIn(pokhara_ticket.id, queue_ids)
        self.assertContains(response, "All Branches")
        self.assertContains(response, "Kathmandu")
        self.assertContains(response, "Pokhara")

    def test_support_dashboard_filters_by_priority(self):
        response = self.client.get(
            reverse("support_dashboard"),
            {"priority": "medium"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_priority"], "medium")
        recent_ids = [ticket.id for ticket in response.context["recent_tickets"]]
        self.assertEqual(
            recent_ids,
            [self.ticket_six.id, self.ticket_four.id, self.ticket_one.id, self.ticket_five.id],
        )

    def test_support_dashboard_filters_by_department(self):
        it_ticket = Ticket.objects.create(
            created_by=self.creator_one,
            assigned_to=self.agent_one,
            subject="IT department dashboard ticket",
            description="Dashboard should show this for IT filter.",
            priority="high",
            status="in_progress",
            department="IT",
            branch="Head Office",
        )
        hr_ticket = Ticket.objects.create(
            created_by=self.creator_two,
            assigned_to=self.agent_two,
            subject="HR department dashboard ticket",
            description="Dashboard should hide this for IT filter.",
            priority="high",
            status="in_progress",
            department="HR",
        )

        response = self.client.get(
            reverse("support_dashboard"),
            {"department": "IT"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_department"], "IT")
        self.assertEqual(response.context["selected_branch"], "Head Office")
        self.assertEqual(response.context["total_tickets"], 1)
        recent_ids = [ticket.id for ticket in response.context["recent_tickets"]]
        self.assertEqual(recent_ids, [it_ticket.id])
        self.assertNotContains(response, hr_ticket.subject)

    def test_support_dashboard_filters_by_branch(self):
        kathmandu_ticket = Ticket.objects.create(
            created_by=self.creator_one,
            assigned_to=self.agent_one,
            subject="Kathmandu branch dashboard ticket",
            description="Dashboard should show this for Kathmandu filter.",
            priority="high",
            status="in_progress",
            branch="Kathmandu",
        )
        pokhara_ticket = Ticket.objects.create(
            created_by=self.creator_two,
            assigned_to=self.agent_two,
            subject="Pokhara branch dashboard ticket",
            description="Dashboard should hide this for Kathmandu filter.",
            priority="high",
            status="in_progress",
            branch="Pokhara",
        )

        response = self.client.get(
            reverse("support_dashboard"),
            {"branch": "Kathmandu"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_branch"], "Kathmandu")
        self.assertEqual(response.context["total_tickets"], 1)
        recent_ids = [ticket.id for ticket in response.context["recent_tickets"]]
        self.assertEqual(recent_ids, [kathmandu_ticket.id])
        self.assertNotContains(response, pokhara_ticket.subject)

    def test_support_department_tickets_split_unassigned_and_assigned_department_work(self):
        unassigned_department_ticket = Ticket.objects.create(
            created_by=self.creator_one,
            assigned_to=None,
            subject="IT queue ticket",
            description="Department-routed ticket waiting for an owner.",
            priority="high",
            status="new",
            department="IT",
        )
        assigned_department_ticket = Ticket.objects.create(
            created_by=self.creator_two,
            assigned_to=self.agent_one,
            subject="Assigned IT department ticket",
            description="Already assigned, so it should stay out of the department queue page.",
            priority="medium",
            status="new",
            department="IT",
        )
        solved_department_ticket = Ticket.objects.create(
            created_by=self.creator_one,
            assigned_to=self.agent_two,
            subject="Resolved IT department ticket",
            description="Solved tickets should not remain in the department queue page.",
            priority="medium",
            status="in_progress",
            department="IT",
        )
        solved_department_ticket.status = "resolved"
        solved_department_ticket.save()
        other_department_ticket = Ticket.objects.create(
            created_by=self.creator_one,
            assigned_to=None,
            subject="HR queue ticket",
            description="Different department should stay hidden for the IT support user.",
            priority="medium",
            status="new",
            department="HR",
        )

        response = self.client.get(reverse("support_department_tickets"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_title"], "Department Tickets")
        self.assertEqual(
            [ticket.id for ticket in response.context["unassigned_tickets"]],
            [unassigned_department_ticket.id],
        )
        self.assertEqual(
            [ticket.id for ticket in response.context["assigned_tickets"]],
            [assigned_department_ticket.id],
        )
        self.assertEqual(
            [ticket.id for ticket in response.context["solved_tickets"]],
            [solved_department_ticket.id],
        )
        self.assertContains(response, "Unassigned Department Queue")
        self.assertContains(response, "Assigned Department Tickets")
        self.assertContains(response, "Resolved / Closed Department Tickets")
        self.assertContains(response, unassigned_department_ticket.subject)
        self.assertContains(response, assigned_department_ticket.subject)
        self.assertContains(response, solved_department_ticket.subject)
        self.assertNotContains(response, other_department_ticket.subject)
        self.assertNotContains(response, self.ticket_four.subject)
        self.assertNotContains(response, "Assigned Username")


class SupportUsersViewTests(TestCase):
    def setUp(self):
        self.viewer = get_user_model().objects.create_user(
            username="support_users_viewer",
            password="testpass123",
            is_itsupport=True,
            branch="Head Office",
            department="IT",
        )
        self.active_user = get_user_model().objects.create_user(
            username="active_portal_user",
            email="active_portal_user@bestfinance.com.np",
            password="testpass123",
            branch="Pokhara",
            department="Operations",
        )
        self.stale_user = get_user_model().objects.create_user(
            username="stale_portal_user",
            email="stale_portal_user@bestfinance.com.np",
            password="testpass123",
            branch="Biratnagar",
            department="Finance",
        )
        self.client.force_login(self.viewer)

        active_client_one = Client()
        active_client_one.force_login(self.active_user)
        active_session_one = active_client_one.session
        active_session_one["active_seen_ts"] = int((timezone.now() - timedelta(minutes=2)).timestamp())
        active_session_one.save()

        active_client_two = Client()
        active_client_two.force_login(self.active_user)
        active_session_two = active_client_two.session
        active_session_two["active_seen_ts"] = int((timezone.now() - timedelta(minutes=1)).timestamp())
        active_session_two.save()

        stale_client = Client()
        stale_client.force_login(self.stale_user)
        stale_session = stale_client.session
        stale_session["active_seen_ts"] = int((timezone.now() - timedelta(minutes=45)).timestamp())
        stale_session.save()

    def test_support_users_page_shows_total_users_and_deduplicated_online_users(self):
        response = self.client.get(reverse("support_users"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_users"], 3)
        self.assertEqual(response.context["currently_logged_in_total"], 2)

        active_usernames = [row["user"].username for row in response.context["currently_logged_in_users"]]
        self.assertEqual(active_usernames.count(self.active_user.username), 1)
        self.assertIn(self.viewer.username, active_usernames)
        self.assertIn(self.active_user.username, active_usernames)
        self.assertNotIn(self.stale_user.username, active_usernames)

        self.assertContains(response, self.active_user.branch)
        self.assertContains(response, self.active_user.department)
        self.assertContains(response, "Currently Logged In")

    def test_support_sidebar_links_are_visible_only_to_support_accounts(self):
        support_response = self.client.get(reverse("support_dashboard"))

        self.assertEqual(support_response.status_code, 200)
        self.assertContains(support_response, reverse("support_users"))
        self.assertContains(support_response, reverse("support_department_tickets"))

        regular_user = get_user_model().objects.create_user(
            username="regular_sidebar_user",
            password="testpass123",
        )
        regular_client = Client()
        regular_client.force_login(regular_user)

        regular_response = regular_client.get(reverse("ticket_list"))

        self.assertEqual(regular_response.status_code, 200)
        self.assertNotContains(regular_response, reverse("support_users"))
        self.assertNotContains(regular_response, reverse("support_department_tickets"))

    def test_regular_users_cannot_open_support_users_page(self):
        regular_user = get_user_model().objects.create_user(
            username="support_users_regular",
            password="testpass123",
        )
        regular_client = Client()
        regular_client.force_login(regular_user)

        response = regular_client.get(reverse("support_users"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_regular_users_cannot_open_support_department_tickets_page(self):
        regular_user = get_user_model().objects.create_user(
            username="support_department_regular",
            password="testpass123",
        )
        regular_client = Client()
        regular_client.force_login(regular_user)

        response = regular_client.get(reverse("support_department_tickets"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])


class IncidentResponseTemplateViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="incident_template_user",
            password="testpass123",
        )
        self.client.force_login(self.user)

    def test_incident_response_template_page_renders(self):
        response = self.client.get(reverse("incident_response_template"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Incident Response Template")
        self.assertContains(response, "Best Finance Company Ltd.")
        self.assertContains(response, 'name="incident_title"', html=False)
        self.assertContains(response, 'name="incident_registered_person"', html=False)
        self.assertContains(response, 'name="incident_notified_person"', html=False)
        self.assertContains(response, "Download PDF")
        self.assertContains(response, "Print View")

    @patch("tickets.views._incident_response_template_docx_to_pdf_payload")
    def test_incident_response_template_downloads_pdf(self, mock_pdf_payload):
        mock_pdf_payload.return_value = b"%PDF-1.4 template pdf"
        response = self.client.post(
            reverse("incident_response_template"),
            data={
                "incident_title": "CBS Outage",
                "incident_id": "INC-2026-001",
                "reported_by": "Service Desk",
                "summary_what_happened": "CBS was unavailable.",
                "incident_registered_person": "Alice",
                "incident_notified_person": "Bob",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("CBS_Outage.pdf", response["Content-Disposition"])


class IncidentReportViewTests(TestCase):
    def setUp(self):
        self.requester = get_user_model().objects.create_user(
            username="incident_requester",
            email="incident_requester@bestfinance.com.np",
            password="testpass123",
            branch="Kathmandu",
            department="Operations",
            position="Operations Officer",
        )
        self.support_user = get_user_model().objects.create_user(
            username="incident_support",
            email="incident_support@bestfinance.com.np",
            password="testpass123",
            is_itsupport=True,
            branch="Head Office",
            department="IT",
        )
        self.ram = get_user_model().objects.create_user(
            username="ram",
            email="ram@bestfinance.com.np",
            first_name="Ram",
            last_name="Thapa",
            password="testpass123",
            branch="Head Office",
            department="Operations",
        )
        self.other_user = get_user_model().objects.create_user(
            username="other_signer",
            email="other_signer@bestfinance.com.np",
            password="testpass123",
            branch="Head Office",
            department="Operations",
        )
        self.ticket = Ticket.objects.create(
            created_by=self.requester,
            subject="CBS outage at branch",
            request_type="incident",
            department="Operations",
            branch="Kathmandu",
            description="CBS is unavailable for front-desk staff.",
        )

    def _signature_upload(self, name="signature.png"):
        return SimpleUploadedFile(
            name,
            base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII="),
            content_type="image/png",
        )

    def _evidence_upload(self, name="evidence.txt", payload=b"incident evidence", content_type="text/plain"):
        return SimpleUploadedFile(name, payload, content_type=content_type)

    def _notified_signoff_formset_payload(self, rows, initial_forms=0):
        payload = {
            "notified_signoffs-TOTAL_FORMS": str(len(rows)),
            "notified_signoffs-INITIAL_FORMS": str(initial_forms),
            "notified_signoffs-MIN_NUM_FORMS": "0",
            "notified_signoffs-MAX_NUM_FORMS": "1000",
        }
        for index, row in enumerate(rows):
            payload[f"notified_signoffs-{index}-level"] = str(row.get("level", ""))
            payload[f"notified_signoffs-{index}-user"] = str(row.get("user", ""))
            if row.get("id"):
                payload[f"notified_signoffs-{index}-id"] = str(row["id"])
            if row.get("DELETE"):
                payload[f"notified_signoffs-{index}-DELETE"] = "on"
        return payload

    def _create_notified_signoff(self, incident_report, user=None, level=1, signed=False):
        signoff = IncidentReportSignoff.objects.create(
            incident_report=incident_report,
            role=IncidentReportSignoff.ROLE_NOTIFIED,
            user=user or self.ram,
            level=level,
        )
        if signed:
            signoff.snapshot_signature = self._signature_upload(f"signoff-level-{level}.png")
            signoff.signed_display_name = signoff.user.get_full_name() or signoff.user.username
            signoff.signed_at = timezone.now()
            signoff.save()
        return signoff

    def _incident_report_payload(self, notified_signoffs=None, notified_initial_forms=0, **overrides):
        payload = {
            "incident_title": "CBS Outage at Kathmandu",
            "incident_id": "INC-2026-001",
            "detected_at": "Apr 11, 2026 09:30",
            "reported_by": "Service Desk",
            "incident_commander": "IT Service Manager",
            "severity_choice": "critical",
            "current_status": "Resolved",
            "service_affected": "cbs",
            "downtime_duration_minutes": "45",
            "branch_impacted": "Kathmandu",
            "regulatory_impact": "on",
            "summary_what_happened": "CBS was unavailable for branch users.",
            "summary_detected": "Detected from branch call.",
            "summary_affected": "CBS tellers and branch operators.",
            "impact_branch_department": "Kathmandu / Operations",
            "impact_users": "80",
            "impact_operational": "Front-desk transactions were delayed.",
            "impact_regulatory": "NRB update prepared.",
            "timeline_detection": "09:30",
            "timeline_initial_triage": "09:40",
            "timeline_containment_started": "09:45",
            "timeline_recovery_started": "10:10",
            "timeline_service_restored": "10:30",
            "timeline_incident_closed": "11:00",
            "containment_actions": "Traffic was redirected.",
            "temporary_workarounds": "Manual transaction log used.",
            "escalations_raised": "CBS vendor was notified.",
            "eradication_root_cause": "Database connection exhaustion.",
            "eradication_fix_applied": "Connection pool was reset.",
            "eradication_validation_steps": "Branch confirmed transaction flow.",
            "eradication_systems_restored": "CBS restored to normal operation.",
            "communication_stakeholders": "Branch manager and IT leadership.",
            "communication_update_frequency": "Every 30 minutes",
            "communication_latest_update": "Final service-restored notice shared.",
            "evidence_ticket_case": self.ticket.ticket_id,
            "evidence_logs": "Application logs and DB session logs.",
            "evidence_attachments": "CBS screenshot and monitoring capture.",
            "evidence_vendors": "CBS vendor hotline ref 123.",
            "review_root_cause_summary": "Connection saturation triggered outage.",
            "review_lessons_learned": "Need better pool monitoring.",
            "review_preventive_actions": "Add DB saturation alert.",
            "review_action_owners": "DBA - alert setup - Apr 20",
            "registered_user": str(self.support_user.id),
        }
        if notified_signoffs is None:
            notified_signoffs = [{"level": 1, "user": self.ram.id}]
        payload.update(self._notified_signoff_formset_payload(notified_signoffs, initial_forms=notified_initial_forms))
        payload.update(overrides)
        return payload

    def test_support_user_can_open_new_incident_report_page(self):
        self.client.force_login(self.support_user)

        response = self.client.get(reverse("ticket_incident_report", args=[self.ticket.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Incident Response Template")
        self.assertContains(response, "Notified Sign-Off Chain")
        self.assertContains(response, "Evidence File Uploads")
        self.assertContains(response, 'name="notified_signoffs-TOTAL_FORMS"', html=False)
        self.assertContains(response, "Submit & Send")

    def test_support_user_can_create_incident_report(self):
        self.client.force_login(self.support_user)

        response = self.client.post(
            reverse("ticket_incident_report", args=[self.ticket.id]),
            data=self._incident_report_payload(),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("ticket_incident_report", args=[self.ticket.id]))

        incident_report = IncidentReport.objects.get(ticket=self.ticket)
        notified_signoff = incident_report.signoffs.get(level=1)
        self.assertEqual(incident_report.incident_title, "CBS Outage at Kathmandu")
        self.assertEqual(incident_report.incident_id, "INC-2026-001")
        self.assertEqual(incident_report.service_affected, "cbs")
        self.assertEqual(incident_report.downtime_duration_minutes, 45)
        self.assertEqual(incident_report.branch_impacted, "Kathmandu")
        self.assertTrue(incident_report.regulatory_impact)
        self.assertEqual(incident_report.review_root_cause_summary, "Connection saturation triggered outage.")
        self.assertEqual(incident_report.registered_user_id, self.support_user.id)
        self.assertEqual(notified_signoff.user_id, self.ram.id)
        self.assertEqual(notified_signoff.level, 1)
        self.assertEqual(incident_report.display_registered_person, "incident_support")
        self.assertEqual(incident_report.display_notified_person, "Acknowledged By: Ram Thapa")
        self.assertEqual(incident_report.created_by_id, self.support_user.id)
        self.assertEqual(incident_report.updated_by_id, self.support_user.id)
        self.assertFalse(bool(incident_report.registered_signature))
        self.assertFalse(bool(incident_report.notified_signature))

    def test_support_user_can_create_incident_report_with_blank_extra_signoff_rows(self):
        self.client.force_login(self.support_user)
        notified_signoffs = [
            {"level": 1, "user": self.ram.id},
            {"level": "", "user": ""},
            {"level": "", "user": ""},
            {"level": "", "user": ""},
            {"level": "", "user": ""},
            {"level": "", "user": ""},
        ]

        response = self.client.post(
            reverse("ticket_incident_report", args=[self.ticket.id]),
            data=self._incident_report_payload(
                notified_signoffs=notified_signoffs,
                incident_signoff_level_count="1",
            ),
        )

        self.assertEqual(response.status_code, 302)
        incident_report = IncidentReport.objects.get(ticket=self.ticket)
        self.assertEqual(incident_report.signoffs.count(), 1)
        self.assertEqual(incident_report.signoffs.get(level=1).user_id, self.ram.id)

    def test_support_user_can_create_multiple_notified_signoffs(self):
        self.client.force_login(self.support_user)

        response = self.client.post(
            reverse("ticket_incident_report", args=[self.ticket.id]),
            data=self._incident_report_payload(
                notified_signoffs=[
                    {"level": 1, "user": self.ram.id},
                    {"level": 2, "user": self.other_user.id},
                ],
            ),
        )

        self.assertEqual(response.status_code, 302)
        incident_report = IncidentReport.objects.get(ticket=self.ticket)
        signoffs = list(incident_report.signoffs.order_by("level").values_list("level", "user_id"))
        self.assertEqual(signoffs, [(1, self.ram.id), (2, self.other_user.id)])
        self.assertEqual(incident_report.display_notified_person, "L1: Ram Thapa, L2: other_signer")

    def test_ticket_detail_shows_incident_report_summary(self):
        IncidentReport.objects.create(
            ticket=self.ticket,
            incident_title="Network Outage",
            incident_id="INC-NET-1",
            current_status="Monitoring",
            service_affected="network",
            downtime_duration_minutes=90,
            branch_impacted="Kathmandu",
            regulatory_impact=False,
            registered_user=self.support_user,
            created_by=self.support_user,
            updated_by=self.support_user,
        )
        existing_signoff = self._create_notified_signoff(IncidentReport.objects.get(ticket=self.ticket), user=self.ram, level=1)
        self.client.force_login(self.support_user)

        response = self.client.get(reverse("ticket_detail", args=[self.ticket.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Incident Report")
        self.assertContains(response, "Network Outage")
        self.assertContains(response, "INC-NET-1")
        self.assertContains(response, "Network")
        self.assertContains(response, "1h 30m")
        self.assertContains(response, reverse("ticket_incident_report", args=[self.ticket.id]))
        self.assertContains(response, reverse("ticket_incident_report_download", args=[self.ticket.id]))
        self.assertNotContains(response, "Download PNG")
        self.assertNotContains(response, "Download JPG")
        self.assertContains(response, "Download Word")

    def test_requester_can_edit_own_incident_report(self):
        IncidentReport.objects.create(
            ticket=self.ticket,
            service_affected="atm",
            downtime_duration_minutes=30,
            branch_impacted="Kathmandu",
            regulatory_impact=True,
            incident_title="ATM Downtime",
            current_status="Resolved",
            registered_user=self.support_user,
        )
        existing_signoff = self._create_notified_signoff(
            IncidentReport.objects.get(ticket=self.ticket),
            user=self.ram,
            level=1,
        )
        self.client.force_login(self.requester)

        get_response = self.client.get(reverse("ticket_incident_report", args=[self.ticket.id]))
        self.assertEqual(get_response.status_code, 200)
        self.assertContains(get_response, "ATM")
        self.assertContains(get_response, "ATM Downtime")

    def test_incident_report_page_no_longer_shows_image_download_links(self):
        IncidentReport.objects.create(
            ticket=self.ticket,
            service_affected="atm",
            downtime_duration_minutes=30,
            branch_impacted="Kathmandu",
            regulatory_impact=True,
            incident_title="ATM Downtime",
            current_status="Resolved",
            registered_user=self.support_user,
        )
        self.client.force_login(self.support_user)

        response = self.client.get(reverse("ticket_incident_report", args=[self.ticket.id]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Download PNG")
        self.assertNotContains(response, "Download JPG")
        self.assertContains(get_response, "Save")
        self.assertContains(get_response, "Submit & Send")

        post_response = self.client.post(
            reverse("ticket_incident_report", args=[self.ticket.id]),
            data=self._incident_report_payload(
                incident_title="Changed",
                service_affected="cbs",
                downtime_duration_minutes="120",
                branch_impacted="Pokhara",
                notified_signoffs=[{"id": existing_signoff.id, "level": 1, "user": self.ram.id}],
                notified_initial_forms=1,
            ),
        )

        self.assertEqual(post_response.status_code, 302)
        self.assertEqual(post_response.url, reverse("ticket_incident_report", args=[self.ticket.id]))

        incident_report = IncidentReport.objects.get(ticket=self.ticket)
        self.assertEqual(incident_report.service_affected, "cbs")
        self.assertEqual(incident_report.branch_impacted, "Pokhara")
        self.assertEqual(incident_report.incident_title, "Changed")
        self.assertEqual(incident_report.updated_by_id, self.requester.id)

    def test_requester_sees_edit_incident_report_from_ticket_detail(self):
        IncidentReport.objects.create(
            ticket=self.ticket,
            service_affected="network",
            incident_title="Network outage",
        )
        self.client.force_login(self.requester)

        response = self.client.get(reverse("ticket_detail", args=[self.ticket.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Edit Incident Report")
        self.assertContains(response, reverse("ticket_incident_report", args=[self.ticket.id]))

    def test_support_user_can_upload_incident_report_evidence_file(self):
        self.client.force_login(self.support_user)

        response = self.client.post(
            reverse("ticket_incident_report", args=[self.ticket.id]),
            data={
                **self._incident_report_payload(),
                "evidence_files": self._evidence_upload("cbs-screenshot.txt"),
            },
        )

        self.assertEqual(response.status_code, 302)
        incident_report = IncidentReport.objects.get(ticket=self.ticket)
        attachment = IncidentReportAttachment.objects.get(incident_report=incident_report)
        self.assertEqual(attachment.filename, "cbs-screenshot.txt")
        self.assertEqual(attachment.uploaded_by_id, self.support_user.id)
        self.assertEqual(attachment.content_type, "text/plain")
        self.assertGreaterEqual(attachment.size, 1)

    def test_incident_report_attachment_can_be_downloaded(self):
        incident_report = IncidentReport.objects.create(
            ticket=self.ticket,
            incident_title="CBS Outage at Kathmandu",
            incident_id="INC-2026-001",
            service_affected="cbs",
            downtime_duration_minutes=45,
            branch_impacted="Kathmandu",
            regulatory_impact=True,
            registered_user=self.support_user,
        )
        attachment = IncidentReportAttachment.objects.create(
            incident_report=incident_report,
            file=self._evidence_upload("incident-log.txt", payload=b"log lines"),
            original_name="incident-log.txt",
            content_type="text/plain",
            size=9,
            uploaded_by=self.support_user,
        )
        self.client.force_login(self.requester)

        response = self.client.get(
            reverse("ticket_incident_report_attachment_download", args=[self.ticket.id, attachment.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain")
        self.assertIn('incident-log.txt', response["Content-Disposition"])

    @patch("tickets.views._incident_response_template_image_payloads")
    def test_saved_incident_report_can_be_downloaded_as_png(self, mock_image_payloads):
        mock_image_payloads.return_value = [b"incident-png"]
        IncidentReport.objects.create(
            ticket=self.ticket,
            incident_title="CBS Outage at Kathmandu",
            incident_id="INC-2026-001",
            service_affected="cbs",
            downtime_duration_minutes=45,
            branch_impacted="Kathmandu",
            regulatory_impact=True,
            registered_user=self.support_user,
        )
        self._create_notified_signoff(IncidentReport.objects.get(ticket=self.ticket), user=self.ram, level=1)
        self.client.force_login(self.requester)

        response = self.client.get(reverse("ticket_incident_report_download", args=[self.ticket.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertIn("CBS_Outage_at_Kathmandu-page-1.png", response["Content-Disposition"])

    @patch("tickets.views._build_ticket_incident_report_docx_response")
    def test_saved_incident_report_can_be_downloaded_as_docx(self, mock_docx_response):
        mock_docx_response.return_value = HttpResponse(b"docx", content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        IncidentReport.objects.create(
            ticket=self.ticket,
            incident_title="CBS Outage at Kathmandu",
            incident_id="INC-2026-001",
            service_affected="cbs",
            downtime_duration_minutes=45,
            branch_impacted="Kathmandu",
            regulatory_impact=True,
            registered_user=self.support_user,
        )
        self._create_notified_signoff(IncidentReport.objects.get(ticket=self.ticket), user=self.ram, level=1)
        self.client.force_login(self.requester)

        response = self.client.get(
            reverse("ticket_incident_report_download", args=[self.ticket.id]) + "?format=docx"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    def test_saved_incident_report_docx_uses_consistent_font_weight(self):
        incident_report = IncidentReport.objects.create(
            ticket=self.ticket,
            incident_title="CBS Outage at Kathmandu",
            incident_id="INC-2026-001",
            service_affected="cbs",
            downtime_duration_minutes=45,
            branch_impacted="Kathmandu",
            regulatory_impact=True,
            reporting_employee_name="Deepen GC",
            registered_user=self.support_user,
        )
        self._create_notified_signoff(incident_report, user=self.ram, level=1)

        payload = _build_ticket_incident_report_docx(self.ticket, incident_report)

        self.assertIsInstance(payload, bytes)
        with zipfile.ZipFile(BytesIO(payload), "r") as docx_file:
            document_xml = docx_file.read("word/document.xml").decode("utf-8")
            package_xml = "\n".join(
                docx_file.read(name).decode("utf-8", errors="ignore")
                for name in docx_file.namelist()
                if name.startswith("word/") and name.endswith(".xml") and name != "word/document.xml"
            )
        self.assertIn("Deepen GC", document_xml)
        self.assertIn("Operations Officer", document_xml)
        self.assertIn("Recovery Actions:", document_xml)
        self.assertIn("System(s) Impacted:", document_xml)
        self.assertIn("Network Impacted:", document_xml)
        self.assertIn("Unit or Department Requiring Notification:", document_xml)
        self.assertRegex(document_xml, r"<w:b\s*/>.*?<w:t>Recovery Actions:</w:t>")
        self.assertRegex(document_xml, r"<w:b\s*/>.*?<w:t>Recommendations for Improvement:</w:t>")
        self.assertNotIn("☑", document_xml)
        self.assertNotIn("☐", document_xml)
        self.assertNotIn("[Title]", document_xml + package_xml)
        self.assertNotIn("<w:t>Critical</w:t>", document_xml)
        self.assertNotIn("<w:t>High</w:t>", document_xml)
        self.assertNotIn("<w:t>Medium</w:t>", document_xml)
        self.assertNotIn("<w:t>Low</w:t>", document_xml)
        self.assertNotIn("hjhkhjkjkjkkjhkhkhkkjkjhkjhkjhkjhkj", document_xml)
        self.assertNotIn("May 07, 2026 22:47", document_xml)
        self.assertIn("INCIDENT REPORT INFORMATION", document_xml)
        self.assertIn("<w:b", document_xml)
        self.assertIn('w:ascii="Times New Roman"', document_xml)

    @patch("tickets.views._build_ticket_incident_report_pdf_response")
    def test_saved_incident_report_can_be_downloaded_as_pdf(self, mock_pdf_response):
        mock_pdf_response.return_value = HttpResponse(b"pdf", content_type="application/pdf")
        incident_report = IncidentReport.objects.create(
            ticket=self.ticket,
            incident_title="CBS Outage at Kathmandu",
            incident_id="INC-2026-001",
            service_affected="cbs",
            downtime_duration_minutes=45,
            branch_impacted="Kathmandu",
            regulatory_impact=True,
            registered_user=self.support_user,
        )
        self._create_notified_signoff(incident_report, user=self.ram, level=1)
        self.client.force_login(self.requester)

        response = self.client.get(
            reverse("ticket_incident_report_download", args=[self.ticket.id]) + "?format=pdf"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        mock_pdf_response.assert_called_once()

    def test_support_user_can_reopen_and_update_saved_incident_report(self):
        incident_report = IncidentReport.objects.create(
            ticket=self.ticket,
            incident_title="CBS Outage at Kathmandu",
            incident_id="INC-2026-001",
            service_affected="cbs",
            downtime_duration_minutes=45,
            branch_impacted="Kathmandu",
            regulatory_impact=True,
            current_status="Monitoring",
            registered_user=self.support_user,
            created_by=self.support_user,
            updated_by=self.support_user,
        )
        existing_signoff = self._create_notified_signoff(incident_report, user=self.ram, level=1)
        self.client.force_login(self.support_user)

        response = self.client.post(
            reverse("ticket_incident_report", args=[self.ticket.id]),
            data=self._incident_report_payload(
                notified_signoffs=[{"id": existing_signoff.id, "level": 2, "user": self.support_user.id}],
                notified_initial_forms=1,
                current_status="Closed",
                review_action_owners="Infra team - complete failover test - Apr 30",
                registered_user=str(self.ram.id),
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("ticket_incident_report", args=[self.ticket.id]))

        incident_report.refresh_from_db()
        updated_signoff = incident_report.signoffs.get(level=2)
        self.assertEqual(incident_report.current_status, "Closed")
        self.assertEqual(incident_report.review_action_owners, "Infra team - complete failover test - Apr 30")
        self.assertEqual(incident_report.registered_user_id, self.ram.id)
        self.assertEqual(updated_signoff.user_id, self.support_user.id)
        self.assertEqual(incident_report.display_registered_person, "Ram Thapa")
        self.assertEqual(incident_report.updated_by_id, self.support_user.id)

    def test_assigned_signer_can_view_incident_report_and_sign_own_section(self):
        self.ram.signature_image = self._signature_upload("ram-signature.png")
        self.ram.save()
        incident_report = IncidentReport.objects.create(
            ticket=self.ticket,
            incident_title="CBS Outage at Kathmandu",
            incident_id="INC-2026-001",
            service_affected="cbs",
            downtime_duration_minutes=45,
            branch_impacted="Kathmandu",
            regulatory_impact=True,
            registered_user=self.support_user,
            created_by=self.support_user,
            updated_by=self.support_user,
        )
        signoff = self._create_notified_signoff(incident_report, user=self.ram, level=1)
        self.client.force_login(self.ram)

        get_response = self.client.get(reverse("ticket_incident_report", args=[self.ticket.id]))

        self.assertEqual(get_response.status_code, 200)
        self.assertContains(get_response, "Sign Level 1")

        post_response = self.client.post(
            reverse("ticket_incident_report_signoff_sign", args=[self.ticket.id, signoff.id]),
        )

        self.assertEqual(post_response.status_code, 302)
        self.assertEqual(post_response.url, reverse("ticket_incident_report", args=[self.ticket.id]))

        signoff.refresh_from_db()
        incident_report.refresh_from_db()
        self.assertTrue(bool(signoff.snapshot_signature))
        self.assertIsNotNone(signoff.signed_at)
        self.assertEqual(incident_report.display_notified_person, "L1: Ram Thapa")

    def test_changing_assigned_signer_clears_existing_signature_snapshot(self):
        self.ram.signature_image = self._signature_upload("ram-signature.png")
        self.ram.save()
        incident_report = IncidentReport.objects.create(
            ticket=self.ticket,
            incident_title="CBS Outage at Kathmandu",
            incident_id="INC-2026-001",
            service_affected="cbs",
            downtime_duration_minutes=45,
            branch_impacted="Kathmandu",
            regulatory_impact=True,
            registered_user=self.support_user,
            created_by=self.support_user,
            updated_by=self.support_user,
        )
        signoff = self._create_notified_signoff(incident_report, user=self.ram, level=1)
        self.client.force_login(self.ram)
        self.client.post(reverse("ticket_incident_report_signoff_sign", args=[self.ticket.id, signoff.id]))

        self.client.force_login(self.support_user)
        response = self.client.post(
            reverse("ticket_incident_report", args=[self.ticket.id]),
            data=self._incident_report_payload(
                notified_signoffs=[{"id": signoff.id, "level": 1, "user": self.other_user.id}],
                notified_initial_forms=1,
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("ticket_incident_report", args=[self.ticket.id]))

        incident_report.refresh_from_db()
        signoff.refresh_from_db()
        self.assertEqual(signoff.user_id, self.other_user.id)
        self.assertFalse(bool(signoff.snapshot_signature))
        self.assertIsNone(signoff.signed_at)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_saving_draft_does_not_email_notified_users(self):
        self.client.force_login(self.support_user)

        response = self.client.post(
            reverse("ticket_incident_report", args=[self.ticket.id]),
            data=self._incident_report_payload(),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    @patch("tickets.views._incident_response_template_image_payloads")
    def test_submitting_incident_report_emails_notified_users_and_cc_recipients(self, mock_image_payloads):
        from tickets.views import _send_incident_report_submission_email

        mock_image_payloads.return_value = [b"incident-png"]
        self.client.force_login(self.support_user)
        request = self.client.get(reverse("ticket_detail", args=[self.ticket.id])).wsgi_request
        incident_report = IncidentReport.objects.create(
            ticket=self.ticket,
            incident_title="CBS Outage at Kathmandu",
            incident_id="INC-2026-001",
            service_affected="cbs",
            downtime_duration_minutes=45,
            branch_impacted="Kathmandu",
            regulatory_impact=True,
            registered_user=self.support_user,
            created_by=self.support_user,
            updated_by=self.support_user,
        )
        self._create_notified_signoff(incident_report, user=self.ram, level=1, signed=True)
        mail.outbox.clear()

        warnings = _send_incident_report_submission_email(
            request,
            self.ticket,
            incident_report,
            cc_users=[self.other_user],
        )

        self.assertEqual(warnings, [])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.ram.email])
        self.assertEqual(mail.outbox[0].cc, [self.other_user.email])
        self.assertIn("Incident Response Submitted: INC-2026-001", mail.outbox[0].subject)
        self.assertIn(reverse("ticket_incident_report", args=[self.ticket.id]), mail.outbox[0].body)
        self.assertEqual(len(mail.outbox[0].attachments), 1)
        self.assertTrue(mail.outbox[0].attachments[0][0].endswith(".png"))
        self.assertEqual(mail.outbox[0].attachments[0][2], "image/png")

    def test_custom_service_can_be_typed_and_saved(self):
        self.client.force_login(self.support_user)

        response = self.client.post(
            reverse("ticket_incident_report", args=[self.ticket.id]),
            data=self._incident_report_payload(
                service_affected="",
                service_affected_other="Card Switch",
            ),
        )

        self.assertEqual(response.status_code, 302)
        incident_report = IncidentReport.objects.get(ticket=self.ticket)
        self.assertEqual(incident_report.service_affected, "Card Switch")

    def test_submitting_incident_report_requires_notified_user(self):
        self.client.force_login(self.support_user)

        response = self.client.post(
            reverse("ticket_incident_report", args=[self.ticket.id]),
            data=self._incident_report_payload(
                notified_signoffs=[],
                action="submit",
                incident_signoff_level_count="1",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add users for selected review/approval sign-off order 1.")

    def test_submitted_incident_report_cannot_be_edited(self):
        incident_report = IncidentReport.objects.create(
            ticket=self.ticket,
            incident_title="CBS Outage at Kathmandu",
            incident_id="INC-2026-001",
            service_affected="cbs",
            downtime_duration_minutes=45,
            branch_impacted="Kathmandu",
            regulatory_impact=True,
            registered_user=self.support_user,
            created_by=self.support_user,
            updated_by=self.support_user,
            submitted_at=timezone.now(),
            submitted_by=self.support_user,
        )
        self.client.force_login(self.support_user)

        response = self.client.post(
            reverse("ticket_incident_report", args=[self.ticket.id]),
            data=self._incident_report_payload(incident_title="Changed after submit"),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        incident_report.refresh_from_db()
        self.ticket.refresh_from_db()
        self.assertEqual(incident_report.incident_title, "CBS Outage at Kathmandu")
        self.assertContains(response, "no longer editable")

    def test_unassigned_user_cannot_sign_other_users_section(self):
        self.support_user.signature_image = self._signature_upload("support-signature.png")
        self.support_user.save()
        incident_report = IncidentReport.objects.create(
            ticket=self.ticket,
            incident_title="CBS Outage at Kathmandu",
            incident_id="INC-2026-001",
            service_affected="cbs",
            downtime_duration_minutes=45,
            branch_impacted="Kathmandu",
            regulatory_impact=True,
            registered_user=self.support_user,
            created_by=self.support_user,
            updated_by=self.support_user,
        )
        signoff = self._create_notified_signoff(incident_report, user=self.ram, level=1)
        self.client.force_login(self.other_user)

        response = self.client.post(
            reverse("ticket_incident_report_signoff_sign", args=[self.ticket.id, signoff.id]),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("ticket_list"))

    def test_sign_requires_profile_signature_uploaded_by_admin(self):
        incident_report = IncidentReport.objects.create(
            ticket=self.ticket,
            incident_title="CBS Outage at Kathmandu",
            incident_id="INC-2026-001",
            service_affected="cbs",
            downtime_duration_minutes=45,
            branch_impacted="Kathmandu",
            regulatory_impact=True,
            registered_user=self.support_user,
            created_by=self.support_user,
            updated_by=self.support_user,
        )
        signoff = self._create_notified_signoff(incident_report, user=self.ram, level=1)
        self.client.force_login(self.ram)

        response = self.client.post(
            reverse("ticket_incident_report_signoff_sign", args=[self.ticket.id, signoff.id]),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("ticket_incident_report", args=[self.ticket.id]))

        signoff.refresh_from_db()
        self.assertFalse(bool(signoff.snapshot_signature))
        self.assertIsNone(signoff.signed_at)

    def test_non_incident_ticket_cannot_open_incident_report(self):
        service_ticket = Ticket.objects.create(
            created_by=self.requester,
            subject="Service request",
            request_type="service",
            department="Operations",
            branch="Kathmandu",
            description="Need access to a shared folder.",
        )
        self.client.force_login(self.support_user)

        response = self.client.get(reverse("ticket_incident_report", args=[service_ticket.id]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("ticket_detail", args=[service_ticket.id]))
        self.assertFalse(IncidentReport.objects.filter(ticket=service_ticket).exists())


class AgentWorkloadViewTests(TestCase):
    def setUp(self):
        AuthenticationSettings.objects.update_or_create(
            pk=1,
            defaults={
                "ad_login_enabled": True,
                "local_login_enabled": False,
                "local_account_self_service_enabled": False,
                "agent_workload_view_enabled": True,
            },
        )
        self.viewer = get_user_model().objects.create_user(
            username="agent_workload_viewer",
            password="testpass123",
        )
        self.creator = get_user_model().objects.create_user(
            username="agent_workload_creator",
            password="testpass123",
        )
        self.agent_one = get_user_model().objects.create_user(
            username="agent_workload_one",
            password="testpass123",
            is_itsupport=True,
        )
        self.agent_two = get_user_model().objects.create_user(
            username="agent_workload_two",
            password="testpass123",
        )
        self.ticket_one = Ticket.objects.create(
            created_by=self.creator,
            assigned_to=self.agent_one,
            subject="Printer issue",
            description="Printer is offline.",
            priority="medium",
            status="new",
        )
        self.ticket_two = Ticket.objects.create(
            created_by=self.creator,
            assigned_to=self.agent_two,
            subject="VPN issue",
            description="VPN cannot connect.",
            priority="high",
            status="in_progress",
        )
        self.ticket_three = Ticket.objects.create(
            created_by=self.creator,
            assigned_to=self.agent_two,
            subject="Resolved issue",
            description="Resolved ticket should stay counted for the resolver.",
            priority="low",
            status="resolved",
            resolved_by=self.agent_two,
        )
        self.client.force_login(self.viewer)

    def test_regular_user_can_open_agent_workload_view(self):
        response = self.client.get(reverse("agent_workload_view"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Agent Workload View")
        self.assertContains(response, self.agent_one.username)
        self.assertContains(response, self.agent_two.username)

        workload_by_username = {
            item["assigned_to__username"]: item["total"] for item in response.context["agent_workload"]
        }
        self.assertEqual(workload_by_username.get(self.agent_one.username), 1)
        self.assertEqual(workload_by_username.get(self.agent_two.username), 2)

    def test_regular_user_sees_agent_workload_menu_link(self):
        response = self.client.get(reverse("ticket_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("agent_workload_view"))
        self.assertContains(response, "Support Workload View")

    def test_agent_workload_view_is_read_only(self):
        response = self.client.get(reverse("agent_workload_view"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse("ticket_update", args=[self.ticket_one.id]))
        self.assertNotContains(response, reverse("ticket_update", args=[self.ticket_two.id]))
        self.assertNotContains(response, "Manage")
        self.assertFalse(any("queue_url" in item for item in response.context["agent_workload"]))

    def test_agent_workload_view_menu_can_be_hidden_from_admin_settings(self):
        AuthenticationSettings.objects.update_or_create(
            pk=1,
            defaults={
                "ad_login_enabled": True,
                "local_login_enabled": False,
                "local_account_self_service_enabled": False,
                "agent_workload_view_enabled": False,
            },
        )

        ticket_list_response = self.client.get(reverse("ticket_list"))
        workload_response = self.client.get(reverse("agent_workload_view"))

        self.assertEqual(ticket_list_response.status_code, 200)
        self.assertNotContains(ticket_list_response, reverse("agent_workload_view"))
        self.assertEqual(workload_response.status_code, 302)
        self.assertEqual(workload_response.url, reverse("ticket_list"))

    def test_support_users_do_not_get_regular_agent_workload_menu_link(self):
        support_user = get_user_model().objects.create_user(
            username="agent_workload_support",
            password="testpass123",
            is_itsupport=True,
        )
        support_client = Client()
        support_client.force_login(support_user)

        response = support_client.get(reverse("support_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse("agent_workload_view"))
        self.assertNotContains(response, "Support Workload View")


class TicketListDateFilterTests(TestCase):
    def setUp(self):
        self.requester = get_user_model().objects.create_user(
            username="ticket_list_user",
            password="testpass123",
        )
        self.other_user = get_user_model().objects.create_user(
            username="ticket_list_other_user",
            password="testpass123",
        )
        self.agent = get_user_model().objects.create_user(
            username="ticket_list_agent",
            password="testpass123",
            is_itsupport=True,
        )
        self.ticket_one = Ticket.objects.create(
            created_by=self.requester,
            assigned_to=self.agent,
            subject="Email access",
            description="Cannot access mailbox.",
            priority="medium",
            status="new",
        )
        self.ticket_two = Ticket.objects.create(
            created_by=self.requester,
            assigned_to=self.agent,
            subject="Payroll portal",
            description="Portal error on login.",
            priority="high",
            status="in_progress",
        )
        self.ticket_three = Ticket.objects.create(
            created_by=self.requester,
            assigned_to=self.agent,
            subject="Printer toner",
            description="Need toner replacement.",
            priority="low",
            status="resolved",
        )
        self.assigned_ticket = Ticket.objects.create(
            created_by=self.other_user,
            assigned_to=self.requester,
            subject="Assigned to requester",
            description="Should stay out of dashboard-scope results.",
            priority="medium",
            status="new",
        )
        base_time = timezone.now().replace(hour=10, minute=0, second=0, microsecond=0)
        Ticket.objects.filter(pk=self.ticket_one.pk).update(created_at=base_time - timedelta(days=6))
        Ticket.objects.filter(pk=self.ticket_two.pk).update(created_at=base_time - timedelta(days=3))
        Ticket.objects.filter(pk=self.ticket_three.pk).update(created_at=base_time - timedelta(days=1))
        self.ticket_one.refresh_from_db()
        self.ticket_two.refresh_from_db()
        self.ticket_three.refresh_from_db()
        self.client.force_login(self.requester)

    def test_ticket_list_filters_by_same_start_and_end_date(self):
        response = self.client.get(
            reverse("ticket_list"),
            {
                "date_from": self.ticket_two.created_at.date().isoformat(),
                "date_to": self.ticket_two.created_at.date().isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([ticket.id for ticket in response.context["tickets"]], [self.ticket_two.id])
        self.assertContains(response, self.ticket_two.subject)
        self.assertNotContains(response, self.ticket_one.subject)
        self.assertNotContains(response, self.ticket_three.subject)

    def test_ticket_list_filters_between_dates(self):
        response = self.client.get(
            reverse("ticket_list"),
            {
                "date_from": self.ticket_two.created_at.date().isoformat(),
                "date_to": self.ticket_three.created_at.date().isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        ticket_ids = [ticket.id for ticket in response.context["tickets"]]
        self.assertIn(self.ticket_two.id, ticket_ids)
        self.assertIn(self.ticket_three.id, ticket_ids)
        self.assertNotIn(self.ticket_one.id, ticket_ids)

    def test_ticket_list_filters_by_status(self):
        response = self.client.get(
            reverse("ticket_list"),
            {"status": "in_progress"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_status"], "in_progress")
        self.assertEqual([ticket.id for ticket in response.context["tickets"]], [self.ticket_two.id])
        self.assertContains(response, self.ticket_two.subject)
        self.assertNotContains(response, self.ticket_one.subject)
        self.assertNotContains(response, self.ticket_three.subject)

    def test_ticket_list_scope_created_by_me_excludes_assigned_tickets(self):
        response = self.client.get(
            reverse("ticket_list"),
            {"scope": "created_by_me", "status": "new"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_scope"], "created_by_me")
        self.assertEqual([ticket.id for ticket in response.context["tickets"]], [self.ticket_one.id])
        self.assertContains(response, self.ticket_one.subject)
        self.assertNotContains(response, self.assigned_ticket.subject)

    def test_ticket_list_scope_assigned_to_me_excludes_created_tickets(self):
        response = self.client.get(
            reverse("ticket_list"),
            {"scope": "assigned_to_me", "status": "new"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_scope"], "assigned_to_me")
        self.assertEqual([ticket.id for ticket in response.context["tickets"]], [self.assigned_ticket.id])
        self.assertContains(response, self.assigned_ticket.subject)
        self.assertNotContains(response, self.ticket_one.subject)

    def test_ticket_list_shows_scope_filter_for_normal_users(self):
        response = self.client.get(reverse("ticket_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="scope"')
        self.assertContains(response, "Created by me")
        self.assertContains(response, "Assigned to me")

    def test_support_user_ticket_list_shows_only_assigned_tickets(self):
        created_by_agent_ticket = Ticket.objects.create(
            created_by=self.agent,
            subject="Support-owned follow-up",
            description="Created directly by the support user.",
            priority="medium",
            status="new",
        )
        resolved_for_agent = Ticket.objects.create(
            created_by=self.requester,
            assigned_to=self.agent,
            subject="Resolved by assigned agent",
            description="Should remain visible through last assignee history.",
            priority="medium",
            status="in_progress",
        )
        resolved_for_agent.status = "resolved"
        resolved_for_agent.save()

        other_agent = get_user_model().objects.create_user(
            username="ticket_list_other_agent",
            password="testpass123",
            is_itsupport=True,
        )
        resolved_for_other_agent = Ticket.objects.create(
            created_by=self.requester,
            assigned_to=other_agent,
            subject="Resolved by another agent",
            description="Should stay out of this agent's My Tickets.",
            priority="medium",
            status="in_progress",
        )
        resolved_for_other_agent.status = "resolved"
        resolved_for_other_agent.save()

        unrelated_ticket = Ticket.objects.create(
            created_by=self.other_user,
            subject="Unassigned queue item",
            description="Should stay in the IT Portal only.",
            priority="medium",
            status="new",
        )

        self.client.force_login(self.agent)
        response = self.client.get(reverse("ticket_list"))

        self.assertEqual(response.status_code, 200)
        ticket_ids = [ticket.id for ticket in response.context["tickets"]]
        self.assertIn(self.ticket_one.id, ticket_ids)
        self.assertIn(self.ticket_two.id, ticket_ids)
        self.assertIn(resolved_for_agent.id, ticket_ids)
        self.assertNotIn(created_by_agent_ticket.id, ticket_ids)
        self.assertNotIn(self.ticket_three.id, ticket_ids)
        self.assertNotIn(resolved_for_other_agent.id, ticket_ids)
        self.assertNotIn(self.assigned_ticket.id, ticket_ids)
        self.assertNotIn(unrelated_ticket.id, ticket_ids)
        self.assertNotContains(response, 'name="scope"')
