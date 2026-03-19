from django.db import migrations


def seed_branches(apps, schema_editor):
    Branch = apps.get_model("accounts", "Branch")
    branches = [
        ("001", "Chabahil"),
        ("002", "NewRoad"),
        ("003", "Mainbranch"),
        ("004", "Amarpath"),
        ("005", "Milanchok"),
        ("006", "Jeetpur"),
        ("007", "Pokhara"),
        ("008", "Bradaghat"),
        ("009", "Kawasoti"),
        ("010", "Nepalgunj"),
        ("011", "Gongabu"),
        ("012", "Dang"),
        ("013", "Banasthali"),
        ("014", "Lagankhel"),
        ("015", "NarayanGhad"),
        ("016", "Galkot"),
        ("017", "Birgunj"),
        ("018", "Itahari"),
        ("999", "Head Office"),
    ]

    for branch_id, name in branches:
        Branch.objects.update_or_create(branch_id=branch_id, defaults={"name": name})


def unseed_branches(apps, schema_editor):
    Branch = apps.get_model("accounts", "Branch")
    Branch.objects.filter(
        branch_id__in=[
            "001",
            "002",
            "003",
            "004",
            "005",
            "006",
            "007",
            "008",
            "009",
            "010",
            "011",
            "012",
            "013",
            "014",
            "015",
            "016",
            "017",
            "018",
            "999",
        ]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_branch"),
    ]

    operations = [
        migrations.RunPython(seed_branches, reverse_code=unseed_branches),
    ]

