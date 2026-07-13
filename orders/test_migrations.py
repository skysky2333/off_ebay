import uuid
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class ShipmentConstraintMigrationTests(TransactionTestCase):
    migrate_from = ("orders", "0002_alter_inventoryreservation_status_refund")
    migrate_to = (
        "orders",
        "0003_remove_shipment_unique_order_tracking_number_and_more",
    )
    migrate_latest = (
        "orders",
        "0008_shipment_carrier_optional_tracking_constraint",
    )

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        return executor.loader.project_state([target]).apps

    def tearDown(self):
        self.migrate(self.migrate_latest)
        super().tearDown()

    def test_duplicate_tracking_keeps_most_recent_then_highest_pk(self):
        apps = self.migrate(self.migrate_from)
        Order = apps.get_model("orders", "Order")
        Shipment = apps.get_model("orders", "Shipment")
        order = Order.objects.create(
            checkout_key=uuid.uuid4(),
            checkout_fingerprint="f" * 64,
            customer_email="buyer@example.com",
            customer_name="Buyer",
            shipping_line_1="1 Main Street",
            shipping_city="Baltimore",
            shipping_region="MD",
            shipping_postal_code="21201",
            shipping_country_code="US",
            subtotal=Decimal("10.00"),
            total=Decimal("10.00"),
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        oldest = Shipment.objects.create(
            order=order,
            carrier="USPS",
            tracking_number="9400000000000000000000",
        )
        tied_lower_pk = Shipment.objects.create(
            order=order,
            carrier="UPS",
            tracking_number="9400000000000000000000",
        )
        tied_higher_pk = Shipment.objects.create(
            order=order,
            carrier="FedEx",
            tracking_number="9400000000000000000000",
        )
        updated_at = timezone.now()
        Shipment.objects.filter(pk=oldest.pk).update(
            updated_at=updated_at - timedelta(minutes=1)
        )
        Shipment.objects.filter(pk__in=(tied_lower_pk.pk, tied_higher_pk.pk)).update(
            updated_at=updated_at
        )

        apps = self.migrate(self.migrate_to)
        Shipment = apps.get_model("orders", "Shipment")
        shipment = Shipment.objects.get()

        self.assertEqual(shipment.pk, tied_higher_pk.pk)
        self.assertEqual(shipment.carrier, "FedEx")

    def test_existing_refund_becomes_completed_with_an_update_timestamp(self):
        apps = self.migrate(
            ("orders", "0005_order_funding_retry_shipment_completes_order")
        )
        Order = apps.get_model("orders", "Order")
        Refund = apps.get_model("orders", "Refund")
        order = Order.objects.create(
            checkout_key=uuid.uuid4(),
            checkout_fingerprint="r" * 64,
            customer_email="buyer@example.com",
            customer_name="Buyer",
            shipping_line_1="1 Main Street",
            shipping_city="Baltimore",
            shipping_region="MD",
            shipping_postal_code="21201",
            shipping_country_code="US",
            subtotal=Decimal("10.00"),
            total=Decimal("10.00"),
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        refund = Refund.objects.create(
            order=order,
            paypal_refund_id="REFUND-LEGACY",
            amount=Decimal("10.00"),
        )

        apps = self.migrate(self.migrate_latest)
        Refund = apps.get_model("orders", "Refund")
        refund = Refund.objects.get(pk=refund.pk)

        self.assertEqual(refund.status, "completed")
        self.assertIsNotNone(refund.updated_at)
