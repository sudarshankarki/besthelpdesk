from __future__ import annotations

import logging
import ssl
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend, ModelBackend
from django.db import transaction
from django.db.models import Q

from .auth_mode import (
    is_ad_login_enabled,
    is_local_login_enabled,
    recovery_superuser_names,
)

logger = logging.getLogger(__name__)


def _normalized_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_values = value
    else:
        raw_values = [value]
    cleaned = []
    for item in raw_values:
        text = str(item).strip()
        if text:
            cleaned.append(text)
    return cleaned


def _first_value(value: Any) -> str:
    values = _normalized_values(value)
    return values[0] if values else ""


def _casefolded_values(value: Any) -> set[str]:
    return {item.casefold() for item in _normalized_values(value)}


def _find_local_user(identifier: str):
    UserModel = get_user_model()
    normalized_identifier = (identifier or "").strip()
    if not normalized_identifier:
        return None

    query = Q(username__iexact=normalized_identifier)
    if "@" in normalized_identifier:
        query |= Q(email__iexact=normalized_identifier)

    return UserModel.objects.filter(query).order_by("-is_superuser", "id").first()


def _is_allowed_recovery_superuser(user) -> bool:
    if user is None or not getattr(user, "is_superuser", False):
        return False

    allowed_names = recovery_superuser_names()
    if not allowed_names:
        return True
    return (user.get_username() or "").casefold() in allowed_names


class RecoverySuperuserBackend(ModelBackend):
    """
    Local-auth backend with two modes:
    - normal local authentication when AD-only mode is off
    - recovery-superuser-only authentication when AD-only mode is on
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        identifier = (username or kwargs.get("username") or "").strip()
        if not identifier or not password:
            return None

        user = _find_local_user(identifier)
        if user is None:
            return None

        if not is_local_login_enabled() and not _is_allowed_recovery_superuser(user):
            return None

        if not self.user_can_authenticate(user):
            return None

        if user.check_password(password):
            return user
        return None


class ActiveDirectoryBackend(BaseBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if not is_ad_login_enabled():
            return None

        identifier = (username or kwargs.get("username") or "").strip()
        if not identifier or not password:
            return None

        directory_user = self._authenticate_against_directory(identifier, password)
        if not directory_user:
            return None

        return self._sync_local_user(directory_user)

    def get_user(self, user_id):
        UserModel = get_user_model()
        try:
            return UserModel.objects.get(pk=user_id)
        except UserModel.DoesNotExist:
            return None

    def _authenticate_against_directory(self, identifier: str, password: str) -> dict[str, Any] | None:
        hosts = self._directory_hosts()
        base_dn = (getattr(settings, "AD_BASE_DN", "") or "").strip()
        if not hosts or not base_dn:
            logger.warning(
                "Active Directory authentication is enabled, but AD_SERVER_HOST/AD_SERVER_HOSTS or AD_BASE_DN is missing."
            )
            return None

        try:
            from ldap3 import Connection, Server, SUBTREE, Tls
            from ldap3.core.exceptions import LDAPException
            from ldap3.utils.conv import escape_filter_chars
        except ImportError:
            logger.exception("ldap3 is not installed, so Active Directory authentication cannot be used.")
            return None

        validate_certs = getattr(settings, "AD_VALIDATE_CERTS", True)
        ca_cert_file = (getattr(settings, "AD_CA_CERT_FILE", "") or "").strip() or None
        tls_config = Tls(
            validate=ssl.CERT_REQUIRED if validate_certs else ssl.CERT_NONE,
            ca_certs_file=ca_cert_file,
        )
        normalized_identifier = identifier.strip()
        if "\\" in normalized_identifier:
            normalized_identifier = normalized_identifier.rsplit("\\", 1)[-1]
        escaped_identifier = escape_filter_chars(normalized_identifier)

        search_terms: list[str] = []
        login_attr = (getattr(settings, "AD_LOGIN_ATTR", "") or "").strip()
        username_attr = (getattr(settings, "AD_USERNAME_ATTR", "") or "").strip()
        email_attr = (getattr(settings, "AD_EMAIL_ATTR", "") or "").strip()
        upn_attr = (getattr(settings, "AD_USER_PRINCIPAL_ATTR", "") or "").strip()
        if login_attr:
            search_terms.append(f"({login_attr}={escaped_identifier})")
        if username_attr and username_attr.casefold() != login_attr.casefold():
            search_terms.append(f"({username_attr}={escaped_identifier})")
        if "@" in normalized_identifier:
            if email_attr and email_attr.casefold() not in {login_attr.casefold(), username_attr.casefold()}:
                search_terms.append(f"({email_attr}={escaped_identifier})")
            if upn_attr and upn_attr.casefold() not in {login_attr.casefold(), username_attr.casefold(), email_attr.casefold()}:
                search_terms.append(f"({upn_attr}={escaped_identifier})")
        if not search_terms:
            return None

        attributes = [
            item
            for item in {
                username_attr,
                email_attr,
                upn_attr,
                (getattr(settings, "AD_FIRST_NAME_ATTR", "") or "").strip(),
                (getattr(settings, "AD_LAST_NAME_ATTR", "") or "").strip(),
                (getattr(settings, "AD_DEPARTMENT_ATTR", "") or "").strip(),
                (getattr(settings, "AD_POSITION_ATTR", "") or "").strip(),
                (getattr(settings, "AD_BRANCH_ATTR", "") or "").strip(),
                (getattr(settings, "AD_GROUP_ATTR", "") or "").strip(),
            }
            if item
        ]

        search_base = (getattr(settings, "AD_USER_SEARCH_BASE", "") or "").strip() or base_dn
        bind_dn = (getattr(settings, "AD_BIND_DN", "") or "").strip()
        bind_password = getattr(settings, "AD_BIND_PASSWORD", "")

        for host in hosts:
            try:
                directory_user = self._authenticate_against_host(
                    host=host,
                    password=password,
                    search_base=search_base,
                    search_terms=search_terms,
                    attributes=attributes,
                    username_attr=username_attr,
                    email_attr=email_attr,
                    upn_attr=upn_attr,
                    bind_dn=bind_dn,
                    bind_password=bind_password,
                    server_factory=Server,
                    connection_factory=Connection,
                    subtree_scope=SUBTREE,
                    tls_config=tls_config,
                )
            except LDAPException:
                continue
            if directory_user:
                return directory_user
        return None

    def _directory_hosts(self) -> list[str]:
        configured_hosts = [
            (host or "").strip()
            for host in getattr(settings, "AD_SERVER_HOSTS", [])
            if (host or "").strip()
        ]
        if configured_hosts:
            return configured_hosts

        legacy_host = (getattr(settings, "AD_SERVER_HOST", "") or "").strip()
        return [legacy_host] if legacy_host else []

    def _authenticate_against_host(
        self,
        *,
        host: str,
        password: str,
        search_base: str,
        search_terms: list[str],
        attributes: list[str],
        username_attr: str,
        email_attr: str,
        upn_attr: str,
        bind_dn: str,
        bind_password: str,
        server_factory,
        connection_factory,
        subtree_scope,
        tls_config,
    ) -> dict[str, Any] | None:
        server = server_factory(
            host,
            port=int(getattr(settings, "AD_SERVER_PORT", 636)),
            use_ssl=bool(getattr(settings, "AD_USE_SSL", True)),
            tls=tls_config,
            connect_timeout=int(getattr(settings, "AD_CONNECT_TIMEOUT", 5)),
            get_info=None,
        )
        service_conn = connection_factory(
            server,
            user=bind_dn or None,
            password=bind_password or None,
            auto_bind=True,
            raise_exceptions=True,
            receive_timeout=int(getattr(settings, "AD_RECEIVE_TIMEOUT", 10)),
        )
        try:
            search_filter = f"(&(objectCategory=person)(objectClass=user)(|{''.join(search_terms)}))"
            service_conn.search(
                search_base=search_base,
                search_filter=search_filter,
                search_scope=subtree_scope,
                attributes=attributes,
                size_limit=2,
            )
            entries = list(service_conn.entries)
            if len(entries) != 1:
                return None

            entry = entries[0]
            attribute_map = entry.entry_attributes_as_dict
            user_dn = entry.entry_dn
        finally:
            service_conn.unbind()

        user_conn = connection_factory(
            server,
            user=user_dn,
            password=password,
            auto_bind=True,
            raise_exceptions=True,
            receive_timeout=int(getattr(settings, "AD_RECEIVE_TIMEOUT", 10)),
        )
        user_conn.unbind()

        groups = _casefolded_values(attribute_map.get(getattr(settings, "AD_GROUP_ATTR", "memberOf")))
        allowed_group = (getattr(settings, "AD_ALLOWED_GROUP_DN", "") or "").strip()
        if allowed_group and allowed_group.casefold() not in groups:
            return None

        username = _first_value(attribute_map.get(username_attr))
        email = _first_value(attribute_map.get(email_attr)) or _first_value(attribute_map.get(upn_attr))
        if not username and email and "@" in email:
            username = email.split("@", 1)[0]

        return {
            "username": username,
            "email": email.lower(),
            "first_name": _first_value(attribute_map.get(getattr(settings, "AD_FIRST_NAME_ATTR", ""))),
            "last_name": _first_value(attribute_map.get(getattr(settings, "AD_LAST_NAME_ATTR", ""))),
            "department": _first_value(attribute_map.get(getattr(settings, "AD_DEPARTMENT_ATTR", ""))),
            "position": _first_value(attribute_map.get(getattr(settings, "AD_POSITION_ATTR", ""))),
            "branch": _first_value(attribute_map.get(getattr(settings, "AD_BRANCH_ATTR", ""))),
            "groups": groups,
        }

    def _sync_local_user(self, directory_user: dict[str, Any]):
        UserModel = get_user_model()
        username = (directory_user.get("username") or "").strip()
        email = (directory_user.get("email") or "").strip().lower()
        if not username and not email:
            return None

        by_username = UserModel.objects.filter(username__iexact=username).first() if username else None
        by_email = UserModel.objects.filter(email__iexact=email).first() if email else None
        if by_username and by_email and by_username.pk != by_email.pk:
            logger.warning("Refusing to sync Active Directory user because username and email match different local accounts.")
            return None

        user = by_username or by_email
        if user is not None and getattr(user, "is_superuser", False):
            logger.warning("Refusing to authenticate Active Directory user into a local superuser account.")
            return None

        created = user is None
        if created:
            user = UserModel(username=username or email.split("@", 1)[0], email=email)

        fields_to_update: set[str] = set()

        if username and (created or (user.username or "").casefold() != username.casefold()):
            username_conflict = (
                UserModel.objects.filter(username__iexact=username)
                .exclude(pk=user.pk)
                .exists()
            ) if user.pk else False
            if username_conflict:
                logger.warning("Refusing to sync Active Directory user because the username is already used by another account.")
                return None
            user.username = username
            fields_to_update.add("username")

        if email and (created or (user.email or "").casefold() != email.casefold()):
            email_conflict = (
                UserModel.objects.filter(email__iexact=email)
                .exclude(pk=user.pk)
                .exists()
            ) if user.pk else False
            if email_conflict:
                logger.warning("Refusing to sync Active Directory user because the email is already used by another account.")
                return None
            user.email = email
            fields_to_update.add("email")

        for field_name in ["first_name", "last_name", "department", "position", "branch"]:
            value = (directory_user.get(field_name) or "").strip()
            if value and (created or getattr(user, field_name, "") != value):
                setattr(user, field_name, value)
                fields_to_update.add(field_name)

        if created or not user.is_active:
            user.is_active = True
            fields_to_update.add("is_active")
        if created or not getattr(user, "email_verified", False):
            user.email_verified = True
            fields_to_update.add("email_verified")

        groups = directory_user.get("groups") or set()
        staff_group = (getattr(settings, "AD_STAFF_GROUP_DN", "") or "").strip()
        if staff_group:
            desired_is_staff = staff_group.casefold() in groups
            if created or user.is_staff != desired_is_staff:
                user.is_staff = desired_is_staff
                fields_to_update.add("is_staff")

        itsupport_group = (getattr(settings, "AD_ITSUPPORT_GROUP_DN", "") or "").strip()
        if itsupport_group:
            desired_is_itsupport = itsupport_group.casefold() in groups
            if created or getattr(user, "is_itsupport", False) != desired_is_itsupport:
                user.is_itsupport = desired_is_itsupport
                fields_to_update.add("is_itsupport")

        # Preserve existing local passwords so admins can re-enable local login later
        # without forcing every previously local user through a reset.
        if created and not user.is_superuser:
            user.set_unusable_password()
            fields_to_update.add("password")

        with transaction.atomic():
            if created:
                user.save()
            elif fields_to_update:
                user.save(update_fields=sorted(fields_to_update))

        return user
