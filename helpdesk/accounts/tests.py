import types
from unittest.mock import patch

from django.contrib.auth import authenticate
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.test.utils import override_settings
from django.urls import reverse
import re
from accounts.admin import CustomUserAdminForm
from accounts.backends import ActiveDirectoryBackend
from accounts.models import AuthenticationSettings, Branch, Department, PasswordHistory
from tickets.models import RemoteAccessApproval, Ticket


class LogoutAllDevicesTests(TestCase):
    def test_logout_from_one_device_logs_out_other_devices(self):
        user = get_user_model().objects.create_user(
            username="multi_device_user",
            password="secure-pass-123",
        )

        client_one = Client()
        client_two = Client()
        client_one.force_login(user)
        client_two.force_login(user)

        response_before = client_two.get(reverse("ticket_list"))
        self.assertEqual(response_before.status_code, 200)

        logout_response = client_one.get(reverse("logout"))
        self.assertEqual(logout_response.status_code, 302)

        response_after = client_two.get(reverse("ticket_list"))
        self.assertEqual(response_after.status_code, 302)
        self.assertIn(reverse("login"), response_after.url)


@override_settings(
    AD_AUTH_ENABLED=False,
    AD_ONLY_LOGIN=False,
    LOCAL_ACCOUNT_SELF_SERVICE_ENABLED=True,
)
class EmailVerificationSignupTests(TestCase):
    def setUp(self):
        AuthenticationSettings.objects.all().delete()

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_signup_rejects_non_company_domain(self):
        response = self.client.post(
            reverse("signup"),
            data={"email": "wrongdomain@gmail.com"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.filter(email="wrongdomain@gmail.com").exists())
        self.assertContains(response, "Registration is allowed only with @bestfinance.com.np email addresses.")
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_signup_sends_complete_link_and_does_not_create_user(self):
        response = self.client.post(
            reverse("signup"),
            data={"email": "newuser@bestfinance.com.np"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("complete-signup", mail.outbox[0].body)
        self.assertFalse(get_user_model().objects.filter(email="newuser@bestfinance.com.np").exists())

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_complete_signup_creates_active_user(self):
        self.client.post(reverse("signup"), data={"email": "verifyme@bestfinance.com.np"})
        self.assertEqual(len(mail.outbox), 1)

        body = mail.outbox[0].body
        match = re.search(r"(http://testserver[^\s]+)", body)
        self.assertIsNotNone(match)
        complete_url = match.group(1)

        response_get = self.client.get(complete_url)
        self.assertEqual(response_get.status_code, 200)

        response_post = self.client.post(
            complete_url,
            data={
                "username": "verifyme",
                "department": str(Department.objects.get(name="CSD").id),
                "branch": "001",
                "password1": "StrongPass789!",
                "password2": "StrongPass789!",
            },
        )
        self.assertEqual(response_post.status_code, 302)

        user = get_user_model().objects.get(username="verifyme")
        self.assertTrue(user.is_active)
        self.assertTrue(user.email_verified)
        self.assertEqual(user.email, "verifyme@bestfinance.com.np")


@override_settings(
    AD_AUTH_ENABLED=False,
    AD_ONLY_LOGIN=False,
    LOCAL_ACCOUNT_SELF_SERVICE_ENABLED=True,
)
class PasswordResetHistoryTests(TestCase):
    def setUp(self):
        AuthenticationSettings.objects.all().delete()
        self.passwords = [
            "ForestKey9!",
            "RiverKey8!",
            "CanyonKey7!",
            "HarborKey6!",
            "MeadowKey5!",
            "SummitKey4!",
            "GalaxyKey3!",
        ]
        self.user = get_user_model().objects.create_user(
            username="resetuser",
            email="resetuser@bestfinance.com.np",
            password=self.passwords[0],
        )
        for password in self.passwords[1:]:
            self._change_password(password)

    def _change_password(self, password):
        self.user.set_password(password)
        self.user.save()
        self.user.refresh_from_db()

    def _get_reset_submit_url(self):
        uidb64 = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)
        response = self.client.get(
            reverse(
                "password_reset_confirm",
                kwargs={"uidb64": uidb64, "token": token},
            )
        )
        self.assertEqual(response.status_code, 302)
        return response.url

    def test_password_reset_rejects_recent_password_reuse(self):
        response = self.client.post(
            self._get_reset_submit_url(),
            data={
                "new_password1": self.passwords[3],
                "new_password2": self.passwords[3],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You cannot reuse any of your last 5 passwords.")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.passwords[-1]))

    def test_password_reset_allows_password_older_than_last_five_and_trims_history(self):
        response = self.client.post(
            self._get_reset_submit_url(),
            data={
                "new_password1": self.passwords[0],
                "new_password2": self.passwords[0],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("password_reset_complete"))

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.passwords[0]))

        history_entries = list(PasswordHistory.objects.filter(user=self.user))
        self.assertEqual(len(history_entries), 5)
        self.assertTrue(any(check_password(self.passwords[-1], entry.encoded_password) for entry in history_entries))
        self.assertFalse(any(check_password(self.passwords[1], entry.encoded_password) for entry in history_entries))


class DirectoryOnlyAuthPolicyTests(TestCase):
    @override_settings(
        AD_AUTH_ENABLED=True,
        AD_ONLY_LOGIN=True,
        LOCAL_ACCOUNT_SELF_SERVICE_ENABLED=False,
    )
    def test_login_page_hides_local_signup_and_password_reset_links(self):
        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Use your computer credentials for your user account to log in to this system.")
        self.assertNotContains(response, reverse("signup"))
        self.assertNotContains(response, reverse("password_reset"))

    @override_settings(
        AD_AUTH_ENABLED=True,
        AD_ONLY_LOGIN=True,
        LOCAL_ACCOUNT_SELF_SERVICE_ENABLED=False,
    )
    def test_signup_redirects_to_login_when_local_self_service_is_disabled(self):
        response = self.client.get(reverse("signup"), follow=True)

        self.assertRedirects(response, reverse("login"))
        self.assertContains(response, "Use your office Active Directory account to sign in.")

    @override_settings(
        AD_AUTH_ENABLED=True,
        AD_ONLY_LOGIN=True,
        LOCAL_ACCOUNT_SELF_SERVICE_ENABLED=False,
    )
    def test_password_reset_redirects_to_login_when_local_self_service_is_disabled(self):
        response = self.client.get(reverse("password_reset"), follow=True)

        self.assertRedirects(response, reverse("login"))
        self.assertContains(response, "Use your office Active Directory account to sign in.")

    @override_settings(
        AD_AUTH_ENABLED=True,
        AD_ONLY_LOGIN=True,
        LOCAL_ACCOUNT_SELF_SERVICE_ENABLED=False,
        LOCAL_RECOVERY_SUPERUSERS=["recovery_admin"],
    )
    def test_non_superuser_cannot_log_in_locally_when_ad_only_mode_is_enabled(self):
        get_user_model().objects.create_user(
            username="local_user",
            email="local_user@bestfinance.com.np",
            password="testpass123",
        )

        response = self.client.post(
            reverse("login"),
            data={"username": "local_user", "password": "testpass123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid username/email or password.")
        self.assertNotIn("_auth_user_id", self.client.session)

    @override_settings(
        AD_AUTH_ENABLED=True,
        AD_ONLY_LOGIN=True,
        LOCAL_ACCOUNT_SELF_SERVICE_ENABLED=False,
        LOCAL_RECOVERY_SUPERUSERS=["recovery_admin"],
    )
    def test_recovery_superuser_can_log_in_locally_when_ad_only_mode_is_enabled(self):
        recovery_user = get_user_model().objects.create_superuser(
            username="recovery_admin",
            email="recovery_admin@bestfinance.com.np",
            password="adminpass123",
        )

        response = self.client.post(
            reverse("login"),
            data={"username": recovery_user.username, "password": "adminpass123"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("ticket_list"))


class ActiveDirectoryBackendTests(TestCase):
    @override_settings(
        AD_AUTH_ENABLED=True,
        AD_ONLY_LOGIN=True,
        LOCAL_ACCOUNT_SELF_SERVICE_ENABLED=False,
    )
    @patch("accounts.backends.ActiveDirectoryBackend._authenticate_against_directory")
    def test_successful_active_directory_login_creates_local_user_profile(self, mock_directory_auth):
        mock_directory_auth.return_value = {
            "username": "directory_user",
            "email": "directory_user@bestfinance.com.np",
            "first_name": "Directory",
            "last_name": "User",
            "department": "IT",
            "position": "Engineer",
            "branch": "Kathmandu",
            "groups": set(),
        }

        user = authenticate(username="directory_user", password="Password123!")

        self.assertIsNotNone(user)
        persisted_user = get_user_model().objects.get(username="directory_user")
        self.assertEqual(persisted_user.email, "directory_user@bestfinance.com.np")
        self.assertEqual(persisted_user.first_name, "Directory")
        self.assertEqual(persisted_user.last_name, "User")
        self.assertEqual(persisted_user.department, "IT")
        self.assertEqual(persisted_user.position, "Engineer")
        self.assertEqual(persisted_user.branch, "Kathmandu")
        self.assertTrue(persisted_user.email_verified)
        self.assertTrue(persisted_user.is_active)
        self.assertFalse(persisted_user.has_usable_password())

    @override_settings(
        AD_AUTH_ENABLED=True,
        AD_ONLY_LOGIN=True,
        LOCAL_ACCOUNT_SELF_SERVICE_ENABLED=False,
    )
    @patch("accounts.backends.ActiveDirectoryBackend._authenticate_against_directory")
    def test_existing_local_password_is_preserved_when_ad_login_is_used(self, mock_directory_auth):
        local_password = "LocalPass123!"
        existing_user = get_user_model().objects.create_user(
            username="switch_user",
            email="switch_user@bestfinance.com.np",
            password=local_password,
            first_name="Local",
        )
        AuthenticationSettings.objects.update_or_create(
            pk=1,
            defaults={
                "ad_login_enabled": True,
                "local_login_enabled": False,
                "local_account_self_service_enabled": False,
            },
        )
        mock_directory_auth.return_value = {
            "username": "switch_user",
            "email": "switch_user@bestfinance.com.np",
            "first_name": "Directory",
            "last_name": "User",
            "department": "IT",
            "position": "Engineer",
            "branch": "Kathmandu",
            "groups": set(),
        }

        authenticated_user = authenticate(username="switch_user", password="DirectoryPass123!")

        self.assertIsNotNone(authenticated_user)
        existing_user.refresh_from_db()
        self.assertTrue(existing_user.has_usable_password())
        self.assertTrue(existing_user.check_password(local_password))
        self.assertEqual(existing_user.first_name, "Directory")

        AuthenticationSettings.objects.update_or_create(
            pk=1,
            defaults={
                "ad_login_enabled": True,
                "local_login_enabled": True,
                "local_account_self_service_enabled": False,
            },
        )

        self.assertIsNotNone(authenticate(username="switch_user", password=local_password))

    @override_settings(
        AD_AUTH_ENABLED=True,
        AD_ONLY_LOGIN=True,
        LOCAL_ACCOUNT_SELF_SERVICE_ENABLED=False,
        AD_BASE_DN="DC=example,DC=local",
        AD_SERVER_HOSTS=["dc1.example.local", "dc2.example.local"],
        AD_SERVER_HOST="",
    )
    def test_directory_authentication_falls_back_to_next_controller(self):
        fake_ldap3 = types.ModuleType("ldap3")
        fake_ldap3.Connection = object()
        fake_ldap3.Server = object()
        fake_ldap3.SUBTREE = object()
        fake_ldap3.Tls = lambda **kwargs: object()

        fake_ldap3_core = types.ModuleType("ldap3.core")
        fake_ldap3_exceptions = types.ModuleType("ldap3.core.exceptions")

        class FakeLDAPException(Exception):
            pass

        fake_ldap3_exceptions.LDAPException = FakeLDAPException
        fake_ldap3_utils = types.ModuleType("ldap3.utils")
        fake_ldap3_conv = types.ModuleType("ldap3.utils.conv")
        fake_ldap3_conv.escape_filter_chars = lambda value: value

        with patch.dict(
            "sys.modules",
            {
                "ldap3": fake_ldap3,
                "ldap3.core": fake_ldap3_core,
                "ldap3.core.exceptions": fake_ldap3_exceptions,
                "ldap3.utils": fake_ldap3_utils,
                "ldap3.utils.conv": fake_ldap3_conv,
            },
        ):
            with patch.object(
                ActiveDirectoryBackend,
                "_authenticate_against_host",
                side_effect=[
                    None,
                    {
                        "username": "directory_user",
                        "email": "",
                        "first_name": "Directory",
                        "last_name": "User",
                        "department": "IT",
                        "position": "Engineer",
                        "branch": "",
                        "groups": set(),
                    },
                ],
            ) as mock_authenticate_host:
                backend = ActiveDirectoryBackend()
                directory_user = backend._authenticate_against_directory("directory_user", "Password123!")

        self.assertIsNotNone(directory_user)
        self.assertEqual(
            [call.kwargs["host"] for call in mock_authenticate_host.call_args_list],
            ["dc1.example.local", "dc2.example.local"],
        )


class AuthenticationSettingsAdminOverrideTests(TestCase):
    @override_settings(
        AD_AUTH_ENABLED=True,
        AD_ONLY_LOGIN=True,
        LOCAL_ACCOUNT_SELF_SERVICE_ENABLED=False,
    )
    def test_authentication_settings_can_enable_local_login_for_all_users(self):
        AuthenticationSettings.objects.update_or_create(
            pk=1,
            defaults={
                "ad_login_enabled": True,
                "local_login_enabled": True,
                "local_account_self_service_enabled": False,
            },
        )
        get_user_model().objects.create_user(
            username="local_enabled_user",
            email="local_enabled_user@bestfinance.com.np",
            password="testpass123",
        )

        response = self.client.post(
            reverse("login"),
            data={"username": "local_enabled_user", "password": "testpass123"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("ticket_list"))

    @override_settings(
        AD_AUTH_ENABLED=True,
        AD_ONLY_LOGIN=True,
        LOCAL_ACCOUNT_SELF_SERVICE_ENABLED=False,
    )
    def test_authentication_settings_can_disable_ad_login_even_when_env_enabled(self):
        AuthenticationSettings.objects.update_or_create(
            pk=1,
            defaults={
                "ad_login_enabled": False,
                "local_login_enabled": False,
                "local_account_self_service_enabled": False,
            },
        )

        with patch("accounts.backends.ActiveDirectoryBackend._authenticate_against_directory") as mock_directory_auth:
            user = authenticate(username="directory_user", password="Password123!")

        self.assertIsNone(user)
        mock_directory_auth.assert_not_called()

    @override_settings(
        AD_AUTH_ENABLED=True,
        AD_ONLY_LOGIN=True,
        LOCAL_ACCOUNT_SELF_SERVICE_ENABLED=False,
    )
    def test_login_page_reflects_admin_enabled_local_login(self):
        AuthenticationSettings.objects.update_or_create(
            pk=1,
            defaults={
                "ad_login_enabled": True,
                "local_login_enabled": True,
                "local_account_self_service_enabled": True,
            },
        )

        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "use a local account if one has been assigned to you")
        self.assertContains(response, reverse("signup"))

    def test_authentication_settings_admin_change_page_is_available(self):
        admin_user = get_user_model().objects.create_superuser(
            username="auth_settings_admin",
            email="auth_settings_admin@bestfinance.com.np",
            password="adminpass123",
        )
        settings_obj = AuthenticationSettings.objects.first()

        self.client.force_login(admin_user)
        response = self.client.get(
            reverse("admin:accounts_authenticationsettings_change", args=[settings_obj.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Authentication settings")
        self.assertContains(response, 'name="ad_login_enabled"')
        self.assertContains(response, 'name="local_login_enabled"')


class CustomUserAdminDepartmentTests(TestCase):
    def setUp(self):
        Department.objects.update_or_create(name="Finance", defaults={})
        Department.objects.update_or_create(name="HR", defaults={})
        Branch.objects.get_or_create(name="Kathmandu", defaults={"branch_id": "KTM"})
        Branch.objects.get_or_create(name="Pokhara", defaults={"branch_id": "PKR"})
        self.admin_user = get_user_model().objects.create_superuser(
            username="siteadmin",
            email="siteadmin@bestfinance.com.np",
            password="adminpass123",
        )
        self.target_user = get_user_model().objects.create_user(
            username="editable_user",
            email="editable_user@bestfinance.com.np",
            password="testpass123",
            department="Finance",
            branch="Kathmandu",
        )
        self.other_user = get_user_model().objects.create_user(
            username="pokhara_user",
            email="pokhara_user@bestfinance.com.np",
            password="testpass123",
            department="Finance",
            branch="Pokhara",
        )

    def test_admin_form_lists_department_choices(self):
        form = CustomUserAdminForm(instance=self.target_user)

        choices = [value for value, _label in form.fields["department"].choices]
        self.assertEqual(choices[0], "")
        self.assertIn("Finance", choices)
        self.assertIn("HR", choices)

    def test_admin_form_lists_branch_choices(self):
        form = CustomUserAdminForm(instance=self.target_user)

        choices = [value for value, _label in form.fields["branch"].choices]
        self.assertEqual(choices[0], "")
        self.assertIn("Kathmandu", choices)
        self.assertIn("Pokhara", choices)

    def test_admin_form_can_switch_user_department(self):
        form = CustomUserAdminForm(
            instance=self.target_user,
            data={
                "username": self.target_user.username,
                "password": self.target_user.password,
                "email": self.target_user.email,
                "department": "HR",
                "is_active": "on",
                "date_joined": self.target_user.date_joined.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        updated_user = form.save()
        self.assertEqual(updated_user.department, "HR")

    def test_admin_form_can_switch_user_branch(self):
        form = CustomUserAdminForm(
            instance=self.target_user,
            data={
                "username": self.target_user.username,
                "password": self.target_user.password,
                "email": self.target_user.email,
                "department": self.target_user.department,
                "branch": "Pokhara",
                "is_active": "on",
                "date_joined": self.target_user.date_joined.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        updated_user = form.save()
        self.assertEqual(updated_user.branch, "Pokhara")

    def test_admin_change_page_shows_department_selector(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("admin:accounts_customuser_change", args=[self.target_user.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<select name="department"')
        self.assertContains(response, 'option value="HR"')

    def test_admin_change_page_shows_branch_selector(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("admin:accounts_customuser_change", args=[self.target_user.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<select name="branch"')
        self.assertContains(response, 'option value="Pokhara"')

    def test_admin_form_includes_signature_field(self):
        form = CustomUserAdminForm(instance=self.target_user)

        self.assertIn("signature_image", form.fields)

    def test_admin_change_page_shows_signature_input(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("admin:accounts_customuser_change", args=[self.target_user.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="signature_image"', html=False)

    def test_admin_form_can_save_signature_image(self):
        form = CustomUserAdminForm(
            instance=self.target_user,
            data={
                "username": self.target_user.username,
                "password": self.target_user.password,
                "email": self.target_user.email,
                "department": self.target_user.department,
                "branch": self.target_user.branch,
                "is_active": "on",
                "date_joined": self.target_user.date_joined.strftime("%Y-%m-%d %H:%M:%S"),
            },
            files={
                "signature_image": SimpleUploadedFile(
                    "signature.png",
                    (
                        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc`\x00"
                        b"\x00\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
                    ),
                    content_type="image/png",
                )
            },
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        updated_user = form.save()
        self.assertTrue(bool(updated_user.signature_image))

    def test_admin_changelist_can_filter_users_by_branch(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("admin:accounts_customuser_changelist"), {"branch__exact": "Kathmandu"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.target_user.username)
        self.assertNotContains(response, self.other_user.username)


class DepartmentRenamePropagationTests(TestCase):
    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(
            username="department_admin",
            email="department_admin@bestfinance.com.np",
            password="adminpass123",
        )
        self.operation_department = Department.objects.create(name="Operation")
        self.finance_department = Department.objects.create(name="Finance")
        self.operation_user = get_user_model().objects.create_user(
            username="operation_user",
            email="operation_user@bestfinance.com.np",
            password="testpass123",
            department="Operation",
        )
        self.finance_user = get_user_model().objects.create_user(
            username="finance_user",
            email="finance_user@bestfinance.com.np",
            password="testpass123",
            department="Finance",
        )
        self.operation_ticket = Ticket.objects.create(
            created_by=self.operation_user,
            subject="Operation ticket",
            description="Created before rename",
            priority="medium",
            status="new",
            department="Operation",
        )
        self.finance_ticket = Ticket.objects.create(
            created_by=self.finance_user,
            subject="Finance ticket",
            description="Should stay unchanged",
            priority="medium",
            status="new",
            department="Finance",
        )

    def test_admin_department_rename_updates_existing_users_and_tickets(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("admin:accounts_department_change", args=[self.operation_department.id]),
            data={
                "name": "Central Operation",
                "_save": "Save",
            },
        )

        self.assertEqual(response.status_code, 302)

        self.operation_department.refresh_from_db()
        self.operation_user.refresh_from_db()
        self.finance_user.refresh_from_db()
        self.operation_ticket.refresh_from_db()
        self.finance_ticket.refresh_from_db()

        self.assertEqual(self.operation_department.name, "Central Operation")
        self.assertEqual(self.operation_user.department, "Central Operation")
        self.assertEqual(self.operation_ticket.department, "Central Operation")
        self.assertEqual(self.finance_user.department, "Finance")
        self.assertEqual(self.finance_ticket.department, "Finance")


class DashboardTicketStatusLinkTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="dashboard_user",
            password="testpass123",
        )
        self.new_ticket = Ticket.objects.create(
            created_by=self.user,
            subject="New dashboard ticket",
            description="Should appear in new count",
            priority="medium",
            status="new",
        )
        self.in_progress_ticket = Ticket.objects.create(
            created_by=self.user,
            subject="In progress dashboard ticket",
            description="Should appear in in-progress count",
            priority="medium",
            status="in_progress",
        )
        self.closed_ticket = Ticket.objects.create(
            created_by=self.user,
            subject="Closed dashboard ticket",
            description="Should appear in closed count",
            priority="medium",
            status="closed",
        )
        self.client.force_login(self.user)

    def test_dashboard_cards_link_to_filtered_ticket_list(self):
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'{reverse("ticket_list")}?scope=created_by_me')
        self.assertContains(response, f'{reverse("ticket_list")}?status=new&amp;scope=created_by_me')
        self.assertContains(response, f'{reverse("ticket_list")}?status=in_progress&amp;scope=created_by_me')
        self.assertContains(response, f'{reverse("ticket_list")}?status=closed&amp;scope=created_by_me')

    def test_dashboard_recent_tickets_include_chat_and_call_actions(self):
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{reverse("ticket_detail", args=[self.new_ticket.id])}#ticket-chat"')
        self.assertContains(response, f'href="{reverse("ticket_detail", args=[self.in_progress_ticket.id])}?autocall=1&callmode=start#ticket-chat"')
        self.assertNotContains(response, f'href="{reverse("ticket_detail", args=[self.closed_ticket.id])}?autocall=1&callmode=start#ticket-chat"')

    def test_dashboard_remote_access_request_shows_view_instead_of_chat_and_call(self):
        approver = get_user_model().objects.create_user(
            username="dashboard_remote_approver",
            password="testpass123",
        )
        remote_access_ticket = Ticket.objects.create(
            created_by=self.user,
            subject="Remote Access Request",
            request_type="access",
            description="Remote access approval request",
            priority="medium",
            status="new",
        )
        RemoteAccessApproval.objects.create(ticket=remote_access_ticket, approver=approver)

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{reverse("ticket_detail", args=[remote_access_ticket.id])}"')
        self.assertNotContains(response, f'href="{reverse("ticket_detail", args=[remote_access_ticket.id])}#ticket-chat"')
        self.assertNotContains(
            response,
            f'href="{reverse("ticket_detail", args=[remote_access_ticket.id])}?autocall=1&callmode=start#ticket-chat"',
        )
