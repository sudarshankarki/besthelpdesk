from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0009_rename_accounts_pas_user_id_0e93cb_idx_accounts_pa_user_id_a6f7e8_idx"),
        ("tickets", "0029_portal_flash_schedule_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="technicaldocument",
            name="allowed_branches",
            field=models.ManyToManyField(
                blank=True,
                related_name="branch_scoped_technical_documents",
                to="accounts.branch",
            ),
        ),
        migrations.AddField(
            model_name="technicaldocument",
            name="allowed_departments",
            field=models.ManyToManyField(
                blank=True,
                related_name="department_scoped_technical_documents",
                to="accounts.department",
            ),
        ),
        migrations.AlterField(
            model_name="technicaldocument",
            name="visibility",
            field=models.CharField(
                choices=[
                    ("public", "All users"),
                    ("branch", "Branch"),
                    ("department", "Department"),
                    ("restricted", "Restricted"),
                    ("support_only", "IT Support only"),
                ],
                default="public",
                max_length=16,
            ),
        ),
    ]
