from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0002_ebayaccountclosure"),
    ]

    operations = [
        migrations.CreateModel(
            name="EbayAccountIdentity",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("username", models.CharField(max_length=100)),
                ("eias_token", models.CharField(max_length=256)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
