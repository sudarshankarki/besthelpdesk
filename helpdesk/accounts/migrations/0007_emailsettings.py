from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0006_department_seed"),
    ]

    operations = [
        migrations.CreateModel(
            name="EmailSettings",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("from_email", models.EmailField(blank=True, default="", max_length=254)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Email settings",
                "verbose_name_plural": "Email settings",
            },
        ),
    ]

