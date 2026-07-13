import uuid
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Event, current_thread
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from catalog.inventory import EbayInventoryGateway
from catalog.models import Product

from .inventory import InventoryUnavailable
from .models import InventoryReservation, Order, OrderItem
from .services import (
    CheckoutLine,
    OrderStateError,
    ShippingAddress,
    capture_paypal_order,
    create_guest_order,
    reconcile_due_funding_retry,
)


class InventoryDouble:
    def reserve(self, reservation):
        return None


@skipUnless(connection.vendor == "postgresql", "PostgreSQL concurrency test")
class CheckoutConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.product = Product.objects.create(
            ebay_item_id="postgres-concurrency",
            slug="postgres-concurrency",
            title="Concurrent checkout item",
            price=Decimal("10.00"),
            currency="USD",
            listing_url="https://www.ebay.com/itm/postgres-concurrency",
            listing_type="FixedPriceItem",
            quantity=2,
            last_synced_at=timezone.now(),
        )

    def test_duplicate_checkout_key_returns_one_order(self):
        checkout_key = uuid.uuid4()
        barrier = Barrier(2)

        def create():
            connections.close_all()
            barrier.wait()
            order = create_guest_order(
                checkout_key=checkout_key,
                email="buyer@example.com",
                address=ShippingAddress(
                    name="Ada Buyer",
                    line_1="1 Main Street",
                    city="Baltimore",
                    region="MD",
                    postal_code="21201",
                    country_code="US",
                ),
                lines=[CheckoutLine(product_id=self.product.pk, quantity=1)],
                shipping_total=Decimal("0.00"),
                expected_total=Decimal("9.00"),
                inventory=InventoryDouble(),
            )
            connections.close_all()
            return order.pk

        with ThreadPoolExecutor(max_workers=2) as executor:
            order_ids = list(executor.map(lambda _: create(), range(2)))

        self.assertEqual(order_ids[0], order_ids[1])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 1)
        self.assertEqual(InventoryReservation.objects.count(), 1)

    def test_distinct_checkouts_cannot_reserve_the_same_last_unit(self):
        Product.objects.filter(pk=self.product.pk).update(quantity=1)
        barrier = Barrier(2)

        def create(checkout_key):
            connections.close_all()
            barrier.wait()
            try:
                order = create_guest_order(
                    checkout_key=checkout_key,
                    email="buyer@example.com",
                    address=ShippingAddress(
                        name="Ada Buyer",
                        line_1="1 Main Street",
                        city="Baltimore",
                        region="MD",
                        postal_code="21201",
                        country_code="US",
                    ),
                    lines=[CheckoutLine(product_id=self.product.pk, quantity=1)],
                    shipping_total=Decimal("0.00"),
                    expected_total=Decimal("9.00"),
                    inventory=EbayInventoryGateway(),
                )
                return order.pk
            except InventoryUnavailable:
                return None
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            order_ids = list(executor.map(create, (uuid.uuid4(), uuid.uuid4())))

        self.assertEqual(sum(order_id is not None for order_id in order_ids), 1)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 1)
        self.assertEqual(InventoryReservation.objects.count(), 1)

    def test_due_funding_reconciliation_cannot_cancel_inflight_capture(self):
        transition_written = Event()
        allow_transition_commit = Event()
        stale_provider_read = Event()
        capture_started = Event()
        capture_may_return = Event()

        class Inventory:
            released = False

            def commit(self, reservation):
                raise AssertionError("Committed inventory must not be committed twice")

            def release(self, reservation):
                self.released = True
                capture_may_return.set()

        expires_at = timezone.now() + timedelta(seconds=1)
        order = Order.objects.create(
            checkout_key=uuid.uuid4(),
            checkout_fingerprint="0" * 64,
            status=Order.Status.FUNDING_RETRY,
            customer_email="buyer@example.com",
            customer_name="Ada Buyer",
            shipping_line_1="1 Main Street",
            shipping_city="Baltimore",
            shipping_region="MD",
            shipping_postal_code="21201",
            shipping_country_code="US",
            subtotal=Decimal("10.00"),
            shipping_total=Decimal("0.00"),
            total=Decimal("10.00"),
            paypal_order_id="PAYPAL-RACE",
            paypal_status="INSTRUMENT_DECLINED",
            expires_at=expires_at,
        )
        item = OrderItem.objects.create(
            order=order,
            product=self.product,
            ebay_item_id=self.product.ebay_item_id,
            title=self.product.title,
            quantity=1,
            unit_price=Decimal("10.00"),
        )
        reservation = InventoryReservation.objects.create(
            order_item=item,
            quantity=1,
            status=InventoryReservation.Status.COMMITTED,
            expires_at=expires_at,
            committed_at=timezone.now(),
        )
        purchase = {
            "reference_id": order.reference,
            "custom_id": order.reference,
            "invoice_id": order.reference,
            "amount": {"currency_code": "USD", "value": "10.00"},
            "shipping": {
                "name": {"full_name": "Ada Buyer"},
                "address": {
                    "address_line_1": "1 Main Street",
                    "admin_area_2": "Baltimore",
                    "admin_area_1": "MD",
                    "postal_code": "21201",
                    "country_code": "US",
                },
            },
        }

        class CapturePayPal:
            capture_calls = 0

            def get_order(self, paypal_order_id):
                return {
                    "id": paypal_order_id,
                    "intent": "CAPTURE",
                    "status": "APPROVED",
                    "purchase_units": [purchase],
                }

            def capture_order(self, paypal_order_id, request_id):
                self.capture_calls += 1
                capture_started.set()
                if not capture_may_return.wait(10):
                    raise AssertionError("Reconciliation did not finish")
                return {
                    "id": paypal_order_id,
                    "status": "COMPLETED",
                    "purchase_units": [
                        {
                            "payments": {
                                "captures": [
                                    {
                                        "id": "CAPTURE-RACE",
                                        "status": "COMPLETED",
                                        "amount": {
                                            "currency_code": "USD",
                                            "value": "10.00",
                                        },
                                    }
                                ]
                            }
                        }
                    ],
                }

        class ReconcilePayPal:
            def get_order(self, paypal_order_id):
                stale_provider_read.set()
                if not capture_started.wait(10):
                    raise AssertionError("Capture did not reach PayPal")
                return {"id": paypal_order_id, "status": "APPROVED"}

        inventory = Inventory()
        capture_paypal = CapturePayPal()
        original_save = Order.save

        def blocking_save(instance, *args, **kwargs):
            result = original_save(instance, *args, **kwargs)
            if (
                current_thread().name == "capture-thread"
                and instance.status == Order.Status.PAYMENT_PROCESSING
            ):
                transition_written.set()
                if not allow_transition_commit.wait(10):
                    raise AssertionError("Payment transition was not released")
            return result

        def capture():
            connections.close_all()
            current_thread().name = "capture-thread"
            try:
                return capture_paypal_order(order.pk, capture_paypal, inventory)
            finally:
                connections.close_all()

        def reconcile():
            connections.close_all()
            try:
                try:
                    return reconcile_due_funding_retry(
                        order.pk, ReconcilePayPal(), inventory, timezone.now()
                    )
                except OrderStateError:
                    return None
            finally:
                capture_may_return.set()
                connections.close_all()

        with patch.object(Order, "save", blocking_save):
            with ThreadPoolExecutor(max_workers=2) as executor:
                capture_future = executor.submit(capture)
                self.assertTrue(transition_written.wait(5))
                time.sleep(
                    max((expires_at - timezone.now()).total_seconds() + 0.05, 0)
                )
                reconcile_future = executor.submit(reconcile)
                stale_provider_read.wait(0.25)
                allow_transition_commit.set()
                captured = capture_future.result(timeout=15)
                reconcile_future.result(timeout=15)

        order.refresh_from_db()
        reservation.refresh_from_db()
        self.assertEqual(captured.status, Order.Status.PAID)
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(reservation.status, InventoryReservation.Status.COMMITTED)
        self.assertFalse(inventory.released)
        self.assertEqual(capture_paypal.capture_calls, 1)
