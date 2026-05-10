from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0010_authenticationsettings"),
    ]

    operations = [
        migrations.AddField(
            model_name="authenticationsettings",
            name="agent_workload_view_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Show the read-only Agent Workload View menu and page for normal users.",
            ),
        ),
    ]
