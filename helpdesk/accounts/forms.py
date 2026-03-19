from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ValidationError
from django.conf import settings
from .models import Branch, CustomUser, Department


class SignupRequestForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"placeholder": "you@bestfinance.com.np"})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault('class', 'form-control')

    def clean_email(self):
        email = (self.cleaned_data.get('email') or '').strip().lower()
        allowed_domains = getattr(settings, "ALLOWED_REGISTRATION_EMAIL_DOMAINS", [])

        if '@' not in email:
            raise ValidationError("Enter a valid email address.")

        domain = email.split('@', 1)[1]
        if not allowed_domains or domain not in [d.lower() for d in allowed_domains]:
            if len(allowed_domains) == 1:
                allowed_hint = f"@{allowed_domains[0]}"
            else:
                allowed_hint = ", ".join([f"@{d}" for d in allowed_domains])
            raise ValidationError(
                f"Registration is allowed only with {allowed_hint} email addresses."
            )

        if CustomUser.objects.filter(email=email).exists():
            raise ValidationError("A user with this email already exists.")
        return email

class CompleteSignupForm(UserCreationForm):
    phone_number = forms.CharField(
        required=False,
        max_length=15,
        widget=forms.TextInput(
            attrs={
                "placeholder": "Enter your phone number",
                "inputmode": "numeric",
                "pattern": "[0-9]*",
                "autocomplete": "tel",
            }
        ),
    )

    department = forms.ChoiceField(
        required=True,
        choices=(),
        widget=forms.Select(),
    )

    branch = forms.ChoiceField(
        required=True,
        choices=(),
        widget=forms.Select(),
    )

    position = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(attrs={'placeholder': 'Enter your position'})
    )

    first_name = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(attrs={'placeholder': 'Enter your first name'})
    )

    last_name = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(attrs={'placeholder': 'Enter your last name'})
    )

    class Meta:
        model = CustomUser
        fields = (
            'username', 'first_name', 'last_name',
            'phone_number', 'department', 'branch', 'position',
            'password1', 'password2'
        )

    def __init__(self, *args, **kwargs):
        self._email = (kwargs.pop("email") or "").strip().lower()
        super().__init__(*args, **kwargs)
        self.instance.email = self._email
        departments = Department.objects.order_by("name").only("id", "name")
        self.fields["department"].choices = [("", "Select a department")] + [(str(d.id), d.name) for d in departments]
        branches = Branch.objects.order_by("name").only("branch_id", "name")
        self.fields["branch"].choices = [("", "Select a branch")] + [(b.branch_id, b.name) for b in branches]
        for field_name, field in self.fields.items():
            if not isinstance(field.widget, forms.CheckboxInput):
                if isinstance(field.widget, forms.Select):
                    field.widget.attrs.setdefault('class', 'form-select')
                else:
                    field.widget.attrs.setdefault('class', 'form-control')

    def clean(self):
        cleaned = super().clean()
        if not self._email:
            raise ValidationError("Registration link is invalid or expired.")

        if CustomUser.objects.filter(email=self._email).exists():
            raise ValidationError("A user with this email already exists.")
        return cleaned

    def clean_phone_number(self):
        value = (self.cleaned_data.get("phone_number") or "").strip()
        if value and not value.isdigit():
            raise ValidationError("Phone number must contain digits only.")
        return value

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self._email
        user.phone_number = self.cleaned_data.get('phone_number')
        department_id = self.cleaned_data.get("department")
        if department_id:
            user.department = Department.objects.get(id=int(department_id)).name
        branch_id = self.cleaned_data.get('branch')
        if branch_id:
            user.branch = Branch.objects.get(branch_id=branch_id).name
        user.position = self.cleaned_data.get('position')
        user.first_name = self.cleaned_data.get('first_name')
        user.last_name = self.cleaned_data.get('last_name')

        user.email_verified = True
        user.is_active = True

        if commit:
            user.save()
        return user
