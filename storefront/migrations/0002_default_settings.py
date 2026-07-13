from decimal import Decimal

from django.db import migrations


def create_settings(apps, schema_editor):
    apps.get_model("storefront", "StoreSettings").objects.create(
        pk=1, flat_shipping_amount=Decimal("0.00"), checkout_enabled=False
    )


class Migration(migrations.Migration):
    dependencies = [("storefront", "0001_initial")]

    operations = [migrations.RunPython(create_settings, migrations.RunPython.noop)]
