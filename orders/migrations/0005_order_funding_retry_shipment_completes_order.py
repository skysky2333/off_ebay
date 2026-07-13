from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0004_shipment_provider_updated_at"),
    ]

    operations = [
        migrations.AlterField(
            model_name="order",
            name="status",
            field=models.CharField(
                choices=[
                    ("awaiting_payment", "Awaiting payment"),
                    ("payment_processing", "Processing payment"),
                    ("capture_pending", "Confirming payment"),
                    ("funding_retry", "Payment method needed"),
                    ("paid", "Paid"),
                    ("fulfilling", "Fulfilling"),
                    ("shipped", "Shipped"),
                    ("partially_refunded", "Partially refunded"),
                    ("cancelled", "Cancelled"),
                    ("expired", "Expired"),
                    ("refunded", "Refunded"),
                ],
                default="awaiting_payment",
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="shipment",
            name="completes_order",
            field=models.BooleanField(default=True, verbose_name="Final shipment"),
        ),
    ]
