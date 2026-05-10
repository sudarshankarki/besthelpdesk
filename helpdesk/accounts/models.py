import os
import uuid

from django.contrib.auth.models import AbstractUser
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db import transaction
from django.utils.text import get_valid_filename

from tickets.storage import TicketImageStorage


def user_signature_upload_to(instance: "CustomUser", filename: str) -> str:
    name = get_valid_filename(os.path.basename(filename or "signature"))
    username = get_valid_filename((instance.username or "user").strip()) or "user"
    return f"user_signatures/{username}/{uuid.uuid4().hex}/{name}"


class Branch(models.Model):
    branch_id = models.CharField(max_length=10, primary_key=True)
    name = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "branch"
        ordering = ["name"]

    def __str__(self):
        return f"{self.branch_id} - {self.name}"


class Department(models.Model):
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "department"
        ordering = ["name"]

    def save(self, *args, **kwargs):
        previous_name = None
        if self.pk:
            previous_name = (
                Department.objects.filter(pk=self.pk)
                .values_list("name", flat=True)
                .first()
            )

        with transaction.atomic():
            super().save(*args, **kwargs)
            if previous_name and previous_name.casefold() != self.name.casefold():
                CustomUser.objects.filter(department__iexact=previous_name).update(department=self.name)
                from tickets.models import Ticket

                Ticket.objects.filter(department__iexact=previous_name).update(department=self.name)

    def __str__(self):
        return self.name


class CustomUser(AbstractUser):
    is_itsupport = models.BooleanField(default=False)
    email_verified = models.BooleanField(default=False)
    id = models.BigAutoField(primary_key=True)
    # Add your extra fields here
    phone_number = models.CharField(max_length=15, blank=True, null=True)
    department = models.CharField(max_length=100, blank=True, null=True)
    branch = models.CharField(max_length=100, blank=True, null=True)
    position = models.CharField(max_length=100, blank=True, null=True)
    date_of_birth = models.DateField(blank=True, null=True)
    signature_image = models.ImageField(
        upload_to=user_signature_upload_to,
        storage=TicketImageStorage(),
        blank=True,
        null=True,
    )

    def set_password(self, raw_password):
        self._previous_password_hash = (
            self.password
            if self.pk and self.has_usable_password() and self.password
            else None
        )
        super().set_password(raw_password)

    def __str__(self):
        return self.username


class EmailSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    from_email = models.EmailField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Email settings"
        verbose_name_plural = "Email settings"

    def save(self, *args, **kwargs):
        self.id = 1
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.from_email or "Default"


class AuthenticationSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    ad_login_enabled = models.BooleanField(
        default=True,
        help_text="Allow users to authenticate with Active Directory using the configured LDAP connection.",
    )
    local_login_enabled = models.BooleanField(
        default=False,
        help_text="Allow standard local Django accounts to log in. Recovery superusers can still log in even when this is off.",
    )
    local_account_self_service_enabled = models.BooleanField(
        default=False,
        help_text="Show local signup and password reset options. Requires local login to be enabled.",
    )
    agent_workload_view_enabled = models.BooleanField(
        default=True,
        help_text="Show the read-only Agent Workload View menu and page for normal users.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Authentication settings"
        verbose_name_plural = "Authentication settings"

    def clean(self):
        if self.local_account_self_service_enabled and not self.local_login_enabled:
            raise ValidationError(
                {"local_account_self_service_enabled": "Local self-service requires local login to be enabled."}
            )

        recovery_users = [
            (value or "").strip()
            for value in getattr(settings, "LOCAL_RECOVERY_SUPERUSERS", [])
            if (value or "").strip()
        ]
        if not self.ad_login_enabled and not self.local_login_enabled and not recovery_users:
            raise ValidationError(
                "Enable at least one login method, or configure LOCAL_RECOVERY_SUPERUSERS for emergency access."
            )

    def save(self, *args, **kwargs):
        self.id = 1
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return "Authentication settings"


class PasswordHistory(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="password_history_entries",
    )
    encoded_password = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "created_at"]),
        ]

    def __str__(self):
        return f"{self.user_id} @ {self.created_at:%Y-%m-%d %H:%M:%S}"


