from django.db import migrations, models


def create_lookup_budget(apps, schema_editor):
    apps.get_model("catalog", "EbayPublicKeyLookupBudget").objects.create()


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0003_ebayaccountidentity"),
    ]

    operations = [
        migrations.CreateModel(
            name="EbayPublicKeyLookupBudget",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("window", models.PositiveBigIntegerField(default=0)),
                ("count", models.PositiveIntegerField(default=0)),
            ],
        ),
        migrations.RunPython(create_lookup_budget, migrations.RunPython.noop),
    ]
