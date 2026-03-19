from django.contrib.auth.models import AbstractUser
from django.conf import settings
from django.db import models


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


