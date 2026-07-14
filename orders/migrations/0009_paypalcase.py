import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0008_shipment_carrier_optional_tracking_constraint"),
    ]

    operations = [
        migrations.CreateModel(
            name="PayPalCase",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[("dispute", "Dispute"), ("reversal", "Reversal")],
                        max_length=10,
                    ),
                ),
                ("paypal_case_id", models.CharField(max_length=255)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("OPEN", "Open"),
                            (
                                "WAITING_FOR_BUYER_RESPONSE",
                                "Waiting for buyer response",
                            ),
                            (
                                "WAITING_FOR_SELLER_RESPONSE",
                                "Waiting for seller response",
                            ),
                            ("UNDER_REVIEW", "Under review"),
                            ("RESOLVED", "Resolved"),
                            ("REVERSED", "Reversed"),
                        ],
                        max_length=32,
                    ),
                ),
                ("reason", models.CharField(blank=True, max_length=64)),
                ("outcome", models.CharField(blank=True, max_length=64)),
                ("stage", models.CharField(blank=True, max_length=32)),
                ("channel", models.CharField(blank=True, max_length=32)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("currency", models.CharField(max_length=3)),
                ("seller_response_due_at", models.DateTimeField(blank=True, null=True)),
                ("provider_created_at", models.DateTimeField(blank=True, null=True)),
                ("provider_updated_at", models.DateTimeField(blank=True, null=True)),
                ("last_event_type", models.CharField(max_length=80)),
                ("needs_review", models.BooleanField(default=True)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="paypal_cases",
                        to="orders.order",
                    ),
                ),
            ],
            options={
                "ordering": ("-updated_at",),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("kind", "paypal_case_id"),
                        name="unique_paypal_case",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("amount__gt", 0)),
                        name="paypal_case_amount_positive",
                    ),
                ],
            },
        ),
    ]
