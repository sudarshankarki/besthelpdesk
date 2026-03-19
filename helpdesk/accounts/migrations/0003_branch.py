from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_customuser_branch"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql="""
                        DROP TABLE IF EXISTS branch;
                        CREATE TABLE branch (
                            branch_id varchar(10) PRIMARY KEY,
                            name varchar(100) NOT NULL UNIQUE,
                            created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP
                        );
                    """,
                    reverse_sql="DROP TABLE IF EXISTS branch;",
                ),
            ],
            state_operations=[
                migrations.CreateModel(
                    name="Branch",
                    fields=[
                        ("branch_id", models.CharField(max_length=10, primary_key=True, serialize=False)),
                        ("name", models.CharField(max_length=100, unique=True)),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                    ],
                    options={
                        "db_table": "branch",
                        "ordering": ["name"],
                    },
                ),
            ],
        )
    ]
