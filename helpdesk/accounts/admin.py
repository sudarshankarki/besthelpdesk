from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm
from django import forms

from .models import AuthenticationSettings, Branch, CustomUser, Department, EmailSettings


class CustomUserAdminForm(UserChangeForm):
    department = forms.ChoiceField(required=False, choices=())
    branch = forms.ChoiceField(required=False, choices=())

    class Meta(UserChangeForm.Meta):
        model = CustomUser
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        departments = list(Department.objects.order_by("name").values_list("name", flat=True))
        branches = list(Branch.objects.order_by("name").values_list("name", flat=True))
        current_value = (self.instance.department or "").strip()
        current_branch = (self.instance.branch or "").strip()
        if current_value and current_value not in departments:
            departments.append(current_value)
            departments.sort()
        if current_branch and current_branch not in branches:
            branches.append(current_branch)
            branches.sort()
        self.fields["department"].choices = [("", "---------")] + [(name, name) for name in departments]
        self.fields["branch"].choices = [("", "---------")] + [(name, name) for name in branches]


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    form = CustomUserAdminForm
    list_display = ("username", "email", "department", "branch", "has_signature_image", "is_staff", "is_itsupport", "is_active")
    list_filter = ("department", "branch", "is_staff", "is_itsupport", "is_active")
    search_fields = ("username", "email", "first_name", "last_name", "department", "branch")
    fieldsets = UserAdmin.fieldsets + (
        ("Profile", {"fields": ("phone_number", "department", "branch", "position", "signature_image")}),
        ("IT Support", {"fields": ("is_itsupport",)}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (("IT Support", {"fields": ("is_itsupport",)}),)

    @admin.display(boolean=True, description="Signature")
    def has_signature_image(self, obj):
        return bool(getattr(obj, "signature_image", None))


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ("branch_id", "name", "created_at")
    search_fields = ("branch_id", "name")


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at")
    search_fields = ("name",)


@admin.register(EmailSettings)
class EmailSettingsAdmin(admin.ModelAdmin):
    fields = ("from_email", "updated_at")
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request):
        return not EmailSettings.objects.exists()


@admin.register(AuthenticationSettings)
class AuthenticationSettingsAdmin(admin.ModelAdmin):
    fields = (
        "ad_login_enabled",
        "local_login_enabled",
        "local_account_self_service_enabled",
        "agent_workload_view_enabled",
        "updated_at",
    )
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request):
        return not AuthenticationSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
