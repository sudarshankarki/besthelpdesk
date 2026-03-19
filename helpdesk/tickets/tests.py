import base64
import hashlib
import hmac
import json
from datetime import timedelta
from io import StringIO
from urllib.parse import urlparse
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.test.utils import override_settings
from django.utils import timezone
from django.urls import reverse

from accounts.models import Department
from .forms import TicketForm
from .models import (
    GroupMailboxEmail,
    TechnicalDocument,
    Ticket,
    TicketAssignmentLog,
    TicketChatReadState,
    TicketMessage,
)
from .notifications import (
    build_call_notification_payload,
    build_chat_notification_payload,
    get_call_notification_target_ids,
    get_chat_notification_target_ids,
)


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
        self.alice = get_user_model().objects.create_user(username="alice", password="testpass123")
        self.bob = get_user_model().objects.create_user(username="bob", password="testpass123")
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

    def test_docs_list_filters_by_visibility(self):
        url = reverse("tech_docs")

        self.client.force_login(self.alice)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.public_doc.title)
        self.assertContains(response, self.restricted_doc.title)
        self.assertNotContains(response, self.support_doc.title)

        self.client.force_login(self.bob)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.public_doc.title)
        self.assertNotContains(response, self.restricted_doc.title)
        self.assertNotContains(response, self.support_doc.title)

        self.client.force_login(self.agent)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.public_doc.title)
        self.assertContains(response, self.restricted_doc.title)
        self.assertContains(response, self.support_doc.title)

    def test_doc_view_forbidden_for_unlisted_user(self):
        url = reverse("tech_doc_view", args=[self.restricted_doc.id])

        self.client.force_login(self.bob)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)


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
        self.creator = get_user_model().objects.create_user(
            username="creator_form",
            email="creator_form@bestfinance.com.np",
            password="testpass123",
        )
        self.assignee = get_user_model().objects.create_user(
            username="assignee_form",
            email="assignee_form@bestfinance.com.np",
            password="testpass123",
        )
        self.client.force_login(self.creator)

    def _ticket_payload(self, **overrides):
        payload = {
            "subject": "Create ticket routing",
            "request_type": "incident",
            "department": "",
            "assign_email": "",
            "notify_email": "",
            "description": "Routing test ticket",
            "impact": "single_user",
            "urgency": "medium",
        }
        payload.update(overrides)
        return payload

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
                assign_email=self.assignee.email,
                notify_email="hr@bestfinance.com.np",
            ),
        )

        self.assertEqual(response.status_code, 302)
        ticket = Ticket.objects.get(subject="HR routed ticket")
        self.assertEqual(ticket.assigned_to_id, self.assignee.id)
        self.assertEqual(ticket.notify_email, "hr@bestfinance.com.np")
        self.assertEqual(ticket.department, "HR")
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
        new_ticket_message = next(
            message for message in mail.outbox
            if message.subject.startswith("New Helpdesk Ticket:")
        )
        assignment_message = next(
            message for message in mail.outbox
            if message.subject.startswith("Ticket Assigned:")
        )
        self.assertIn("Open Ticket:", new_ticket_message.body)
        self.assertIn("Requester:", new_ticket_message.body)
        self.assertIn("has raised the following ticket for service.", new_ticket_message.body)
        self.assertIn("User Message:", new_ticket_message.body)
        self.assertIn("Open Ticket:", assignment_message.body)
        self.assertIn("Assigned By:", assignment_message.body)
        self.assertIn("has raised the following ticket for service.", assignment_message.body)
        self.assertIn("User Message:", assignment_message.body)

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
        self.assertIn("Open Ticket:", mail.outbox[0].body)
        self.assertIn("has raised the following ticket for service.", mail.outbox[0].body)

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


class DepartmentOwnershipTests(TestCase):
    def setUp(self):
        self.requester = get_user_model().objects.create_user(
            username="dept_requester",
            email="dept_requester@bestfinance.com.np",
            password="testpass123",
            department="Operations",
        )
        self.hr_user = get_user_model().objects.create_user(
            username="hr_owner",
            email="hr_owner@bestfinance.com.np",
            password="testpass123",
            department="HR",
        )
        self.finance_user = get_user_model().objects.create_user(
            username="finance_owner",
            email="finance_owner@bestfinance.com.np",
            password="testpass123",
            department="Finance",
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
        )
        self.assigned_user = get_user_model().objects.create_user(
            username="private_assignee",
            email="private_assignee@bestfinance.com.np",
            password="testpass123",
            department="HR",
        )
        self.hr_peer = get_user_model().objects.create_user(
            username="private_hr_peer",
            email="private_hr_peer@bestfinance.com.np",
            password="testpass123",
            department="HR",
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


class TicketClosedEmailTests(TestCase):
    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_closing_ticket_sends_email_to_requester_with_note(self):
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
        ticket = Ticket.objects.create(
            created_by=requester,
            subject="Close email test",
            description="Test ticket",
            priority="low",
            status="resolved",
            resolved_at=timezone.now(),
            assigned_to=agent,
        )

        self.client.force_login(agent)
        response = self.client.post(
            reverse("ticket_update", args=[ticket.id]),
            data={
                "status": "closed",
                "priority": "low",
                "assigned_to": agent.id,
                "status_note": "Closed after confirmation.",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(ticket.ticket_id, mail.outbox[0].subject)
        self.assertIn(requester.email, mail.outbox[0].to)
        self.assertIn("Closed after confirmation.", mail.outbox[0].body)

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "closed")
        self.assertEqual(ticket.closed_note, "Closed after confirmation.")
        self.assertEqual(ticket.closed_by_id, agent.id)


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


class SupportPortalFiltersTests(TestCase):
    def setUp(self):
        self.viewer = get_user_model().objects.create_user(
            username="support_viewer",
            password="testpass123",
            is_itsupport=True,
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
        base_time = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
        Ticket.objects.filter(pk=self.ticket_one.pk).update(created_at=base_time - timedelta(days=5))
        Ticket.objects.filter(pk=self.ticket_two.pk).update(created_at=base_time - timedelta(days=2))
        Ticket.objects.filter(pk=self.ticket_three.pk).update(created_at=base_time - timedelta(days=1))
        self.ticket_one.refresh_from_db()
        self.ticket_two.refresh_from_db()
        self.ticket_three.refresh_from_db()
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
                "q": self.ticket_three.ticket_id,
                "created_by_username": self.creator_one.username,
                "assigned_to_username": self.agent_two.username,
                "date_from": self.ticket_three.created_at.date().isoformat(),
                "date_to": self.ticket_three.created_at.date().isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([ticket.id for ticket in response.context["tickets"]], [self.ticket_three.id])
        self.assertContains(response, self.ticket_three.subject)
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


class TicketListDateFilterTests(TestCase):
    def setUp(self):
        self.requester = get_user_model().objects.create_user(
            username="ticket_list_user",
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
