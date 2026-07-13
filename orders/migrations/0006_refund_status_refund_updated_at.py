from django.db import migrations, models


def set_refund_updated_at(apps, schema_editor):
    Refund = apps.get_model("orders", "Refund")
    Refund.objects.using(schema_editor.connection.alias).update(
        updated_at=models.F("created_at")
    )


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0005_order_funding_retry_shipment_completes_order"),
    ]

    operations = [
        migrations.AddField(
            model_name="refund",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("completed", "Completed"),
                    ("failed", "Failed"),
                    ("cancelled", "Cancelled"),
                ],
                default="completed",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="refund",
            name="updated_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.RunPython(set_refund_updated_at, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="refund",
            name="updated_at",
            field=models.DateTimeField(auto_now=True),
        ),
    ]
