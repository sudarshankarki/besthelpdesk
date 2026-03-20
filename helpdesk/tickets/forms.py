from django import forms
from django.conf import settings
from django.db.models import Q

from accounts.models import Branch, CustomUser, Department
from .models import GroupMailboxEmail, Ticket, is_group_mailbox_email


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
        help_text="Optional. If provided, it will be included in the requester email when marking the ticket Resolved/Closed.",
    )


def _make_close_email_attachments_field() -> MultipleFileField:
    return MultipleFileField(
        required=False,
        widget=MultipleFileInput(attrs={"class": "form-control", "multiple": True}),
        label="Close Email Attachments (optional)",
        help_text="Optional. These files will be sent to the requester with the close notification. You can select multiple files at once.",
    )


class _TicketStatusNoteMixin:
    def __init__(self, *args, **kwargs):
        self._request_user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        if "status" in self.fields and "status_note" in self.fields:
            ordered = ["status", "status_note"]
            if "close_email_attachments" in self.fields:
                ordered.append("close_email_attachments")
            for name in self.fields.keys():
                if name not in ordered:
                    ordered.append(name)
            self.order_fields(ordered)

    def clean(self):
        cleaned_data = super().clean()
        status = cleaned_data.get("status")
        note = (cleaned_data.get("status_note") or "").strip()
        cleaned_data["status_note"] = note

        return cleaned_data

    def clean_close_email_attachments(self):
        return _clean_uploaded_files(self.cleaned_data.get("close_email_attachments"))


def _build_department_email_options(include_group_mailboxes=False):
    department_names = list(Department.objects.order_by("name").values_list("name", flat=True))
    department_lookup = {name.casefold(): name for name in department_names}
    options = {name: [] for name in department_names}

    rows = CustomUser.objects.filter(is_active=True).exclude(email="").exclude(email__isnull=True).values_list(
        "department",
        "email",
    )
    for raw_department, raw_email in rows:
        department_name = department_lookup.get(((raw_department or "").strip().casefold()))
        email = (raw_email or "").strip().lower()
        if not department_name or not email:
            continue
        options[department_name].append(email)

    if include_group_mailboxes:
        mailbox_rows = GroupMailboxEmail.objects.exclude(email="").exclude(email__isnull=True).values_list(
            "department__name",
            "email",
        )
        for raw_department, raw_email in mailbox_rows:
            department_name = department_lookup.get(((raw_department or "").strip().casefold()))
            email = (raw_email or "").strip().lower()
            if not department_name or not email:
                continue
            options[department_name].append(email)

    return {
        name: sorted(set(emails))
        for name, emails in options.items()
    }


def _build_assignable_emails_by_department_and_branch():
    department_names = list(Department.objects.order_by("name").values_list("name", flat=True))
    branch_names = list(Branch.objects.order_by("name").values_list("name", flat=True))
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
        super().__init__(*args, **kwargs)

        department_names = Department.objects.order_by("name").values_list("name", flat=True)
        self.fields["department"].choices = [("", "Select department")] + [
            (name, name) for name in department_names
        ]
        branch_names = Branch.objects.order_by("name").values_list("name", flat=True)
        self.fields["branch"].choices = [("", "Select branch")] + [
            (name, name) for name in branch_names
        ]
        request_user = getattr(self, "_request_user", None)
        if request_user and getattr(request_user, "branch", None):
            self.fields["branch"].initial = request_user.branch
        self.assignable_emails_by_department_and_branch = _build_assignable_emails_by_department_and_branch()
        self.notify_emails_by_department = _build_department_email_options(include_group_mailboxes=True)
        self.fields["assign_email"].help_text = (
            "Use a single user email here. Suggestions follow the selected department and branch."
        )
        self.fields["notify_email"].help_text = (
            "Use this for notifications or group mailboxes like hr@bestfinance.com.np. Suggestions follow the selected department."
        )

        self.order_fields(
            [
                "subject",
                "request_type",
                "department",
                "branch",
                "assign_email",
                "notify_email",
                "description",
                "impact",
                "urgency",
                "attachments",
            ]
        )

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
        if not value:
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

    def clean(self):
        cleaned_data = super().clean()
        department = (cleaned_data.get("department") or "").strip()
        branch = (cleaned_data.get("branch") or "").strip()
        assignee_department = (self._assign_user_department or "").strip()
        assignee_branch = (self._assign_user_branch or "").strip()
        notify_department = (self._notify_department or "").strip()

        if (
            department
            and self._assign_user_id
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

        return cleaned_data

    def save(self, commit=True):
        ticket = super().save(commit=False)
        ticket.priority = Ticket.calculate_priority(ticket.impact, ticket.urgency)
        if commit:
            ticket.save()
            self.save_m2m()
        return ticket


class TicketAssigneeUpdateForm(_TicketStatusNoteMixin, forms.ModelForm):
    status_note = _make_status_note_field()
    close_email_attachments = _make_close_email_attachments_field()

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
            self.fields["status"].choices = [
                (value, label)
                for (value, label) in self.fields["status"].choices
                if value in allowed
            ]


class TicketUpdateForm(_TicketStatusNoteMixin, forms.ModelForm):
    status_note = _make_status_note_field()
    close_email_attachments = _make_close_email_attachments_field()

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
        if "status" in self.fields:
            current = getattr(self.instance, "status", None)
            if current not in {"resolved", "closed"}:
                self.fields["status"].choices = [
                    (value, label)
                    for (value, label) in self.fields["status"].choices
                    if value != "closed"
                ]
        self.fields["assigned_to"].queryset = CustomUser.objects.filter(
            Q(is_itsupport=True) | Q(is_staff=True)
        ).distinct()

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
