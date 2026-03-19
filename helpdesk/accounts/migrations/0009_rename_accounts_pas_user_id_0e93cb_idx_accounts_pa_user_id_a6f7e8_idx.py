from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0008_passwordhistory"),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="passwordhistory",
            new_name="accounts_pa_user_id_a6f7e8_idx",
            old_name="accounts_pas_user_id_0e93cb_idx",
        ),
    ]
