from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="EbayAccountClosure",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("notification_id", models.CharField(max_length=128, unique=True)),
                ("closed_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
    ]
