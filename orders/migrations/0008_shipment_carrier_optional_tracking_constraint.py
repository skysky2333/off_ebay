from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0007_order_refund_permission_shipment_help"),
    ]

    operations = [
        migrations.AlterField(
            model_name="shipment",
            name="carrier",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddConstraint(
            model_name="shipment",
            constraint=models.CheckConstraint(
                condition=models.Q(tracking_number="") | ~models.Q(carrier=""),
                name="shipment_tracking_requires_carrier",
            ),
        ),
    ]
