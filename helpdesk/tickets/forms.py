from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Q
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.forms.models import InlineForeignKeyField, construct_instance
from django.utils import timezone

from accounts.models import Branch, CustomUser, Department
from .models import (
    GroupMailboxEmail,
    IncidentReport,
    IncidentReportSignoff,
    RemoteAccessApproval,
    Ticket,
    is_group_mailbox_email,
    parse_email_list,
)


IT_SUPPORT_DEPARTMENT_NAME = "IT"
IT_SUPPORT_BRANCH_NAME = "Head Office"
IT_SUPPORT_NOTIFY_EMAIL = "it@bestfinance.com.np"
HR_DEPARTMENT_NAME = "HR"
HR_NOTIFY_EMAIL = "hr@bestfinance.com.np"
INCIDENT_SERVICE_OTHER_VALUE = "__other__"


def _restricted_branch_by_department():
    return {
        IT_SUPPORT_DEPARTMENT_NAME: IT_SUPPORT_BRANCH_NAME,
        HR_DEPARTMENT_NAME: IT_SUPPORT_BRANCH_NAME,
    }


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def clean(self, data, initial=None):
        if not data:
            return []
        if not isinstance(data, (list, tuple)):
            data = [data]
        parent_clean = super().clean
        return [parent_clean(item, initial) for item in data]


class UserDisplayChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        full_name = (obj.get_full_name() or "").strip()
        label = full_name or obj.username
        details = " • ".join(
            value
            for value in [
                obj.username if full_name and full_name.casefold() != obj.username.casefold() else "",
                (getattr(obj, "department", "") or "").strip(),
                (getattr(obj, "branch", "") or "").strip(),
            ]
            if value
        )
        return f"{label} ({details})" if details else label


def _clean_uploaded_files(uploads):
    uploads = uploads or []
    max_bytes = int(getattr(settings, "TICKET_ATTACHMENT_MAX_BYTES", 20 * 1024 * 1024))
    for upload in uploads:
        if upload.size and upload.size > max_bytes:
            raise forms.ValidationError(f"File too large (max {max_bytes} bytes): {upload.name}")
    return uploads


def _make_status_note_field() -> forms.CharField:
    return forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Add extra details (optional)",
            }
        ),
        label="Status details",
        help_text="Optional. It will be included in the requester email when marking the ticket Resolved, and saved as the status note for Closed.",
    )


def _make_status_email_attachments_field() -> MultipleFileField:
    return MultipleFileField(
        required=False,
        widget=MultipleFileInput(attrs={"class": "form-control", "multiple": True}),
        label="Resolved Email Attachments (optional)",
        help_text="Optional. These files will be sent to the requester with the resolved notification email. You can select multiple files at once.",
    )


def _make_status_cc_emails_field() -> forms.CharField:
    return forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 2,
                "placeholder": "Optional: manager@bestfinance.com.np, audit@bestfinance.com.np",
            }
        ),
        label="Resolved Email CC (optional)",
        help_text="Optional. These email addresses will be CC'd when the resolved notification email is sent.",
    )


def _make_template_textarea_field(label: str, placeholder: str = "", rows: int = 3) -> forms.CharField:
    return forms.CharField(
        required=False,
        label=label,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": rows,
                "placeholder": placeholder,
            }
        ),
    )


def _clean_cc_email_value(value):
    emails = parse_email_list(value or "")
    for email in emails:
        try:
            validate_email(email)
        except ValidationError:
            raise forms.ValidationError(f"Enter valid CC email addresses only. Invalid value: {email}")
    return ", ".join(emails)


def _incident_report_display_name_for_user(user):
    if not user:
        return ""
    return ((user.get_full_name() or "").strip() or getattr(user, "username", "") or "").strip()


def _find_user_by_incident_display_name(display_name):
    normalized = (display_name or "").strip().casefold()
    if not normalized:
        return None
    for user in CustomUser.objects.filter(is_active=True).order_by("first_name", "last_name", "username"):
        candidates = {
            (getattr(user, "username", "") or "").strip().casefold(),
            _incident_report_display_name_for_user(user).casefold(),
            f"{(getattr(user, 'first_name', '') or '').strip()} {(getattr(user, 'last_name', '') or '').strip()}".strip().casefold(),
        }
        if normalized in candidates:
            return user
    return None


class _TicketStatusNoteMixin:
    def __init__(self, *args, **kwargs):
        self._request_user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        if "status" in self.fields and "status_note" in self.fields:
            ordered = ["status", "status_note"]
            if "status_email_attachments" in self.fields:
                ordered.append("status_email_attachments")
            if "status_cc_emails" in self.fields:
                ordered.append("status_cc_emails")
            for name in self.fields.keys():
                if name not in ordered:
                    ordered.append(name)
            self.order_fields(ordered)

        if "status_cc_emails" in self.fields and not self.is_bound:
            self.initial.setdefault("status_cc_emails", getattr(self.instance, "cc_emails", "") or "")

    def clean(self):
        cleaned_data = super().clean()
        status = cleaned_data.get("status")
        note = (cleaned_data.get("status_note") or "").strip()
        cleaned_data["status_note"] = note

        return cleaned_data

    def clean_status_email_attachments(self):
        return _clean_uploaded_files(self.cleaned_data.get("status_email_attachments"))

    def clean_status_cc_emails(self):
        return _clean_cc_email_value(self.cleaned_data.get("status_cc_emails"))


def _build_notify_emails_by_department_and_branch():
    department_names = list(Department.objects.order_by("name").values_list("name", flat=True))
    branch_names = _all_branch_names()
    department_lookup = {name.casefold(): name for name in department_names}
    branch_lookup = {name.casefold(): name for name in branch_names}
    options = {
        name: {
            "group_mailboxes": [],
            "branches": {branch: [] for branch in branch_names},
        }
        for name in department_names
    }

    user_rows = CustomUser.objects.filter(is_active=True).exclude(email="").exclude(email__isnull=True).values_list(
        "department",
        "branch",
        "email",
    )
    for raw_department, raw_branch, raw_email in user_rows:
        department_name = department_lookup.get(((raw_department or "").strip().casefold()))
        branch_name = branch_lookup.get(((raw_branch or "").strip().casefold()))
        email = (raw_email or "").strip().lower()
        if not department_name or not branch_name or not email:
            continue
        options[department_name]["branches"][branch_name].append(email)

    mailbox_rows = GroupMailboxEmail.objects.exclude(email="").exclude(email__isnull=True).values_list(
        "department__name",
        "email",
    )
    for raw_department, raw_email in mailbox_rows:
        department_name = department_lookup.get(((raw_department or "").strip().casefold()))
        email = (raw_email or "").strip().lower()
        if not department_name or not email:
            continue
        options[department_name]["group_mailboxes"].append(email)

    for department_name, email in _default_notify_email_by_department().items():
        canonical_department = department_lookup.get((department_name or "").strip().casefold())
        normalized_email = (email or "").strip().lower()
        if canonical_department and normalized_email:
            options[canonical_department]["group_mailboxes"].append(normalized_email)

    return {
        department: {
            "group_mailboxes": sorted(set(option_map["group_mailboxes"])),
            "branches": {
                branch: sorted(set(emails))
                for branch, emails in option_map["branches"].items()
            },
        }
        for department, option_map in options.items()
    }


def _build_assignable_emails_by_department_and_branch():
    department_names = list(Department.objects.order_by("name").values_list("name", flat=True))
    branch_names = _all_branch_names()
    department_lookup = {name.casefold(): name for name in department_names}
    branch_lookup = {name.casefold(): name for name in branch_names}
    options = {
        department: {branch: [] for branch in branch_names}
        for department in department_names
    }

    rows = CustomUser.objects.filter(is_active=True).exclude(email="").exclude(email__isnull=True).values_list(
        "department",
        "branch",
        "email",
    )
    for raw_department, raw_branch, raw_email in rows:
        department_name = department_lookup.get(((raw_department or "").strip().casefold()))
        branch_name = branch_lookup.get(((raw_branch or "").strip().casefold()))
        email = (raw_email or "").strip().lower()
        if not department_name or not branch_name or not email:
            continue
        options[department_name][branch_name].append(email)

    return {
        department: {
            branch: sorted(set(emails))
            for branch, emails in branch_map.items()
        }
        for department, branch_map in options.items()
    }


def _all_branch_names():
    branch_names = []
    seen = set()
    for raw_name in Branch.objects.order_by("name").values_list("name", flat=True):
        name = (raw_name or "").strip()
        normalized = name.casefold()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        branch_names.append(name)
    return branch_names


def _restricted_branch_for_department(department=""):
    normalized_department = (department or "").strip().casefold()
    for department_name, branch_name in _restricted_branch_by_department().items():
        if normalized_department == department_name.casefold():
            return branch_name
    return ""


def _default_notify_email_by_department():
    return {
        IT_SUPPORT_DEPARTMENT_NAME: IT_SUPPORT_NOTIFY_EMAIL,
        HR_DEPARTMENT_NAME: HR_NOTIFY_EMAIL,
    }


def _default_notify_email_for_department(department=""):
    normalized_department = (department or "").strip().casefold()
    for department_name, email in _default_notify_email_by_department().items():
        if normalized_department == department_name.casefold():
            return email
    return ""


def _department_for_default_notify_email(email=""):
    normalized_email = (email or "").strip().lower()
    for department_name, default_email in _default_notify_email_by_department().items():
        if normalized_email == (default_email or "").strip().lower():
            return department_name
    return ""


def _branch_names_for_department(department=""):
    restricted_branch = _restricted_branch_for_department(department)
    if restricted_branch:
        return [restricted_branch]
    return _all_branch_names()


class TicketForm(forms.ModelForm):
    assign_email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(
            attrs={
                "class": "form-control",
                "placeholder": "Optional: assign directly to one person",
                "list": "assign-email-suggestions",
            }
        ),
        label="Assign To Email (optional)",
        help_text="Use a single user email here. Group mailboxes like hr@... belong in Notify Email.",
    )
    attachments = MultipleFileField(
        required=False,
        widget=MultipleFileInput(attrs={"class": "form-control", "multiple": True}),
        label="Attachments (optional)",
        help_text="You can select multiple files.",
    )
    incident_detected_at = forms.CharField(
        required=False,
        label="Detected At",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. May 03, 2026 14:30"}),
    )
    incident_additional_departments = forms.MultipleChoiceField(
        required=False,
        choices=(),
        label="Additional Responsible Departments",
        widget=forms.CheckboxSelectMultiple(attrs={"class": "incident-additional-departments"}),
        help_text="Optional. Select other departments that need visibility or coordination for this incident.",
    )
    incident_service_affected = forms.ChoiceField(
        required=False,
        label="Service Affected",
        choices=[],
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    incident_service_affected_other = forms.CharField(
        required=False,
        max_length=32,
        label="Other Service Affected",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Type service if not listed"}),
    )
    incident_current_status = forms.CharField(
        required=False,
        label="Current Status",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Investigating, Contained, Restored"}),
    )
    incident_detected_how = _make_template_textarea_field(
        "How Was It Detected?",
        "Monitoring alert, branch report, customer complaint, etc.",
        rows=2,
    )
    incident_affected = _make_template_textarea_field(
        "Affected Systems / Users",
        "List impacted systems, services, branches, users, or customer groups.",
        rows=2,
    )
    incident_business_impact = _make_template_textarea_field(
        "Business Impact",
        "Describe customer, operational, regulatory, or branch impact.",
        rows=2,
    )
    incident_initial_action = _make_template_textarea_field(
        "Initial Action Taken",
        "Record containment, escalation, workaround, or first response actions.",
        rows=2,
    )
    incident_evidence = _make_template_textarea_field(
        "Evidence / Logs",
        "List logs, screenshots, exports, vendors, or ticket references collected.",
        rows=2,
    )
    cc_emails = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 2,
                "placeholder": "Optional: manager@bestfinance.com.np, audit@bestfinance.com.np",
            }
        ),
        label="CC Emails (optional)",
        help_text="Optional. Add one or more email addresses separated by commas or semicolons.",
    )
    department = forms.ChoiceField(
        required=False,
        choices=(),
        widget=forms.Select(attrs={"class": "form-control"}),
        label="Responsible Department",
        help_text="Select which department this ticket should be routed to.",
    )
    branch = forms.ChoiceField(
        required=False,
        choices=(),
        widget=forms.Select(attrs={"class": "form-control"}),
        label="Responsible Branch",
        help_text="Select which branch this ticket belongs to.",
    )

    class Meta:
        model = Ticket
        fields = [
            "subject",
            "request_type",
            "department",
            "branch",
            "assign_email",
            "notify_email",
            "cc_emails",
            "description",
            "impact",
            "urgency",
            "attachments",
        ]
        widgets = {
            'subject': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Enter ticket subject'
            }),
            "request_type": forms.Select(attrs={"class": "form-control"}),
            'notify_email': forms.EmailInput(attrs={
                'class': 'form-control',
                'placeholder': 'Optional: notify a person or group mailbox like hr@bestfinance.com.np',
                'list': 'notify-email-suggestions',
            }),
            'description': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 4,
                'placeholder': 'Describe your issue in detail...'
            }),
            "impact": forms.Select(attrs={"class": "form-control"}),
            "urgency": forms.Select(attrs={"class": "form-control"}),
        }
        labels = {
            'subject': 'Subject',
            "request_type": "Request Type",
            'department': 'Responsible Department',
            'branch': 'Responsible Branch',
            'notify_email': 'Notify Email (optional)',
            "cc_emails": "CC Emails (optional)",
            'description': 'Description',
            "impact": "Impact",
            "urgency": "Urgency",
        }

    def __init__(self, *args, **kwargs):
        self._request_user = kwargs.pop("user", None)
        self._assign_user_id = None
        self._assign_user_department = ""
        self._assign_user_branch = ""
        self._notify_department = ""
        provided_initial = kwargs.get("initial") or {}
        super().__init__(*args, **kwargs)
        if not self.is_bound and "request_type" not in provided_initial:
            self.initial["request_type"] = "service"
        self.fields["request_type"].initial = "service"

        department_names = Department.objects.order_by("name").values_list("name", flat=True)
        self.fields["department"].choices = [("", "Select department")] + [
            (name, name) for name in department_names
        ]
        self.fields["incident_additional_departments"].choices = [
            (name, name) for name in department_names
        ]
        selected_department = self._selected_department()
        branch_names = _branch_names_for_department(selected_department)
        self.fields["branch"].choices = [("", "Select branch")] + [
            (name, name) for name in branch_names
        ]
        self.all_branch_options = _all_branch_names()
        self.restricted_branch_by_department = {
            **_restricted_branch_by_department(),
        }
        self.default_notify_email_by_department = _default_notify_email_by_department()
        request_user = getattr(self, "_request_user", None)
        restricted_branch = _restricted_branch_for_department(selected_department)
        if restricted_branch:
            self.fields["branch"].error_messages["invalid_choice"] = (
                f"The {selected_department} department can only be routed to the {restricted_branch} branch."
            )
            self.fields["branch"].initial = restricted_branch
        elif request_user and getattr(request_user, "branch", None):
            self.fields["branch"].initial = request_user.branch
        default_notify_email = _default_notify_email_for_department(selected_department)
        if default_notify_email and not self._selected_notify_email():
            self.fields["notify_email"].initial = default_notify_email
        self.assignable_emails_by_department_and_branch = _build_assignable_emails_by_department_and_branch()
        self.notify_emails_by_department_and_branch = _build_notify_emails_by_department_and_branch()
        self.fields["assign_email"].help_text = (
            "Use a single user email here. Suggestions follow the selected department and branch."
        )
        self.fields["notify_email"].help_text = (
            "Use this for notifications or group mailboxes like hr@bestfinance.com.np. Suggestions prefer the selected department's group mailbox and otherwise use the selected department and branch."
        )
        self.fields["incident_service_affected"].choices = [
            ("", "Select service"),
            *IncidentReport.SERVICE_CHOICES,
            (INCIDENT_SERVICE_OTHER_VALUE, "Other / Type below"),
        ]

        self.order_fields(
            [
                "subject",
                "request_type",
                "department",
                "branch",
                "assign_email",
                "notify_email",
                "cc_emails",
                "description",
                "impact",
                "urgency",
                "incident_detected_at",
                "incident_additional_departments",
                "incident_service_affected",
                "incident_service_affected_other",
                "incident_current_status",
                "incident_detected_how",
                "incident_affected",
                "incident_business_impact",
                "incident_initial_action",
                "incident_evidence",
                "attachments",
            ]
        )

    def _selected_department(self):
        if self.is_bound:
            return (self.data.get(self.add_prefix("department")) or "").strip()
        if isinstance(self.initial, dict):
            initial_department = (self.initial.get("department") or "").strip()
            if initial_department:
                return initial_department
        return (getattr(self.instance, "department", "") or "").strip()

    def _selected_notify_email(self):
        if self.is_bound:
            return (self.data.get(self.add_prefix("notify_email")) or "").strip()
        if isinstance(self.initial, dict):
            initial_notify_email = (self.initial.get("notify_email") or "").strip()
            if initial_notify_email:
                return initial_notify_email
        return (getattr(self.instance, "notify_email", "") or "").strip()

    def clean_attachments(self):
        return _clean_uploaded_files(self.cleaned_data.get("attachments"))

    def clean_assign_email(self):
        value = (self.cleaned_data.get("assign_email") or "").strip().lower()
        self._assign_user_id = None
        self._assign_user_department = ""
        self._assign_user_branch = ""
        if not value:
            return value

        if is_group_mailbox_email(value):
            raise forms.ValidationError("Group mailboxes belong in Notify Email, not Assign To Email.")

        matches = list(
            CustomUser.objects.filter(email__iexact=value, is_active=True).only("id", "department", "branch")[:2]
        )
        if len(matches) != 1:
            raise forms.ValidationError("Enter the email of one existing user to assign this ticket.")

        request_user = getattr(self, "_request_user", None)
        if request_user and matches[0].id == request_user.id:
            raise forms.ValidationError("You cannot assign a ticket to yourself.")

        self._assign_user_id = matches[0].id
        self._assign_user_department = (getattr(matches[0], "department", "") or "").strip()
        self._assign_user_branch = (getattr(matches[0], "branch", "") or "").strip()
        return value

    def clean_notify_email(self):
        value = (self.cleaned_data.get("notify_email") or "").strip().lower()
        self._notify_department = ""
        selected_department = self._selected_department()
        default_notify_email = _default_notify_email_for_department(selected_department)
        if not value and default_notify_email:
            value = default_notify_email
        if not value:
            return value

        default_notify_department = _department_for_default_notify_email(value)
        if default_notify_department:
            self._notify_department = default_notify_department
            return value

        user_matches = list(
            CustomUser.objects.filter(email__iexact=value, is_active=True).only("id", "department")[:2]
        )
        if len(user_matches) == 1:
            self._notify_department = (getattr(user_matches[0], "department", "") or "").strip()
            return value

        mailbox = GroupMailboxEmail.objects.select_related("department").filter(email__iexact=value).first()
        if mailbox and mailbox.department_id:
            self._notify_department = (getattr(mailbox.department, "name", "") or "").strip()
        return value

    def clean_cc_emails(self):
        return _clean_cc_email_value(self.cleaned_data.get("cc_emails"))

    def clean(self):
        cleaned_data = super().clean()
        department = (cleaned_data.get("department") or "").strip()
        branch = (cleaned_data.get("branch") or "").strip()
        assignee_department = (self._assign_user_department or "").strip()
        assignee_branch = (self._assign_user_branch or "").strip()
        notify_department = (self._notify_department or "").strip()
        restricted_branch = _restricted_branch_for_department(department)

        if restricted_branch:
            if not branch:
                cleaned_data["branch"] = restricted_branch
                branch = restricted_branch
            elif branch.casefold() != restricted_branch.casefold():
                self.add_error(
                    "branch",
                    f"The {department} department can only be routed to the {restricted_branch} branch.",
                )

        if (
            department
            and self._assign_user_id
            and "branch" not in self.errors
            and assignee_department.casefold() != department.casefold()
            and "assign_email" not in self.errors
        ):
            self.add_error(
                "assign_email",
                f"The selected assignee must belong to the {department} department.",
            )

        if (
            branch
            and self._assign_user_id
            and "branch" not in self.errors
            and assignee_branch.casefold() != branch.casefold()
            and "assign_email" not in self.errors
        ):
            self.add_error(
                "assign_email",
                f"The selected assignee must belong to the {branch} branch.",
            )

        if (
            department
            and cleaned_data.get("notify_email")
            and notify_department
            and notify_department.casefold() != department.casefold()
            and "notify_email" not in self.errors
        ):
            self.add_error(
                "notify_email",
                f"The selected notify email must belong to the {department} department.",
            )

        selected_service = (cleaned_data.get("incident_service_affected") or "").strip()
        custom_service = (cleaned_data.get("incident_service_affected_other") or "").strip()
        if selected_service == INCIDENT_SERVICE_OTHER_VALUE:
            if not custom_service:
                self.add_error("incident_service_affected_other", "Enter the affected service name.")
            cleaned_data["incident_service_affected"] = custom_service
        elif custom_service:
            cleaned_data["incident_service_affected"] = custom_service

        primary_department_key = department.casefold()
        additional_departments = [
            item
            for item in cleaned_data.get("incident_additional_departments") or []
            if item and item.casefold() != primary_department_key
        ]
        cleaned_data["incident_additional_departments"] = additional_departments

        return cleaned_data

    def save(self, commit=True):
        ticket = super().save(commit=False)
        ticket.priority = Ticket.calculate_priority(ticket.impact, ticket.urgency)
        if ticket.request_type == "incident":
            ticket.additional_departments = ", ".join(self.cleaned_data.get("incident_additional_departments") or [])
        else:
            ticket.additional_departments = ""
        if commit:
            ticket.save()
            self.save_m2m()
        return ticket


class RemoteAccessRequestForm(forms.Form):
    subject = forms.CharField(
        required=False,
        initial="Remote Access Request",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "readonly": "readonly",
            }
        ),
        label="Subject",
    )
    details = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 6,
                "placeholder": "Explain why remote access is needed, which third-party user needs access, and any important timing or system details.",
            }
        ),
        label="Details",
        help_text="Describe why the remote access is needed for the third-party user.",
    )
    recommender = UserDisplayChoiceField(
        queryset=CustomUser.objects.none(),
        required=False,
        widget=forms.Select(
            attrs={
                "class": "form-control",
            }
        ),
        label="Recommended By (optional)",
        help_text="Choose a recommender if this request should be reviewed before final approval.",
        empty_label="Send directly to approver",
    )
    approver = UserDisplayChoiceField(
        queryset=CustomUser.objects.none(),
        widget=forms.Select(
            attrs={
                "class": "form-control",
            }
        ),
        label="Approved By",
        help_text="Choose the person who gives the final approval for this remote access request.",
        empty_label="Select final approver",
    )

    def __init__(self, *args, **kwargs):
        request_user = kwargs.pop("request_user", None)
        self._request_user = request_user
        super().__init__(*args, **kwargs)
        queryset = CustomUser.objects.filter(is_active=True)
        if request_user and getattr(request_user, "id", None):
            queryset = queryset.exclude(id=request_user.id)
        queryset = queryset.order_by("first_name", "username")
        self.fields["recommender"].queryset = queryset
        self.fields["approver"].queryset = queryset

    def clean_subject(self):
        return "Remote Access Request"

    def clean(self):
        cleaned_data = super().clean()
        recommender = cleaned_data.get("recommender")
        approver = cleaned_data.get("approver")
        if recommender and approver and recommender.id == approver.id:
            self.add_error("recommender", "Recommended by and approved by must be different users.")
            self.add_error("approver", "Recommended by and approved by must be different users.")
        return cleaned_data


CBS_USER_GROUP_CHOICES = [
    ("1", "Internal Audit Dept."),
    ("2", "NRB/Auditor"),
    ("3", "Marketing"),
    ("4", "Risk Dept."),
    ("5", "CEO/AGM"),
    ("E", "AML/CFT Maker"),
    ("F", "Finance Maker/Checker"),
    ("H", "Card Department Maker"),
    ("I", "AML/CFT"),
    ("J", "Central Operation Checker"),
    ("L", "Credit Head"),
    ("M", "Finance Checker"),
    ("N", "CAD Checker"),
    ("O", "Operation Checker"),
    ("P", "Management"),
    ("Q", "View Only (GSD, Compliance, Recovery)"),
    ("R", "Information Technology Checker"),
    ("S", "Information Technology Maker"),
    ("T", "Treasury Checker"),
    ("U", "Operation Checker (Clearing)"),
    ("V", "Treasury Maker"),
    ("W", "Chief Operating Officer"),
    ("X", "Head Credit Checker (Head Office)"),
    ("Y", "CAD Maker"),
    ("Z", "Head Recovery (Head Office)"),
    ("a", "HR Department Maker"),
    ("b", "HR Department Checker"),
    ("d", "Card Department Checker"),
    ("e", "Operation Maker (Clearing)"),
    ("f", "Finance Maker 1"),
    ("r", "Rate and Limit"),
]


CBS_BRANCH_USER_GROUP_CHOICES = [
    ("A", "Customer Service Desk"),
    ("B", "Operation In charge"),
    ("C", "Teller 1"),
    ("K", "Branch Manager"),
    ("u", "Loan Repayment (View Only)"),
]


class CBSAccessRequestForm(forms.Form):
    subject = forms.CharField(
        required=False,
        initial="CBS Access Request",
        label="Subject",
        widget=forms.TextInput(attrs={"class": "form-control", "readonly": "readonly"}),
    )
    name = forms.CharField(
        label="Name",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Employee full name"}),
    )
    designation = forms.CharField(
        label="Designation",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Officer / Assistant / Manager"}),
    )
    department = forms.CharField(
        label="Department",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Department"}),
    )
    employee_id = forms.CharField(
        label="Employee ID",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Employee ID"}),
    )
    access_user = UserDisplayChoiceField(
        queryset=CustomUser.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label="User Who Needs Access / Acknowledgement Signature",
        help_text="Select the portal user who needs CBS access. Their admin-uploaded profile signature will be used in the endorsement section.",
        empty_label="Select user who needs access",
    )
    user_type = forms.ChoiceField(
        label="Type of User",
        choices=[("new", "New User"), ("amendment", "Amendment for Old User")],
        widget=forms.RadioSelect(attrs={"class": "form-check-input"}),
    )
    old_user_id = forms.CharField(
        required=False,
        label="Old User ID",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Existing CBS user ID, if any"}),
    )
    user_groups = forms.MultipleChoiceField(
        label="User Requirements",
        choices=CBS_USER_GROUP_CHOICES,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "cbs-group-options"}),
        help_text="Select every CBS user group required for this request.",
    )
    amendment_reason = forms.CharField(
        required=False,
        label="Reason for Amendment for Old User",
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Required when the request is an amendment.",
            }
        ),
    )
    recommender = UserDisplayChoiceField(
        queryset=CustomUser.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label="Digital Recommended By (optional)",
        help_text="The selected user will receive the first digital sign-off request.",
        empty_label="Send directly to approver",
    )
    second_recommender = UserDisplayChoiceField(
        queryset=CustomUser.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label="Second Digital Recommended By (optional)",
        help_text="For Branch Office only. If selected, this user receives the second recommendation request after the first recommender signs.",
        empty_label="No second recommender",
    )
    approver = UserDisplayChoiceField(
        queryset=CustomUser.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
        label="Digital Approved By",
        help_text="The selected user gives the final digital approval.",
        empty_label="Select final approver",
    )
    requested_by_name = forms.CharField(
        required=False,
        label="Requested By - Name",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    requested_by_designation = forms.CharField(
        required=False,
        label="Requested By - Designation",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    requested_by_date = forms.DateField(
        required=False,
        label="Requested By - Date",
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    recommended_by_name = forms.CharField(
        required=False,
        label="Recommended By - Name",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    recommended_by_designation = forms.CharField(
        required=False,
        label="Recommended By - Designation",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    recommended_by_date = forms.DateField(
        required=False,
        label="Recommended By - Date",
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    branch_second_recommended_by_name = forms.CharField(
        required=False,
        label="Second Recommended By - Name",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    branch_second_recommended_by_designation = forms.CharField(
        required=False,
        label="Second Recommended By - Designation",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    branch_second_recommended_by_date = forms.DateField(
        required=False,
        label="Second Recommended By - Date",
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    approved_by_name = forms.CharField(
        required=False,
        label="Approved By - Name",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    approved_by_designation = forms.CharField(
        required=False,
        label="Approved By - Designation",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    approved_by_date = forms.DateField(
        required=False,
        label="Approved By - Date",
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    endorsement = forms.BooleanField(
        label="I acknowledge the CBS/Pumori user endorsement.",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def __init__(self, *args, **kwargs):
        request_user = kwargs.pop("request_user", None)
        self._request_user = request_user
        self.office_type = (kwargs.pop("office_type", "head_office") or "head_office").strip()
        self.request_type = "cbs_access_branch" if self.office_type == "branch" else "cbs_access_ho"
        super().__init__(*args, **kwargs)
        queryset = CustomUser.objects.filter(is_active=True)
        if request_user and getattr(request_user, "id", None):
            queryset = queryset.exclude(id=request_user.id)
        queryset = queryset.order_by("first_name", "last_name", "username")
        self.fields["recommender"].queryset = queryset
        self.fields["second_recommender"].queryset = queryset
        self.fields["approver"].queryset = queryset
        self.fields["access_user"].queryset = CustomUser.objects.filter(is_active=True).order_by(
            "first_name",
            "last_name",
            "username",
        )
        if self.office_type == "branch":
            self.fields["department"].label = "Branch / Department"
            self.fields["department"].widget.attrs["placeholder"] = "Branch / Department"
            self.fields["user_groups"].choices = CBS_BRANCH_USER_GROUP_CHOICES
            self.fields["recommender"].label = "Digital Recommended By (Branch / Operation)"
            self.fields["second_recommender"].label = "Second Digital Recommended By (optional)"
            self.fields["approver"].label = "Digital Approved By"
        else:
            self.fields["second_recommender"].widget = forms.HiddenInput()
        if request_user and not self.is_bound:
            display_name = (request_user.get_full_name() or request_user.username or "").strip()
            department_value = (getattr(request_user, "department", "") or "").strip()
            if self.office_type == "branch":
                branch_value = (getattr(request_user, "branch", "") or "").strip()
                department_value = " / ".join(value for value in [branch_value, department_value] if value)
            self.initial.setdefault("name", display_name)
            self.initial.setdefault("designation", (getattr(request_user, "position", "") or "").strip())
            self.initial.setdefault("department", department_value)
            self.initial.setdefault("access_user", request_user.id)
            self.initial.setdefault("requested_by_name", display_name)
            self.initial.setdefault("requested_by_designation", (getattr(request_user, "position", "") or "").strip())
            self.initial.setdefault("requested_by_date", timezone.localdate())

    def clean_subject(self):
        return "CBS Access Request"

    def clean(self):
        cleaned_data = super().clean()
        user_type = cleaned_data.get("user_type")
        old_user_id = (cleaned_data.get("old_user_id") or "").strip()
        amendment_reason = (cleaned_data.get("amendment_reason") or "").strip()
        if user_type == "amendment":
            if not old_user_id:
                self.add_error("old_user_id", "Old User ID is required for amendment requests.")
            if not amendment_reason:
                self.add_error("amendment_reason", "Reason is required for amendment requests.")
        recommender = cleaned_data.get("recommender")
        second_recommender = cleaned_data.get("second_recommender") if self.office_type == "branch" else None
        cleaned_data["second_recommender"] = second_recommender
        approver = cleaned_data.get("approver")
        access_user = cleaned_data.get("access_user")
        if second_recommender and not recommender:
            self.add_error("second_recommender", "Select the first recommender before selecting a second recommender.")
        if recommender and approver and recommender.id == approver.id:
            self.add_error("recommender", "Recommended by and approved by must be different users.")
            self.add_error("approver", "Recommended by and approved by must be different users.")
        if recommender and second_recommender and recommender.id == second_recommender.id:
            self.add_error("second_recommender", "First and second recommended by users must be different.")
        if second_recommender and approver and second_recommender.id == approver.id:
            self.add_error("second_recommender", "Second recommended by and approved by must be different users.")
            self.add_error("approver", "Second recommended by and approved by must be different users.")
        request_user = getattr(self, "_request_user", None)
        if request_user is not None:
            if not getattr(request_user, "signature_image", None):
                raise forms.ValidationError("Your profile signature must be uploaded by admin before you submit a CBS access request.")
            if recommender and recommender.id == request_user.id:
                self.add_error("recommender", "Recommended by cannot be the user who requested this CBS access.")
            if second_recommender and second_recommender.id == request_user.id:
                self.add_error("second_recommender", "Second recommended by cannot be the user who requested this CBS access.")
            if approver and approver.id == request_user.id:
                self.add_error("approver", "Approved by cannot be the user who requested this CBS access.")
        if not access_user:
            self.add_error("access_user", "Select the user who needs CBS access so their acknowledgement signature can be captured.")
        elif not getattr(access_user, "signature_image", None):
            self.add_error("access_user", "The selected user does not have a profile signature uploaded by admin.")
        else:
            if recommender and recommender.id == access_user.id:
                self.add_error("recommender", "Recommended by cannot be the user who needs access / acknowledgement signature.")
            if second_recommender and second_recommender.id == access_user.id:
                self.add_error("second_recommender", "Second recommended by cannot be the user who needs access / acknowledgement signature.")
            if approver and approver.id == access_user.id:
                self.add_error("approver", "Approved by cannot be the user who needs access / acknowledgement signature.")

        requested_by_name = (cleaned_data.get("requested_by_name") or "").strip().casefold()

        def selected_user_names(user):
            if not user:
                return set()
            full_name = ((user.get_full_name() or "").strip()).casefold()
            username = (getattr(user, "username", "") or "").strip().casefold()
            return {value for value in [full_name, username] if value}

        if requested_by_name:
            if recommender and requested_by_name in selected_user_names(recommender):
                self.add_error("recommender", "Recommended by cannot be the same person entered as User Requested By.")
            if second_recommender and requested_by_name in selected_user_names(second_recommender):
                self.add_error("second_recommender", "Second recommended by cannot be the same person entered as User Requested By.")
            if approver and requested_by_name in selected_user_names(approver):
                self.add_error("approver", "Approved by cannot be the same person entered as User Requested By.")
        cleaned_data["old_user_id"] = old_user_id
        cleaned_data["amendment_reason"] = amendment_reason
        return cleaned_data


class RemoteAccessApprovalDecisionForm(forms.Form):
    decision = forms.ChoiceField(
        choices=[
            (RemoteAccessApproval.STATUS_APPROVED, "Approve"),
            (RemoteAccessApproval.STATUS_REJECTED, "Reject"),
        ],
        required=True,
    )
    decision_note = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Optional message to requester. For CBS access, include the CBS User ID if it has been created.",
            }
        ),
        label="Message to Requester / CBS User ID (optional)",
    )

    def clean_decision_note(self):
        return (self.cleaned_data.get("decision_note") or "").strip()


class TicketAssigneeUpdateForm(_TicketStatusNoteMixin, forms.ModelForm):
    status_note = _make_status_note_field()
    status_email_attachments = _make_status_email_attachments_field()
    status_cc_emails = _make_status_cc_emails_field()

    class Meta:
        model = Ticket
        fields = ["status"]
        widgets = {
            "status": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "status" in self.fields:
            allowed = {
                "new",
                "acknowledged",
                "in_progress",
                "waiting_on_user",
                "waiting_on_third_party",
                "resolved",
            }
            current_status = getattr(self.instance, "status", None)
            if current_status == "resolved":
                allowed.add("closed")
                allowed = {"resolved", "closed"}
            self.fields["status"].choices = [
                (value, label)
                for (value, label) in self.fields["status"].choices
                if value in allowed
            ]


class TicketUpdateForm(_TicketStatusNoteMixin, forms.ModelForm):
    status_note = _make_status_note_field()
    status_email_attachments = _make_status_email_attachments_field()
    status_cc_emails = _make_status_cc_emails_field()

    class Meta:
        model = Ticket
        fields = ["status", "priority", "assigned_to"]
        widgets = {
            "status": forms.Select(attrs={"class": "form-select"}),
            "priority": forms.Select(attrs={"class": "form-select"}),
            "assigned_to": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        effective_assignee = getattr(self.instance, "display_assignee", None)
        if "status" in self.fields:
            current = getattr(self.instance, "status", None)
            if current in {"resolved", "closed"}:
                allowed = {"closed"} if current == "closed" else {"resolved", "closed"}
                self.fields["status"].choices = [
                    (value, label)
                    for (value, label) in self.fields["status"].choices
                    if value in allowed
                ]
            elif current not in {"resolved", "closed"}:
                self.fields["status"].choices = [
                    (value, label)
                    for (value, label) in self.fields["status"].choices
                    if value != "closed"
                ]
        # Match direct assignment behavior: support users can reassign to any active user,
        # while still keeping the current/historical assignee selectable.
        queryset = CustomUser.objects.filter(is_active=True)
        if getattr(effective_assignee, "id", None):
            queryset = queryset | CustomUser.objects.filter(id=effective_assignee.id)
        self.fields["assigned_to"].queryset = queryset.distinct().order_by("username")

        if getattr(effective_assignee, "id", None) and not getattr(self.instance, "assigned_to_id", None):
            self.initial["assigned_to"] = effective_assignee.id

        request_user = getattr(self, "_request_user", None)
        if (
            request_user
            and getattr(self.instance, "created_by_id", None) == request_user.id
            and getattr(self.instance, "assigned_to_id", None) != request_user.id
        ):
            self.fields["assigned_to"].queryset = self.fields["assigned_to"].queryset.exclude(
                id=request_user.id
            )

    def clean_assigned_to(self):
        assignee = self.cleaned_data.get("assigned_to")
        if not assignee:
            return assignee

        request_user = getattr(self, "_request_user", None)
        if request_user and getattr(self.instance, "created_by_id", None) == request_user.id:
            if assignee.id == request_user.id and assignee.id != getattr(self.instance, "assigned_to_id", None):
                raise forms.ValidationError("You cannot assign a ticket to yourself.")

        return assignee


class IncidentReportForm(forms.ModelForm):
    service_affected = forms.ChoiceField(
        required=False,
        label="Service Affected",
        choices=[],
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    service_affected_other = forms.CharField(
        required=False,
        label="Or Type Service Affected",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Type a custom affected service if it is not in the list",
            }
        ),
    )
    registered_user = UserDisplayChoiceField(
        queryset=CustomUser.objects.none(),
        required=False,
        label="Incident Registered By",
        empty_label="Select user",
    )
    notified_user = UserDisplayChoiceField(
        queryset=CustomUser.objects.none(),
        required=False,
        label="Incident Notified User",
        empty_label="Select user",
    )
    incident_commander_user = UserDisplayChoiceField(
        queryset=CustomUser.objects.none(),
        required=False,
        label="Incident Commander / Owner",
        empty_label="Select user",
    )
    cc_recipients = forms.ModelMultipleChoiceField(
        queryset=CustomUser.objects.none(),
        required=False,
        label="CC Recipients (for submission email)",
        help_text="Select additional users to CC when submitting the incident report.",
        widget=forms.SelectMultiple(attrs={"class": "form-select", "size": "6"}),
    )

    class Meta:
        model = IncidentReport
        fields = [
            "reporting_employee_name",
            "reporting_employee_designation",
            "reporting_employee_email",
            "reporting_employee_contact",
            "date_of_report",
            "incident_id",
            "date_time_of_occurrence",
            "date_time_of_detection",
            "source_of_incident",
            "incident_location_ip",
            "incident_description",
            "unit_or_department_impacted",
            "systems_impacted",
            "network_impacted",
            "operations_impacted",
            "severity_choice",
            "recovery_actions",
            "recovery_timeframe",
            "post_recovery_verification",
            "recovery_communication",
            "quarantine_process",
            "immediate_actions",
            "root_cause_analysis",
            "eradication_method",
            "lessons_learned",
            "recommendations_for_improvement",
            "action_plan",
            "unit_or_department_requiring_notification",
            "point_of_contact",
            "date_of_notification",
            "evidence_attachments",
            "incident_registered_person",
            "incident_notified_person",
            "incident_title",
            "detected_at",
            "reported_by",
            "incident_commander_user",
            "incident_commander",
            "current_status",
            "service_affected",
            "downtime_duration_minutes",
            "branch_impacted",
            "regulatory_impact",
            "summary_what_happened",
            "summary_detected",
            "summary_affected",
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
            "containment_actions",
            "temporary_workarounds",
            "escalations_raised",
            "eradication_root_cause",
            "eradication_fix_applied",
            "eradication_validation_steps",
            "eradication_systems_restored",
            "communication_stakeholders",
            "communication_update_frequency",
            "communication_latest_update",
            "evidence_ticket_case",
            "evidence_logs",
            "evidence_vendors",
            "review_root_cause_summary",
            "review_lessons_learned",
            "review_preventive_actions",
            "review_action_owners",
        ]
        widgets = {
            "reporting_employee_name": forms.TextInput(attrs={"class": "form-control"}),
            "reporting_employee_designation": forms.TextInput(attrs={"class": "form-control"}),
            "reporting_employee_email": forms.EmailInput(attrs={"class": "form-control"}),
            "reporting_employee_contact": forms.TextInput(attrs={"class": "form-control"}),
            "date_of_report": forms.DateTimeInput(attrs={"class": "form-control", "type": "datetime-local"}),
            "incident_title": forms.TextInput(attrs={"class": "form-control"}),
            "incident_id": forms.TextInput(attrs={"class": "form-control"}),
            "detected_at": forms.TextInput(attrs={"class": "form-control"}),
            "date_time_of_occurrence": forms.DateTimeInput(attrs={"class": "form-control", "type": "datetime-local"}),
            "date_time_of_detection": forms.DateTimeInput(attrs={"class": "form-control", "type": "datetime-local"}),
            "source_of_incident": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "incident_location_ip": forms.TextInput(attrs={"class": "form-control"}),
            "incident_description": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "reported_by": forms.TextInput(attrs={"class": "form-control"}),
            "incident_commander": forms.TextInput(attrs={"class": "form-control"}),
            "severity_level": forms.TextInput(attrs={"class": "form-control"}),
            "severity_choice": forms.Select(attrs={"class": "form-select"}),
            "current_status": forms.TextInput(attrs={"class": "form-control"}),
            "service_affected": forms.Select(attrs={"class": "form-select"}),
            "downtime_duration_minutes": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "min": "0",
                    "placeholder": "e.g. 45",
                }
            ),
            "branch_impacted": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "e.g. Kathmandu Branch",
                }
            ),
            "regulatory_impact": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "summary_what_happened": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "summary_detected": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "summary_affected": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "unit_or_department_impacted": forms.TextInput(attrs={"class": "form-control"}),
            "systems_impacted": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "network_impacted": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "operations_impacted": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "impact_branch_department": forms.TextInput(attrs={"class": "form-control"}),
            "impact_users": forms.TextInput(attrs={"class": "form-control"}),
            "impact_operational": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "impact_regulatory": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "timeline_detection": forms.TextInput(attrs={"class": "form-control"}),
            "timeline_initial_triage": forms.TextInput(attrs={"class": "form-control"}),
            "timeline_containment_started": forms.TextInput(attrs={"class": "form-control"}),
            "timeline_recovery_started": forms.TextInput(attrs={"class": "form-control"}),
            "timeline_service_restored": forms.TextInput(attrs={"class": "form-control"}),
            "timeline_incident_closed": forms.TextInput(attrs={"class": "form-control"}),
            "recovery_actions": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "recovery_timeframe": forms.TextInput(attrs={"class": "form-control"}),
            "post_recovery_verification": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "recovery_communication": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "containment_actions": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "temporary_workarounds": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "escalations_raised": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "quarantine_process": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "immediate_actions": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "root_cause_analysis": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "eradication_method": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "lessons_learned": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "recommendations_for_improvement": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "action_plan": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "eradication_root_cause": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "eradication_fix_applied": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "eradication_validation_steps": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "eradication_systems_restored": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "communication_stakeholders": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "communication_update_frequency": forms.TextInput(attrs={"class": "form-control"}),
            "communication_latest_update": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "evidence_ticket_case": forms.TextInput(attrs={"class": "form-control"}),
            "evidence_logs": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "evidence_attachments": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "evidence_vendors": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "unit_or_department_requiring_notification": forms.TextInput(attrs={"class": "form-control"}),
            "point_of_contact": forms.TextInput(attrs={"class": "form-control"}),
            "date_of_notification": forms.DateTimeInput(attrs={"class": "form-control", "type": "datetime-local"}),
            "review_root_cause_summary": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "review_lessons_learned": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "review_preventive_actions": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "review_action_owners": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "incident_registered_person": forms.TextInput(attrs={"class": "form-control", "placeholder": "Name of the person who registered the incident"}),
            "incident_notified_person": forms.TextInput(attrs={"class": "form-control", "placeholder": "Name of the person who was notified"}),
        }
        labels = {
            "reporting_employee_name": "Reporting Employee's Name",
            "reporting_employee_designation": "Designation",
            "reporting_employee_email": "Email",
            "reporting_employee_contact": "Contact Number",
            "date_of_report": "Date of Report",
            "incident_title": "Incident Title",
            "incident_id": "Incident ID",
            "detected_at": "Date / Time Detected",
            "date_time_of_occurrence": "Date & Time of Occurrence",
            "date_time_of_detection": "Date & Time of Detection",
            "source_of_incident": "Source of Incident",
            "incident_location_ip": "Incident Location (IP)",
            "incident_description": "Incident Description",
            "reported_by": "Reported By",
            "incident_commander": "Incident Commander / Owner",
            "severity_level": "Severity Level (Text)",
            "severity_choice": "Incident Severity",
            "current_status": "Current Status",
            "service_affected": "Service Affected",
            "downtime_duration_minutes": "Downtime Duration (minutes)",
            "branch_impacted": "Branch Impacted",
            "regulatory_impact": "Regulatory Impact (NRB)",
            "summary_what_happened": "Summary: What happened?",
            "summary_detected": "Summary: How was the incident detected?",
            "summary_affected": "Summary: Which systems, users, or services are affected?",
            "unit_or_department_impacted": "Unit or Department Impacted",
            "systems_impacted": "System(s) Impacted",
            "network_impacted": "Network Impacted",
            "operations_impacted": "Operations Impacted",
            "impact_branch_department": "Business Impact: Affected branch / department",
            "impact_users": "Business Impact: Number of users affected",
            "impact_operational": "Business Impact: Operational or customer impact",
            "impact_regulatory": "Business Impact: Regulatory / compliance impact",
            "timeline_detection": "Timeline: Detection",
            "timeline_initial_triage": "Timeline: Initial triage",
            "timeline_containment_started": "Timeline: Containment started",
            "timeline_recovery_started": "Timeline: Recovery started",
            "timeline_service_restored": "Timeline: Service restored",
            "timeline_incident_closed": "Timeline: Incident closed",
            "recovery_actions": "Recovery Actions",
            "recovery_timeframe": "Recovery Timeframe",
            "post_recovery_verification": "Post Recovery Verification",
            "recovery_communication": "Communication",
            "containment_actions": "Containment Actions: Immediate actions taken",
            "temporary_workarounds": "Containment Actions: Temporary workarounds",
            "escalations_raised": "Containment Actions: Escalations raised",
            "quarantine_process": "Quarantine Process",
            "immediate_actions": "Immediate Actions",
            "root_cause_analysis": "Root Cause Analysis",
            "eradication_method": "Eradication",
            "lessons_learned": "Lessons Learned",
            "recommendations_for_improvement": "Recommendations for Improvement",
            "action_plan": "Action Plan",
            "eradication_root_cause": "Eradication and Recovery: Root cause identified",
            "eradication_fix_applied": "Eradication and Recovery: Fix applied",
            "eradication_validation_steps": "Eradication and Recovery: Validation steps completed",
            "eradication_systems_restored": "Eradication and Recovery: Systems restored",
            "communication_stakeholders": "Communication Log: Stakeholders notified",
            "communication_update_frequency": "Communication Log: Update frequency",
            "communication_latest_update": "Communication Log: Latest update shared",
            "evidence_ticket_case": "Evidence and References: Ticket / case number",
            "evidence_logs": "Evidence and References: Logs collected",
            "evidence_attachments": "Evidence and References: Screenshots / attachments",
            "evidence_vendors": "Evidence and References: Related vendors / contacts",
            "unit_or_department_requiring_notification": "Unit or Department Requiring Notification",
            "point_of_contact": "Point of Contact",
            "date_of_notification": "Date of Notification",
            "review_root_cause_summary": "Post-Incident Review: Root cause summary",
            "review_lessons_learned": "Post-Incident Review: Lessons learned",
            "review_preventive_actions": "Post-Incident Review: Preventive actions",
            "review_action_owners": "Post-Incident Review: Action owners and due dates",
            "incident_registered_person": "Incident Registered By",
            "incident_notified_person": "Incident Notified Person",
        }
        help_texts = {
            "incident_title": "Optional. Defaults to the ticket subject if left blank.",
            "incident_id": "Optional. Use your formal incident reference if different from the ticket number.",
            "detected_at": "Record when the incident was first detected.",
            "reported_by": "Who first reported the incident.",
            "incident_commander": "Primary owner or incident commander for the response.",
            "severity_level": "Critical, high, medium, or your internal severity label.",
            "current_status": "Current operational status of the incident.",
            "service_affected": "Select the service area impacted by the incident.",
            "downtime_duration_minutes": "Optional. Total outage time in minutes.",
            "branch_impacted": "Optional. Record the affected branch if the incident is branch-specific.",
            "regulatory_impact": "Check this when NRB reporting or escalation is required.",
            "summary_what_happened": "Describe the incident at a high level.",
            "summary_detected": "Monitoring alert, branch report, customer complaint, etc.",
            "summary_affected": "List impacted systems, services, or user groups.",
            "impact_operational": "Describe customer or business disruption.",
            "impact_regulatory": "Record NRB or compliance concerns, even if the answer is none.",
            "evidence_ticket_case": "Reference the ticket number or external case number.",
            "evidence_logs": "List the log sources reviewed, such as application logs, firewall logs, or database logs.",
            "evidence_attachments": "Describe the screenshots or exported files collected. Use the upload area on the form to attach the actual files.",
            "evidence_vendors": "Record vendor names, contacts, escalation references, or external case IDs.",
            "registered_user": "Select the portal user who registered the incident and must sign this section.",
            "notified_user": "Select the portal user who is responsible for the notified-person sign-off.",
            "incident_commander_user": "Select the portal user who owns and edits the draft incident report.",
        }

    def __init__(self, *args, **kwargs):
        ticket = kwargs.pop("ticket", None)
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        signer_queryset = CustomUser.objects.filter(is_active=True).order_by("first_name", "last_name", "username")
        existing_signer_ids = [
            value
            for value in [
                getattr(self.instance, "registered_user_id", None),
                getattr(self.instance, "notified_user_id", None),
                getattr(self.instance, "incident_commander_user_id", None),
            ]
            if value
        ]
        if existing_signer_ids:
            signer_queryset = CustomUser.objects.filter(
                Q(is_active=True) | Q(id__in=existing_signer_ids)
            ).order_by("first_name", "last_name", "username")

        self.fields["registered_user"].queryset = signer_queryset
        self.fields["registered_user"].widget.attrs["class"] = "form-select"
        self.fields["notified_user"].queryset = signer_queryset
        self.fields["notified_user"].widget.attrs["class"] = "form-select"
        self.fields["incident_commander_user"].queryset = signer_queryset
        self.fields["incident_commander_user"].widget.attrs["class"] = "form-select"
        self.fields["service_affected"].choices = [
            ("", "Select service"),
            *IncidentReport.SERVICE_CHOICES,
            (INCIDENT_SERVICE_OTHER_VALUE, "Other / Type below"),
        ]

        if not self.is_bound:
            if getattr(self.instance, "registered_user_id", None):
                self.initial["registered_user"] = self.instance.registered_user_id
            if getattr(self.instance, "notified_user_id", None):
                self.initial["notified_user"] = self.instance.notified_user_id
            if getattr(self.instance, "incident_commander_user_id", None):
                self.initial["incident_commander_user"] = self.instance.incident_commander_user_id
            if not getattr(self.instance, "notified_user_id", None):
                matched_notified_user = _find_user_by_incident_display_name(
                    getattr(self.instance, "incident_notified_person", "")
                )
                if matched_notified_user is not None:
                    self.initial.setdefault("notified_user", matched_notified_user.id)
            if not getattr(self.instance, "registered_user_id", None):
                matched_registered_user = _find_user_by_incident_display_name(
                    getattr(self.instance, "incident_registered_person", "")
                )
                if matched_registered_user is not None:
                    self.initial.setdefault("registered_user", matched_registered_user.id)

        # Set up CC recipients field
        cc_queryset = CustomUser.objects.filter(is_active=True).order_by("first_name", "last_name", "username")
        self.fields["cc_recipients"].queryset = cc_queryset

        if ticket is not None and not self.is_bound:
            requester_name = getattr(ticket.created_by, "get_full_name", lambda: "")().strip() or getattr(ticket.created_by, "username", "")
            defaults = {
                "incident_title": getattr(ticket, "subject", "") or "",
                "incident_id": getattr(ticket, "ticket_id", "") or "",
                "detected_at": timezone.localtime(ticket.created_at).strftime("%b %d, %Y %H:%M") if getattr(ticket, "created_at", None) else "",
                "reported_by": requester_name,
                "reporting_employee_name": requester_name,
                "reporting_employee_email": getattr(ticket.created_by, "email", "") or "",
                "reporting_employee_contact": getattr(ticket.created_by, "phone_number", "") or getattr(ticket.created_by, "phone", "") or "",
                "date_of_report": timezone.localtime(ticket.created_at).strftime("%Y-%m-%dT%H:%M") if getattr(ticket, "created_at", None) else "",
                "date_time_of_detection": timezone.localtime(ticket.created_at).strftime("%Y-%m-%dT%H:%M") if getattr(ticket, "created_at", None) else "",
                "incident_commander": getattr(ticket, "display_assignee", None) or "",
                "incident_commander_user": getattr(ticket, "assigned_to", None),
                "impact_branch_department": " / ".join([value for value in [ticket.branch, ticket.department] if value]),
                "unit_or_department_impacted": " / ".join([value for value in [ticket.branch, ticket.department] if value]),
                "branch_impacted": (getattr(ticket, "branch", "") or "").strip(),
                "evidence_ticket_case": getattr(ticket, "ticket_id", "") or "",
            }
            if user is not None:
                defaults["registered_user"] = user

            for field_name, value in defaults.items():
                if not self.initial.get(field_name):
                    self.initial[field_name] = value

        incident_owner = self.initial.get("incident_commander")
        if hasattr(incident_owner, "get_full_name"):
            self.initial["incident_commander"] = (incident_owner.get_full_name() or "").strip() or incident_owner.username

        current_service = (
            self.data.get(self.add_prefix("service_affected"))
            if self.is_bound
            else self.initial.get("service_affected") or getattr(self.instance, "service_affected", "")
        )
        known_services = {value for value, _label in IncidentReport.SERVICE_CHOICES}
        if current_service and current_service not in known_services:
            if not self.is_bound:
                self.initial["service_affected"] = INCIDENT_SERVICE_OTHER_VALUE
                self.initial["service_affected_other"] = current_service

    def clean(self):
        cleaned_data = super().clean()
        selected_service = (cleaned_data.get("service_affected") or "").strip()
        custom_service = (cleaned_data.get("service_affected_other") or "").strip()

        if selected_service == INCIDENT_SERVICE_OTHER_VALUE:
            if not custom_service:
                self.add_error("service_affected_other", "Enter the affected service name.")
            cleaned_data["service_affected"] = custom_service
        elif custom_service:
            cleaned_data["service_affected"] = custom_service

        if not cleaned_data.get("incident_registered_person"):
            registered_user = cleaned_data.get("registered_user")
            if registered_user is not None:
                cleaned_data["incident_registered_person"] = (
                    (registered_user.get_full_name() or "").strip() or registered_user.username
                )
            elif getattr(self.instance, "incident_registered_person", ""):
                cleaned_data["incident_registered_person"] = self.instance.incident_registered_person
        if not cleaned_data.get("incident_notified_person"):
            notified_user = cleaned_data.get("notified_user")
            if notified_user is not None:
                cleaned_data["incident_notified_person"] = (
                    (notified_user.get_full_name() or "").strip() or notified_user.username
                )
            elif getattr(self.instance, "incident_notified_person", ""):
                cleaned_data["incident_notified_person"] = self.instance.incident_notified_person
        incident_commander_user = cleaned_data.get("incident_commander_user")
        notified_user = cleaned_data.get("notified_user")
        if incident_commander_user is not None and notified_user is not None and incident_commander_user.id == notified_user.id:
            self.add_error("incident_commander_user", "Select a different user from the incident notified user.")
            self.add_error("notified_user", "Select a different user from the incident commander / owner.")
        if incident_commander_user is not None:
            cleaned_data["incident_commander"] = (
                (incident_commander_user.get_full_name() or "").strip() or incident_commander_user.username
            )
        elif not (cleaned_data.get("incident_commander") or "").strip() and getattr(self.instance, "incident_commander", ""):
            cleaned_data["incident_commander"] = self.instance.incident_commander

        return cleaned_data

    def save(self, commit=True):
        incident_report = super().save(commit=False)
        incident_report.service_affected = (self.cleaned_data.get("service_affected") or "").strip()
        incident_report.registered_user = self.cleaned_data.get("registered_user")
        incident_report.notified_user = self.cleaned_data.get("notified_user")
        incident_report.incident_commander_user = self.cleaned_data.get("incident_commander_user")
        if commit:
            incident_report.save()
            self.save_m2m()
        return incident_report

    def _post_clean(self):
        opts = self._meta
        exclude = self._get_validation_exclusions()
        exclude.add("service_affected")

        for name, field in self.fields.items():
            if isinstance(field, InlineForeignKeyField):
                exclude.add(name)

        try:
            self.instance = construct_instance(self, self.instance, opts.fields, opts.exclude)
        except ValidationError as exc:
            self._update_errors(exc)

        try:
            self.instance.full_clean(exclude=exclude, validate_unique=False)
        except ValidationError as exc:
            self._update_errors(exc)

        if self._validate_unique:
            self.validate_unique()


class IncidentResponseTemplateForm(forms.Form):
    reporting_employee_name = forms.CharField(
        required=False,
        label="Reporting Employee's Name",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    reporting_employee_designation = forms.CharField(
        required=False,
        label="Designation",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    reporting_employee_email = forms.EmailField(
        required=False,
        label="Email",
        widget=forms.EmailInput(attrs={"class": "form-control"}),
    )
    reporting_employee_contact = forms.CharField(
        required=False,
        label="Contact Number",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    date_of_report = forms.DateTimeField(
        required=False,
        label="Date of Report",
        widget=forms.DateTimeInput(attrs={"class": "form-control", "type": "datetime-local"}),
    )
    date_time_of_occurrence = forms.DateTimeField(
        required=False,
        label="Date & Time of Occurrence",
        widget=forms.DateTimeInput(attrs={"class": "form-control", "type": "datetime-local"}),
    )
    date_time_of_detection = forms.DateTimeField(
        required=False,
        label="Date & Time of Detection",
        widget=forms.DateTimeInput(attrs={"class": "form-control", "type": "datetime-local"}),
    )
    source_of_incident = _make_template_textarea_field("Source of Incident", "")
    incident_location_ip = forms.CharField(
        required=False,
        label="Incident Location (IP)",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    incident_description = _make_template_textarea_field("Incident Description", "")
    unit_or_department_impacted = forms.CharField(
        required=False,
        label="Unit or Department Impacted",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    systems_impacted = _make_template_textarea_field("System(s) Impacted", "")
    network_impacted = _make_template_textarea_field("Network Impacted", "")
    operations_impacted = _make_template_textarea_field("Operations Impacted", "")
    recovery_actions = _make_template_textarea_field("Recovery Actions", "")
    recovery_timeframe = forms.CharField(
        required=False,
        label="Recovery Timeframe",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    post_recovery_verification = _make_template_textarea_field("Post Recovery Verification", "")
    recovery_communication = _make_template_textarea_field("Communication", "")
    quarantine_process = _make_template_textarea_field("Quarantine Process", "")
    immediate_actions = _make_template_textarea_field("Immediate Actions", "")
    root_cause_analysis = _make_template_textarea_field("Root Cause Analysis", "")
    eradication_method = _make_template_textarea_field("Eradication", "")
    lessons_learned = _make_template_textarea_field("Lessons Learned", "")
    recommendations_for_improvement = _make_template_textarea_field("Recommendations for Improvement", "")
    action_plan = _make_template_textarea_field("Action Plan", "")
    unit_or_department_requiring_notification = forms.CharField(
        required=False,
        label="Unit or Department Requiring Notification",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    point_of_contact = forms.CharField(
        required=False,
        label="Point of Contact",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    date_of_notification = forms.DateTimeField(
        required=False,
        label="Date of Notification",
        widget=forms.DateTimeInput(attrs={"class": "form-control", "type": "datetime-local"}),
    )
    incident_title = forms.CharField(
        required=False,
        label="Incident Title",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Core banking outage"}),
    )
    incident_id = forms.CharField(
        required=False,
        label="Incident ID",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "INC-2026-001"}),
    )
    detected_at = forms.CharField(
        required=False,
        label="Date / Time Detected",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Apr 11, 2026 09:30"}),
    )
    reported_by = forms.CharField(
        required=False,
        label="Reported By",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Branch Operations Team"}),
    )
    incident_commander = forms.CharField(
        required=False,
        label="Incident Commander / Owner",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "IT Service Manager"}),
    )
    severity_level = forms.ChoiceField(
        required=False,
        label="Incident Severity",
        choices=[("", "Select severity")] + list(IncidentReport.SEVERITY_CHOICES),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    current_status = forms.CharField(
        required=False,
        label="Current Status",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Resolved / Monitoring"}),
    )
    service_affected = forms.ChoiceField(
        required=False,
        label="Service Affected",
        choices=[("", "Select service")] + list(IncidentReport.SERVICE_CHOICES),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    downtime_duration_minutes = forms.IntegerField(
        required=False,
        label="Downtime Duration (minutes)",
        min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "placeholder": "45"}),
    )
    branch_impacted = forms.CharField(
        required=False,
        label="Branch Impacted",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Kathmandu Branch"}),
    )
    regulatory_impact = forms.BooleanField(
        required=False,
        label="Regulatory Impact (NRB)",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    summary_what_happened = _make_template_textarea_field(
        "Summary: What happened?",
        "Describe the incident at a high level.",
    )
    summary_detected = _make_template_textarea_field(
        "Summary: How was the incident detected?",
        "Monitoring alert, branch report, customer complaint, etc.",
    )
    summary_affected = _make_template_textarea_field(
        "Summary: Which systems, users, or services are affected?",
        "CBS, ATM switch, network links, internet banking, affected user groups.",
    )

    impact_branch_department = forms.CharField(
        required=False,
        label="Business Impact: Affected branch / department",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Kathmandu Branch / Operations"}),
    )
    impact_users = forms.CharField(
        required=False,
        label="Business Impact: Number of users affected",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "120 users"}),
    )
    impact_operational = _make_template_textarea_field(
        "Business Impact: Operational or customer impact",
        "Payments delayed, branch service unavailable, customers unable to transact.",
    )
    impact_regulatory = _make_template_textarea_field(
        "Business Impact: Regulatory / compliance impact",
        "Mention NRB impact, reporting obligations, or state none.",
    )

    timeline_detection = forms.CharField(
        required=False,
        label="Timeline: Detection",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "09:30"}),
    )
    timeline_initial_triage = forms.CharField(
        required=False,
        label="Timeline: Initial triage",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "09:40"}),
    )
    timeline_containment_started = forms.CharField(
        required=False,
        label="Timeline: Containment started",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "09:55"}),
    )
    timeline_recovery_started = forms.CharField(
        required=False,
        label="Timeline: Recovery started",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "10:20"}),
    )
    timeline_service_restored = forms.CharField(
        required=False,
        label="Timeline: Service restored",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "11:05"}),
    )
    timeline_incident_closed = forms.CharField(
        required=False,
        label="Timeline: Incident closed",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "12:30"}),
    )

    containment_actions = _make_template_textarea_field(
        "Containment Actions: Immediate actions taken",
        "Traffic reroute, service isolation, rollback, access restriction.",
    )
    temporary_workarounds = _make_template_textarea_field(
        "Containment Actions: Temporary workarounds",
        "Manual process, alternate channel, fallback route.",
    )
    escalations_raised = _make_template_textarea_field(
        "Containment Actions: Escalations raised",
        "Internal escalation, vendor escalation, management notification.",
    )

    eradication_root_cause = _make_template_textarea_field(
        "Eradication and Recovery: Root cause identified",
        "Misconfiguration, hardware failure, expired certificate, etc.",
    )
    eradication_fix_applied = _make_template_textarea_field(
        "Eradication and Recovery: Fix applied",
        "Patch deployed, device replaced, service restarted.",
    )
    eradication_validation_steps = _make_template_textarea_field(
        "Eradication and Recovery: Validation steps completed",
        "Health checks, user verification, monitoring confirmation.",
    )
    eradication_systems_restored = _make_template_textarea_field(
        "Eradication and Recovery: Systems restored",
        "List restored services and current monitoring posture.",
    )

    communication_stakeholders = _make_template_textarea_field(
        "Communication Log: Stakeholders notified",
        "Branch manager, IT leadership, vendors, customers, NRB liaison.",
    )
    communication_update_frequency = forms.CharField(
        required=False,
        label="Communication Log: Update frequency",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Every 30 minutes"}),
    )
    communication_latest_update = _make_template_textarea_field(
        "Communication Log: Latest update shared",
        "Summarize the latest message or status shared.",
    )

    evidence_ticket_case = forms.CharField(
        required=False,
        label="Evidence and References: Ticket / case number",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "BFC-ABC1234567"}),
    )
    evidence_logs = _make_template_textarea_field(
        "Evidence and References: Logs collected",
        "Firewall logs, CBS server logs, monitoring screenshots.",
    )
    evidence_attachments = _make_template_textarea_field(
        "Evidence and References: Screenshots / attachments",
        "List evidence files or screenshots captured.",
    )
    evidence_vendors = _make_template_textarea_field(
        "Evidence and References: Related vendors / contacts",
        "Vendor name, contact person, escalation reference.",
    )

    review_root_cause_summary = _make_template_textarea_field(
        "Post-Incident Review: Root cause summary",
        "Summarize the final understanding of the incident cause.",
    )
    review_lessons_learned = _make_template_textarea_field(
        "Post-Incident Review: Lessons learned",
        "Document key takeaways.",
    )
    review_preventive_actions = _make_template_textarea_field(
        "Post-Incident Review: Preventive actions",
        "What should be improved or changed to prevent recurrence?",
    )
    review_action_owners = _make_template_textarea_field(
        "Post-Incident Review: Action owners and due dates",
        "Owner - action - target date.",
    )

    incident_registered_person = forms.CharField(
        required=False,
        label="Incident Registered By",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Name of the person who registered the incident"}),
    )
    incident_notified_person = forms.CharField(
        required=False,
        label="Incident Notified Person",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Name of the person who was notified"}),
    )

class IncidentReportSignoffForm(forms.ModelForm):
    user = UserDisplayChoiceField(
        queryset=CustomUser.objects.none(),
        required=False,
        label="Notified User",
        empty_label="Select user",
    )

    class Meta:
        model = IncidentReportSignoff
        fields = ["level", "user"]
        widgets = {
            "level": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "min": "1",
                    "placeholder": "1",
                }
            ),
        }
        labels = {
            "level": "Level",
            "user": "Notified User",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        signer_queryset = CustomUser.objects.filter(is_active=True).order_by("first_name", "last_name", "username")
        current_user_id = getattr(self.instance, "user_id", None)
        if current_user_id:
            signer_queryset = CustomUser.objects.filter(Q(is_active=True) | Q(id=current_user_id)).order_by(
                "first_name",
                "last_name",
                "username",
            )
        self.fields["user"].queryset = signer_queryset
        self.fields["user"].widget.attrs["class"] = "form-select"
        self.fields["level"].required = False


class BaseIncidentReportSignoffFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        seen_levels = set()
        seen_users = set()
        require_notified_user = bool(getattr(self, "require_notified_user", False))
        required_level_count = max(1, min(int(getattr(self, "required_level_count", 2) or 2), 6))

        for form in self.forms:
            if not hasattr(form, "cleaned_data"):
                continue
            if form.cleaned_data.get("DELETE"):
                continue

            user = form.cleaned_data.get("user")
            level = form.cleaned_data.get("level")
            if not user and not level:
                continue

            if user is None:
                raise ValidationError("Each notified sign-off row must have a user.")
            if level in {None, ""}:
                raise ValidationError("Each notified sign-off row must have a level.")

            if level in seen_levels:
                raise ValidationError("Notification levels must be unique.")
            seen_levels.add(level)

            if user.id in seen_users:
                raise ValidationError("The same user cannot be added more than once in the notified sign-off chain.")
            seen_users.add(user.id)

        if require_notified_user:
            required_levels = set(range(1, required_level_count + 1))
            missing_required_levels = sorted(required_levels - set(seen_levels))
            if missing_required_levels:
                level_text = ", ".join(f"order {level}" for level in missing_required_levels)
                raise ValidationError(f"Add users for selected review/approval sign-off {level_text}.")


IncidentReportNotifiedSignoffFormSet = inlineformset_factory(
    IncidentReport,
    IncidentReportSignoff,
    form=IncidentReportSignoffForm,
    formset=BaseIncidentReportSignoffFormSet,
    fields=["level", "user"],
    extra=6,
    max_num=6,
    can_delete=True,
)


class TicketChatPrivacyForm(forms.Form):
    chat_is_private = forms.BooleanField(
        required=False,
        label="Private ticket chat",
        help_text="When enabled, only the requester and the assigned user can open the chat.",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def __init__(self, *args, ticket=None, user=None, **kwargs):
        self.ticket = ticket
        super().__init__(*args, **kwargs)

        if ticket is None:
            raise ValueError("TicketChatPrivacyForm requires a ticket instance.")

        self.fields["chat_is_private"].initial = ticket.chat_is_private

    def save(self):
        ticket = self.ticket
        ticket.chat_is_private = bool(self.cleaned_data.get("chat_is_private"))
        ticket.save()
        return ticket
