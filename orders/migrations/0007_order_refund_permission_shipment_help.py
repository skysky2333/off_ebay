from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0006_refund_status_refund_updated_at"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="order",
            options={
                "ordering": ("-created_at",),
                "permissions": (
                    ("refund_order", "Can refund orders through PayPal"),
                ),
            },
        ),
        migrations.AlterField(
            model_name="shipment",
            name="completes_order",
            field=models.BooleanField(
                default=True,
                help_text="Check this only when no more packages are expected for the order.",
                verbose_name="Final shipment",
            ),
        ),
    ]
