from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0013_alter_customuser_signature_image"),
    ]

    operations = [
        migrations.AddField(
            model_name="customuser",
            name="is_central_operation",
            field=models.BooleanField(default=False),
        ),
    ]
