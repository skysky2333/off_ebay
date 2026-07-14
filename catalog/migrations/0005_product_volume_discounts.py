from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0004_ebaypublickeylookupbudget"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="volume_discounts",
            field=models.JSONField(default=list),
        ),
    ]
