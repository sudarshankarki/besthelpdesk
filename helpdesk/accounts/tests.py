from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.core import mail
from django.test import Client, TestCase
from django.test.utils import override_settings
from django.urls import reverse
import re
from accounts.admin import CustomUserAdminForm
from accounts.models import Department, PasswordHistory


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


class EmailVerificationSignupTests(TestCase):
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


class PasswordResetHistoryTests(TestCase):
    def setUp(self):
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


class CustomUserAdminDepartmentTests(TestCase):
    def setUp(self):
        Department.objects.update_or_create(name="Finance", defaults={})
        Department.objects.update_or_create(name="HR", defaults={})
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
        )

    def test_admin_form_lists_department_choices(self):
        form = CustomUserAdminForm(instance=self.target_user)

        choices = [value for value, _label in form.fields["department"].choices]
        self.assertEqual(choices[0], "")
        self.assertIn("Finance", choices)
        self.assertIn("HR", choices)

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

    def test_admin_change_page_shows_department_selector(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("admin:accounts_customuser_change", args=[self.target_user.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<select name="department"')
        self.assertContains(response, 'option value="HR"')
