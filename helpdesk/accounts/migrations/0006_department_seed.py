from django.db import migrations


def seed_departments(apps, schema_editor):
    Department = apps.get_model("accounts", "Department")
    departments = [
        "CSD",
        "TELLER",
        "CREDIT",
        "OI",
        "CAD",
        "RISK",
        "AML/CFT",
        "LEGAL",
        "DIGITAL",
        "OPERATION",
        "FINANCE",
        "MARKETING",
        "MGMT",
        "INTERNAL AUDIT",
    ]

    for name in departments:
        Department.objects.update_or_create(name=name, defaults={})


def unseed_departments(apps, schema_editor):
    Department = apps.get_model("accounts", "Department")
    Department.objects.filter(
        name__in=[
            "CSD",
            "TELLER",
            "CREDIT",
            "OI",
            "CAD",
            "RISK",
            "AML/CFT",
            "LEGAL",
            "DIGITAL",
            "OPERATION",
            "FINANCE",
            "MARKETING",
            "MGMT",
            "INTERNAL AUDIT",
        ]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0005_department"),
    ]

    operations = [
        migrations.RunPython(seed_departments, reverse_code=unseed_departments),
    ]

