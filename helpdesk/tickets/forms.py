from django import forms
from django.conf import settings
from django.db.models import Q

from accounts.models import CustomUser, Department
from .models import Ticket, is_group_mailbox_email


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


class _TicketStatusNoteMixin:
    def __init__(self, *args, **kwargs):
        self._request_user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        if "status" in self.fields and "status_note" in self.fields:
            ordered = ["status", "status_note"]
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


class TicketForm(forms.ModelForm):
    assign_email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(
            attrs={
                "class": "form-control",
                "placeholder": "Optional: assign directly to one person",
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
        label="Assign Department",
        help_text="Select which department this ticket should be routed to.",
    )

    class Meta:
        model = Ticket
        fields = [
            "subject",
            "request_type",
            "department",
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
                'placeholder': 'Optional: notify a person or group mailbox like hr@bestfinance.com.np'
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
            'department': 'Assign Department',
            'notify_email': 'Notify Email (optional)',
            'description': 'Description',
            "impact": "Impact",
            "urgency": "Urgency",
        }

    def __init__(self, *args, **kwargs):
        self._request_user = kwargs.pop("user", None)
        self._assign_user_id = None
        super().__init__(*args, **kwargs)

        department_names = Department.objects.order_by("name").values_list("name", flat=True)
        self.fields["department"].choices = [("", "Select department")] + [
            (name, name) for name in department_names
        ]
        self.fields["notify_email"].help_text = (
            "Use this for notifications or group mailboxes like hr@bestfinance.com.np or it@bestfinance.com.np."
        )

        self.order_fields(
            [
                "subject",
                "request_type",
                "department",
                "assign_email",
                "notify_email",
                "description",
                "impact",
                "urgency",
                "attachments",
            ]
        )

    def clean_attachments(self):
        attachments = self.cleaned_data.get("attachments") or []
        max_bytes = int(getattr(settings, "TICKET_ATTACHMENT_MAX_BYTES", 20 * 1024 * 1024))
        for upload in attachments:
            if upload.size and upload.size > max_bytes:
                raise forms.ValidationError(f"File too large (max {max_bytes} bytes): {upload.name}")
        return attachments

    def clean_assign_email(self):
        value = (self.cleaned_data.get("assign_email") or "").strip().lower()
        self._assign_user_id = None
        if not value:
            return value

        if is_group_mailbox_email(value):
            raise forms.ValidationError("Group mailboxes belong in Notify Email, not Assign To Email.")

        matches = list(CustomUser.objects.filter(email__iexact=value).values_list("id", flat=True)[:2])
        if len(matches) != 1:
            raise forms.ValidationError("Enter the email of one existing user to assign this ticket.")

        request_user = getattr(self, "_request_user", None)
        if request_user and matches[0] == request_user.id:
            raise forms.ValidationError("You cannot assign a ticket to yourself.")

        self._assign_user_id = matches[0]
        return value

    def clean_notify_email(self):
        value = (self.cleaned_data.get("notify_email") or "").strip().lower()
        return value

    def save(self, commit=True):
        ticket = super().save(commit=False)
        ticket.priority = Ticket.calculate_priority(ticket.impact, ticket.urgency)
        if commit:
            ticket.save()
            self.save_m2m()
        return ticket


class TicketAssigneeUpdateForm(_TicketStatusNoteMixin, forms.ModelForm):
    status_note = _make_status_note_field()

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
