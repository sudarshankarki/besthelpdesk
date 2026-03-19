from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0022_ticketchatreadstate"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="chat_is_private",
            field=models.BooleanField(default=False),
        ),
    ]
