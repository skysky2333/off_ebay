import json
import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from catalog.inventory import EbayInventoryGateway
from catalog.models import InventoryOperation, Product

from .inventory import InventoryUnavailable
from .models import InventoryReservation, Order, OrderEvent, Refund
from .paypal import PayPalClient
from .services import (
    CheckoutLine,
    IdempotencyConflict,
    PaymentDataError,
    ShippingAddress,
    WebhookVerificationError,
    capture_paypal_order,
    cancel_order,
    create_guest_order,
    create_paypal_checkout,
    expire_due_orders,
    process_paypal_webhook,
    record_manual_shipment,
    refund_order,
)


class InventorySpy:
    def __init__(self, events=None):
        self.reserved = []
        self.committed = []
        self.released = []
        self.events = events if events is not None else []

    def reserve(self, reservation):
        self.reserved.append(reservation.pk)

    def commit(self, reservation):
        self.committed.append(reservation.pk)
        self.events.append("inventory.commit")

    def release(self, reservation):
        self.released.append(reservation.pk)


class PayPalSpy:
    def __init__(self, events=None):
        self.created_payload = None
        self.capture_calls = 0
        self.refund_calls = 0
        self.signature_valid = True
        self.events = events if events is not None else []

    def create_order(self, payload, request_id):
        self.created_payload = payload
        return {"id": "PAYPAL-ORDER-1", "status": "CREATED"}

    def capture_order(self, paypal_order_id, request_id):
        self.capture_calls += 1
        self.events.append("paypal.capture")
        return {
            "id": paypal_order_id,
            "status": "COMPLETED",
            "purchase_units": [
                {
                    "payments": {
                        "captures": [
                            {
                                "id": "CAPTURE-1",
                                "status": "COMPLETED",
                                "amount": {
                                    "currency_code": "USD",
                                    "value": "15.00",
                                },
                            }
                        ]
                    }
                }
            ],
        }

    def get_order(self, paypal_order_id):
        purchase = self.created_payload["purchase_units"][0]
        return {
            "id": paypal_order_id,
            "intent": "CAPTURE",
            "status": "APPROVED",
            "purchase_units": [purchase],
        }

    def refund_capture(self, capture_id, amount, currency, invoice_id, request_id):
        self.refund_calls += 1
        return {
            "id": "REFUND-1",
            "status": "COMPLETED",
            "amount": {"currency_code": "USD", "value": "15.00"},
        }

    def verify_webhook_signature(self, headers, event):
        return self.signature_valid


class OrderServiceTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(
            ebay_item_id="123456789",
            slug="test-item",
            title="Test item",
            price=Decimal("6.00"),
            currency="USD",
            listing_url="https://www.ebay.com/itm/123456789",
            listing_type="FixedPriceItem",
            quantity=5,
            last_synced_at=timezone.now(),
        )
        self.address = ShippingAddress(
            name="Ada Buyer",
            line_1="1 Main Street",
            city="Baltimore",
            region="MD",
            postal_code="21201",
            country_code="US",
        )
        self.line = CheckoutLine(
            product_id=self.product.pk,
            quantity=2,
        )
        self.inventory = InventorySpy()

    def create_order(self, checkout_key=None):
        return create_guest_order(
            checkout_key=checkout_key or uuid.uuid4(),
            email="ada@example.com",
            address=self.address,
            lines=[self.line],
            shipping_total=Decimal("3.00"),
            inventory=self.inventory,
        )

    def test_create_is_idempotent_and_snapshots_are_immutable(self):
        checkout_key = uuid.uuid4()
        order = self.create_order(checkout_key)
        duplicate = self.create_order(checkout_key)

        self.assertEqual(duplicate.pk, order.pk)
        self.assertEqual(order.total, Decimal("15.00"))
        self.assertEqual(len(self.inventory.reserved), 1)
        item = order.items.get()
        item.title = "Changed"
        with self.assertRaises(ValidationError):
            item.save()

    def test_reused_checkout_key_with_other_payload_is_rejected(self):
        checkout_key = uuid.uuid4()
        self.create_order(checkout_key)
        other_line = CheckoutLine(
            product_id=self.product.pk,
            quantity=1,
        )
        with self.assertRaises(IdempotencyConflict):
            create_guest_order(
                checkout_key=checkout_key,
                email="ada@example.com",
                address=self.address,
                lines=[other_line],
                shipping_total=Decimal("3.00"),
                inventory=self.inventory,
            )

    def test_checkout_uses_current_catalog_price(self):
        Product.objects.filter(pk=self.product.pk).update(price=Decimal("8.00"))

        order = self.create_order()

        self.assertEqual(order.subtotal, Decimal("16.00"))
        self.assertEqual(order.total, Decimal("19.00"))

    def test_local_reservations_prevent_overbooking(self):
        line = CheckoutLine(product_id=self.product.pk, quantity=3)
        create_guest_order(
            checkout_key=uuid.uuid4(),
            email="first@example.com",
            address=self.address,
            lines=[line],
            shipping_total=Decimal("3.00"),
            inventory=EbayInventoryGateway(),
        )
        reservation = Order.objects.get().items.get().reservation
        reservation.status = InventoryReservation.Status.COMMITTING
        reservation.save(update_fields=("status", "updated_at"))

        with self.assertRaises(InventoryUnavailable):
            create_guest_order(
                checkout_key=uuid.uuid4(),
                email="second@example.com",
                address=self.address,
                lines=[line],
                shipping_total=Decimal("3.00"),
                inventory=EbayInventoryGateway(),
            )

        self.assertEqual(Order.objects.count(), 1)

    def test_non_us_destination_is_rejected_before_inventory_reservation(self):
        address = ShippingAddress(
            name="Ada Buyer",
            line_1="1 Main Street",
            city="Toronto",
            region="ON",
            postal_code="M5V 3A8",
            country_code="CA",
        )
        with self.assertRaisesMessage(ValueError, "Only United States"):
            create_guest_order(
                checkout_key=uuid.uuid4(),
                email="ada@example.com",
                address=address,
                lines=[self.line],
                shipping_total=Decimal("3.00"),
                inventory=self.inventory,
            )
        self.assertEqual(self.inventory.reserved, [])

    def test_paypal_create_and_capture_are_idempotent(self):
        order = self.create_order()
        events = []
        self.inventory.events = events
        paypal = PayPalSpy(events)

        create_paypal_checkout(
            order.pk,
            "https://store.test/return",
            "https://store.test/cancel",
            paypal,
        )
        payload = paypal.created_payload
        purchase = payload["purchase_units"][0]
        self.assertEqual(purchase["invoice_id"], order.reference)
        self.assertEqual(purchase["amount"]["value"], "15.00")
        self.assertEqual(purchase["items"][0]["quantity"], "2")
        self.assertEqual(purchase["shipping"]["address"]["postal_code"], "21201")

        capture_paypal_order(order.pk, paypal, self.inventory)
        capture_paypal_order(order.pk, paypal, self.inventory)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.paypal_capture_id, "CAPTURE-1")
        self.assertEqual(paypal.capture_calls, 1)
        self.assertEqual(len(self.inventory.committed), 1)
        self.assertEqual(events, ["inventory.commit", "paypal.capture"])

    def test_capture_revalidates_us_destination_before_paypal(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        Order.objects.filter(pk=order.pk).update(shipping_country_code="CA")

        with self.assertRaisesMessage(ValueError, "Only United States"):
            capture_paypal_order(order.pk, paypal, self.inventory)
        self.assertEqual(paypal.capture_calls, 0)
        self.assertEqual(self.inventory.committed, [])

    def test_duplicate_capture_webhook_does_not_commit_twice(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)
        event = {
            "id": "WH-1",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "CAPTURE-1",
                "status": "COMPLETED",
                "amount": {"currency_code": "USD", "value": "15.00"},
                "supplementary_data": {
                    "related_ids": {"order_id": "PAYPAL-ORDER-1"}
                },
            },
        }
        process_paypal_webhook({}, event, paypal, self.inventory)
        process_paypal_webhook({}, event, paypal, self.inventory)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(len(self.inventory.committed), 1)
        self.assertEqual(
            OrderEvent.objects.filter(event_key="paypal-webhook:WH-1").count(), 1
        )

    def test_capture_webhook_never_decrements_uncommitted_inventory(self):
        order = self.create_order()
        order.paypal_order_id = "PAYPAL-ORDER-1"
        order.save(update_fields=("paypal_order_id", "updated_at"))
        event = {
            "id": "WH-EARLY",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "CAPTURE-1",
                "status": "COMPLETED",
                "amount": {"currency_code": "USD", "value": "15.00"},
                "supplementary_data": {
                    "related_ids": {"order_id": "PAYPAL-ORDER-1"}
                },
            },
        }

        with self.assertRaisesMessage(PaymentDataError, "committed inventory"):
            process_paypal_webhook({}, event, PayPalSpy(), self.inventory)

        order.refresh_from_db()
        self.assertIsNone(order.paid_at)
        self.assertEqual(self.inventory.committed, [])

    def test_invalid_webhook_signature_does_not_change_order(self):
        order = self.create_order()
        paypal = PayPalSpy()
        paypal.signature_valid = False
        with self.assertRaises(WebhookVerificationError):
            process_paypal_webhook(
                {},
                {
                    "id": "WH-BAD",
                    "event_type": "CHECKOUT.ORDER.APPROVED",
                    "resource": {"id": "PAYPAL-ORDER-1"},
                },
                paypal,
                self.inventory,
            )
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.AWAITING_PAYMENT)

    def test_expiration_releases_inventory_once(self):
        order = self.create_order()
        now = timezone.now()
        order.expires_at = now - timedelta(seconds=1)
        order.save(update_fields=("expires_at", "updated_at"))

        self.assertEqual(expire_due_orders(self.inventory, now), 1)
        self.assertEqual(expire_due_orders(self.inventory, now), 0)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.EXPIRED)
        self.assertEqual(len(self.inventory.released), 1)
        self.assertEqual(
            order.items.get().reservation.status,
            InventoryReservation.Status.RELEASED,
        )

    def test_cancellation_resolves_an_in_progress_commit_before_release(self):
        order = self.create_order()
        reservation = order.items.get().reservation
        reservation.status = InventoryReservation.Status.COMMITTING
        reservation.save(update_fields=("status", "updated_at"))

        cancel_order(order.pk, self.inventory, capture_definitely_absent=True)

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, InventoryReservation.Status.RELEASED)
        self.assertEqual(self.inventory.events, ["inventory.commit"])
        self.assertEqual(self.inventory.released, [reservation.pk])

    def test_cancelled_order_can_retry_an_interrupted_release(self):
        order = self.create_order()

        class FailingReleaseInventory(InventorySpy):
            def __init__(self):
                super().__init__()
                self.fail_once = True

            def release(self, reservation):
                if self.fail_once:
                    self.fail_once = False
                    raise RuntimeError("release interrupted")
                super().release(reservation)

        inventory = FailingReleaseInventory()
        with self.assertRaisesMessage(RuntimeError, "release interrupted"):
            cancel_order(order.pk, inventory)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertEqual(
            order.items.get().reservation.status,
            InventoryReservation.Status.RELEASING,
        )

        cancel_order(order.pk, inventory, capture_definitely_absent=True)

        self.assertEqual(
            order.items.get().reservation.status,
            InventoryReservation.Status.RELEASED,
        )

    def test_gateway_replays_the_original_plan_after_a_lost_response(self):
        order = self.create_order()
        reservation = order.items.get().reservation

        class LostResponseClient:
            quantity = 5
            revise_calls = 0

            def __enter__(self):
                return self

            def __exit__(self, exception_type, exception, traceback):
                return False

            def get_item(self, item_id):
                return SimpleNamespace(item_id=item_id, quantity=type(self).quantity, variations=())

            def revise_inventory_status(self, item_id, quantity, message_id, sku=""):
                type(self).revise_calls += 1
                type(self).quantity = quantity
                if type(self).revise_calls == 1:
                    raise RuntimeError("response lost")
                return quantity

        gateway = EbayInventoryGateway()
        with patch("catalog.inventory.EbayTradingClient", LostResponseClient):
            with self.assertRaisesMessage(RuntimeError, "response lost"):
                gateway.commit(reservation)
            gateway.commit(reservation)

        operation = InventoryOperation.objects.get(
            idempotency_key=f"sale-{reservation.pk}"
        )
        self.assertEqual(operation.expected_quantity, 5)
        self.assertEqual(operation.requested_quantity, 3)
        self.assertEqual(operation.status, InventoryOperation.Status.SUCCEEDED)
        self.assertEqual(LostResponseClient.revise_calls, 1)

    def test_gateway_replays_a_lost_release_without_adding_twice(self):
        order = self.create_order()
        reservation = order.items.get().reservation
        reservation.status = InventoryReservation.Status.RELEASING
        reservation.save(update_fields=("status", "updated_at"))
        InventoryOperation.objects.create(
            idempotency_key=f"sale-{reservation.pk}",
            product=self.product,
            reason=InventoryOperation.Reason.SALE,
            expected_quantity=5,
            requested_quantity=3,
            verified_quantity=3,
            status=InventoryOperation.Status.SUCCEEDED,
        )

        class LostReleaseClient:
            quantity = 3
            revise_calls = 0

            def __enter__(self):
                return self

            def __exit__(self, exception_type, exception, traceback):
                return False

            def get_item(self, item_id):
                return SimpleNamespace(item_id=item_id, quantity=type(self).quantity, variations=())

            def revise_inventory_status(self, item_id, quantity, message_id, sku=""):
                type(self).revise_calls += 1
                type(self).quantity = quantity
                if type(self).revise_calls == 1:
                    raise RuntimeError("release response lost")
                return quantity

        gateway = EbayInventoryGateway()
        with patch("catalog.inventory.EbayTradingClient", LostReleaseClient):
            with self.assertRaisesMessage(RuntimeError, "release response lost"):
                gateway.release(reservation)
            gateway.release(reservation)

        operation = InventoryOperation.objects.get(
            idempotency_key=f"release-{reservation.pk}"
        )
        self.assertEqual(operation.expected_quantity, 3)
        self.assertEqual(operation.requested_quantity, 5)
        self.assertEqual(operation.status, InventoryOperation.Status.SUCCEEDED)
        self.assertEqual(LostReleaseClient.revise_calls, 1)

    def test_full_refund_is_idempotent(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)

        refund_order(order.pk, paypal)
        refund_order(order.pk, paypal)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.REFUNDED)
        self.assertEqual(order.refunded_total, order.total)
        self.assertEqual(paypal.refund_calls, 1)

    def test_older_partial_refund_webhook_is_never_counted_twice(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)

        def event(event_id, refund_id, value):
            return {
                "id": event_id,
                "event_type": "PAYMENT.CAPTURE.REFUNDED",
                "resource": {
                    "id": refund_id,
                    "amount": {"currency_code": "USD", "value": value},
                    "supplementary_data": {
                        "related_ids": {"capture_id": "CAPTURE-1"}
                    },
                },
            }

        process_paypal_webhook({}, event("WH-R1", "REFUND-1", "5.00"), paypal, self.inventory)
        process_paypal_webhook({}, event("WH-R2", "REFUND-2", "4.00"), paypal, self.inventory)
        process_paypal_webhook({}, event("WH-R1-LATE", "REFUND-1", "5.00"), paypal, self.inventory)

        order.refresh_from_db()
        self.assertEqual(order.refunded_total, Decimal("9.00"))
        self.assertEqual(Refund.objects.filter(order=order).count(), 2)

    def test_manual_shipment_updates_order_and_event_once(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)

        first = record_manual_shipment(order.pk, "USPS", "940000000", "shipped")
        duplicate = record_manual_shipment(
            order.pk, "USPS", "940000000", "shipped"
        )
        order.refresh_from_db()
        self.assertEqual(first.pk, duplicate.pk)
        self.assertEqual(order.status, Order.Status.SHIPPED)
        self.assertIsNotNone(order.shipped_at)
        self.assertEqual(
            order.events.filter(kind="shipment.recorded").count(), 1
        )


class PayPalClientTests(TestCase):
    def test_orders_refunds_and_signature_use_paypal_api(self):
        requests = []

        def handler(request):
            requests.append(request)
            path = request.url.path
            if path == "/v1/oauth2/token":
                return httpx.Response(200, json={"access_token": "TOKEN"})
            if path == "/v2/checkout/orders" and request.method == "POST":
                return httpx.Response(201, json={"id": "ORDER", "status": "CREATED"})
            if path == "/v2/checkout/orders/ORDER" and request.method == "GET":
                return httpx.Response(200, json={"id": "ORDER", "status": "APPROVED"})
            if path == "/v2/checkout/orders/ORDER/capture":
                return httpx.Response(201, json={"id": "ORDER", "status": "COMPLETED"})
            if path == "/v2/payments/captures/CAPTURE/refund":
                return httpx.Response(201, json={"id": "REFUND", "status": "COMPLETED"})
            if path == "/v1/notifications/verify-webhook-signature":
                return httpx.Response(200, json={"verification_status": "SUCCESS"})
            return httpx.Response(404)

        http = httpx.Client(transport=httpx.MockTransport(handler))
        client = PayPalClient(
            client_id="CLIENT",
            client_secret="SECRET",
            webhook_id="WEBHOOK",
            base_url="https://api-m.sandbox.paypal.com",
            http_client=http,
        )
        self.assertEqual(client.create_order({"intent": "CAPTURE"}, "CREATE")["id"], "ORDER")
        self.assertEqual(client.get_order("ORDER")["status"], "APPROVED")
        self.assertEqual(client.capture_order("ORDER", "CAPTURE")["status"], "COMPLETED")
        self.assertEqual(
            client.refund_capture("CAPTURE", "1.00", "USD", "FM-1", "REFUND")["id"],
            "REFUND",
        )
        headers = {
            "PAYPAL-AUTH-ALGO": "SHA256withRSA",
            "PAYPAL-CERT-URL": "https://paypal.test/cert",
            "PAYPAL-TRANSMISSION-ID": "TRANSMISSION",
            "PAYPAL-TRANSMISSION-SIG": "SIGNATURE",
            "PAYPAL-TRANSMISSION-TIME": "2026-01-01T00:00:00Z",
        }
        self.assertTrue(client.verify_webhook_signature(headers, {"id": "WH-1"}))
        verification_request = requests[-1]
        body = json.loads(verification_request.content)
        self.assertEqual(body["webhook_id"], "WEBHOOK")
        self.assertEqual(verification_request.headers["authorization"], "Bearer TOKEN")
