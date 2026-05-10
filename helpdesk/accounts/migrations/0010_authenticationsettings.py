from django.db import migrations, models


def create_authentication_settings(apps, schema_editor):
    AuthenticationSettings = apps.get_model("accounts", "AuthenticationSettings")
    AuthenticationSettings.objects.get_or_create(
        pk=1,
        defaults={
            "ad_login_enabled": True,
            "local_login_enabled": False,
            "local_account_self_service_enabled": False,
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0009_rename_accounts_pas_user_id_0e93cb_idx_accounts_pa_user_id_a6f7e8_idx"),
    ]

    operations = [
        migrations.CreateModel(
            name="AuthenticationSettings",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                (
                    "ad_login_enabled",
                    models.BooleanField(
                        default=True,
                        help_text="Allow users to authenticate with Active Directory using the configured LDAP connection.",
                    ),
                ),
                (
                    "local_login_enabled",
                    models.BooleanField(
                        default=False,
                        help_text="Allow standard local Django accounts to log in. Recovery superusers can still log in even when this is off.",
                    ),
                ),
                (
                    "local_account_self_service_enabled",
                    models.BooleanField(
                        default=False,
                        help_text="Show local signup and password reset options. Requires local login to be enabled.",
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Authentication settings",
                "verbose_name_plural": "Authentication settings",
            },
        ),
        migrations.RunPython(create_authentication_settings, migrations.RunPython.noop),
    ]
