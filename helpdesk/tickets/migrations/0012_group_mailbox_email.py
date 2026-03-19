from django.db import migrations, models
import django.db.models.deletion


def seed_group_mailboxes(apps, schema_editor):
    from django.conf import settings

    GroupMailboxEmail = apps.get_model("tickets", "GroupMailboxEmail")
    Department = apps.get_model("accounts", "Department")

    emails = getattr(settings, "GROUP_MAILBOX_EMAILS", []) or []
    for raw in emails:
        email = (raw or "").strip().lower()
        if not email or "@" not in email:
            continue

        local_part = (email.split("@", 1)[0] or "").strip()
        dept = Department.objects.filter(name__iexact=local_part).first() if local_part else None

        defaults = {}
        if dept:
            defaults["department_id"] = dept.pk

        GroupMailboxEmail.objects.update_or_create(email=email, defaults=defaults)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0007_emailsettings"),
        ("tickets", "0011_ticket_image_storage"),
    ]

    operations = [
        migrations.CreateModel(
            name="GroupMailboxEmail",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.EmailField(max_length=254, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "department",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="group_mailboxes",
                        to="accounts.department",
                    ),
                ),
            ],
            options={
                "ordering": ["email"],
            },
        ),
        migrations.RunPython(seed_group_mailboxes, reverse_code=migrations.RunPython.noop),
    ]

