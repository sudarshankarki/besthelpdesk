from __future__ import annotations

import re

from django.contrib.auth.hashers import check_password
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _


class DisallowEmailDomainInPasswordValidator:
    """
    Disallow using the user's email domain (e.g. bestfinance.com.np / bestfinance) in the password.
    """

    def __init__(self, min_token_length: int = 4):
        self.min_token_length = int(min_token_length)

    def _domain_tokens(self, email: str) -> set[str]:
        email = (email or "").strip().lower()
        if "@" not in email:
            return set()

        domain = email.split("@", 1)[1].strip().lower()
        if not domain:
            return set()

        tokens = {domain}
        for part in re.split(r"[\\s\\._\\-]+", domain):
            part = (part or "").strip().lower()
            if len(part) >= self.min_token_length:
                tokens.add(part)
        return {t for t in tokens if t}

    def validate(self, password, user=None):
        if not user:
            return

        email = (getattr(user, "email", "") or "").strip().lower()
        if not email:
            return

        password_lc = (password or "").lower()
        for token in self._domain_tokens(email):
            if token and token in password_lc:
                raise ValidationError(
                    _("Password cannot contain your email domain name."),
                    code="password_contains_email_domain",
                )

    def get_help_text(self):
        return _("Your password can’t contain your email domain name.")


class DisallowSequenceInPasswordValidator:
    """
    Disallow specific insecure sequences like '123' in the password.
    """

    def __init__(self, sequences: list[str] | None = None):
        self.sequences = sequences or ["123"]

    def validate(self, password, user=None):
        password_lc = (password or "").lower()
        for seq in self.sequences:
            if seq and seq.lower() in password_lc:
                raise ValidationError(
                    _(f"Password cannot contain the sequence '{seq}'."),
                    code="password_contains_sequence",
                )

    def get_help_text(self):
        sequences = ", ".join([f"'{s}'" for s in self.sequences if s])
        return _("Your password can’t contain insecure sequences like: %(seq)s.") % {"seq": sequences}


class RecentPasswordReuseValidator:
    """
    Disallow reusing recently used passwords.
    """

    def __init__(self, history_size: int = 5):
        self.history_size = int(history_size)

    def validate(self, password, user=None):
        if not user or not password or not getattr(user, "pk", None):
            return

        from .models import PasswordHistory

        encoded_passwords = []
        current_password = getattr(user, "password", "")
        if current_password:
            encoded_passwords.append(current_password)

        history_passwords = PasswordHistory.objects.filter(user=user).values_list(
            "encoded_password",
            flat=True,
        ).order_by("-created_at", "-id")[: self.history_size]
        encoded_passwords.extend(history_passwords)

        for encoded_password in encoded_passwords:
            if encoded_password and check_password(password, encoded_password):
                raise ValidationError(
                    _("You cannot reuse any of your last %(history_size)d passwords."),
                    code="password_reused",
                    params={"history_size": self.history_size},
                )

    def password_changed(self, password, user=None):
        if not user:
            return

        previous_password_hash = getattr(user, "_previous_password_hash", None)
        user._previous_password_hash = None
        if not previous_password_hash:
            return

        from .models import PasswordHistory

        PasswordHistory.objects.create(
            user=user,
            encoded_password=previous_password_hash,
        )

        history_ids_to_keep = list(
            PasswordHistory.objects.filter(user=user)
            .order_by("-created_at", "-id")
            .values_list("id", flat=True)[: self.history_size]
        )
        if history_ids_to_keep:
            PasswordHistory.objects.filter(user=user).exclude(id__in=history_ids_to_keep).delete()

    def get_help_text(self):
        return _("Your password can’t match any of your last %(history_size)d passwords.") % {
            "history_size": self.history_size
        }
