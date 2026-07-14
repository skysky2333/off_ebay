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

from catalog.ebay import EbayInventoryConflict
from catalog.inventory import EbayInventoryGateway
from catalog.models import InventoryOperation, Product

from .inventory import InventoryUnavailable
from .models import InventoryReservation, Order, OrderEvent, PayPalCase, Refund, Shipment
from .paypal import PayPalClient, PayPalInstrumentDeclined, PayPalRefundError
from .services import (
    CheckoutLine,
    IdempotencyConflict,
    OrderStateError,
    PaymentDataError,
    ShippingAddress,
    WebhookVerificationError,
    capture_paypal_order,
    cancel_order,
    create_guest_order,
    create_paypal_checkout,
    expire_due_orders,
    orders_needing_paypal_tracking,
    reconcile_due_funding_retry,
    reconcile_pending_refund,
    reconcile_paypal_tracking,
    _refund_capture_id,
    _shipment_status,
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
        self.capture_status = "COMPLETED"
        self.order_status = "APPROVED"
        self.instrument_declined = False
        self.refund_status = "COMPLETED"
        self.refund_amount = None
        self.trackers = {}
        self.events = events if events is not None else []

    def create_order(self, payload, request_id):
        self.created_payload = payload
        return {"id": "PAYPAL-ORDER-1", "status": "CREATED"}

    def capture_order(self, paypal_order_id, request_id):
        self.capture_calls += 1
        self.events.append("paypal.capture")
        if self.instrument_declined:
            raise PayPalInstrumentDeclined("PayPal declined the selected funding source.")
        self.order_status = "COMPLETED"
        amount = self.created_payload["purchase_units"][0]["amount"]
        return {
            "id": paypal_order_id,
            "status": "COMPLETED",
            "purchase_units": [
                {
                    "payments": {
                        "captures": [
                            {
                                "id": "CAPTURE-1",
                                "status": self.capture_status,
                                "amount": {
                                    "currency_code": "USD",
                                    "value": amount["value"],
                                },
                            }
                        ]
                    }
                }
            ],
        }

    def get_order(self, paypal_order_id):
        purchase = self.created_payload["purchase_units"][0]
        if self.trackers:
            purchase = {
                **purchase,
                "shipping": {
                    **purchase["shipping"],
                    "trackers": [{"id": tracker_id} for tracker_id in self.trackers],
                },
            }
        if self.order_status == "COMPLETED":
            purchase = {
                **purchase,
                "payments": {
                    "captures": [
                        {
                            "id": "CAPTURE-1",
                            "status": self.capture_status,
                            "amount": {
                                "currency_code": "USD",
                                "value": purchase["amount"]["value"],
                            },
                        }
                    ]
                },
            }
        return {
            "id": paypal_order_id,
            "intent": "CAPTURE",
            "status": self.order_status,
            "purchase_units": [purchase],
        }

    def get_tracker(self, tracker_id):
        return self.trackers[tracker_id]

    def refund_capture(self, capture_id, amount, currency, invoice_id, request_id):
        self.refund_calls += 1
        self.refund_amount = amount
        return {
            "id": f"REFUND-{self.refund_calls}",
            "status": self.refund_status,
            "amount": {"currency_code": "USD", "value": self.refund_amount},
        }

    def get_refund(self, refund_id):
        return {
            "id": refund_id,
            "status": self.refund_status,
            "amount": {"currency_code": "USD", "value": self.refund_amount},
            "links": [
                {
                    "rel": "up",
                    "href": "https://api-m.paypal.com/v2/payments/captures/CAPTURE-1",
                }
            ],
        }

    def verify_webhook_signature(self, headers, event):
        return self.signature_valid


def ebay_listing(
    product, price, currency, quantity, variations=(), volume_discounts=()
):
    return SimpleNamespace(
        item_id=product.ebay_item_id,
        title=product.title,
        description=product.description,
        price=price,
        currency=currency,
        condition=product.condition,
        category_id=product.category_id,
        category_name=product.category_name,
        item_specifics=product.item_specifics,
        shipping=product.shipping,
        listing_url=product.listing_url,
        listing_type=product.listing_type,
        listing_status="Active",
        quantity=quantity,
        started_at=product.ebay_started_at,
        ends_at=product.ebay_ends_at,
        images=(),
        variations=variations,
        volume_discounts=volume_discounts,
    )


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

    def create_order(self, checkout_key=None, expected_total=Decimal("13.80")):
        return create_guest_order(
            checkout_key=checkout_key or uuid.uuid4(),
            email="ada@example.com",
            address=self.address,
            lines=[self.line],
            shipping_total=Decimal("3.00"),
            expected_total=expected_total,
            inventory=self.inventory,
        )

    def test_create_is_idempotent_and_snapshots_are_immutable(self):
        checkout_key = uuid.uuid4()
        order = self.create_order(checkout_key)
        duplicate = self.create_order(checkout_key)

        self.assertEqual(duplicate.pk, order.pk)
        self.assertEqual(order.total, Decimal("13.80"))
        self.assertEqual(order.items.get().unit_price, Decimal("5.40"))
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
                expected_total=Decimal("9.00"),
                inventory=self.inventory,
            )

    def test_checkout_uses_current_catalog_price(self):
        Product.objects.filter(pk=self.product.pk).update(price=Decimal("8.00"))

        order = self.create_order(expected_total=Decimal("17.40"))

        self.assertEqual(order.subtotal, Decimal("14.40"))
        self.assertEqual(order.total, Decimal("17.40"))

    def test_checkout_applies_the_qualifying_volume_discount(self):
        self.product.volume_discounts = [
            {"min_quantity": 2, "percent_off": "10"}
        ]
        self.product.save(update_fields=("volume_discounts", "updated_at"))

        order = self.create_order(expected_total=Decimal("12.72"))

        self.assertEqual(order.items.get().unit_price, Decimal("4.86"))
        self.assertEqual(order.subtotal, Decimal("9.72"))

    def test_checkout_combines_variants_for_volume_pricing(self):
        self.product.volume_discounts = [
            {"min_quantity": 2, "percent_off": "5"}
        ]
        self.product.save(update_fields=("volume_discounts", "updated_at"))
        first = self.product.variants.create(
            source_key="first",
            sku="FIRST",
            title="First option",
            price=Decimal("6.00"),
            quantity=2,
        )
        second = self.product.variants.create(
            source_key="second",
            sku="SECOND",
            title="Second option",
            price=Decimal("8.00"),
            quantity=2,
        )
        lines = [
            CheckoutLine(
                product_id=self.product.pk, variant_id=first.pk, quantity=1
            ),
            CheckoutLine(
                product_id=self.product.pk, variant_id=second.pk, quantity=1
            ),
        ]

        order = create_guest_order(
            checkout_key=uuid.uuid4(),
            email="ada@example.com",
            address=self.address,
            lines=lines,
            shipping_total=Decimal("3.00"),
            expected_total=Decimal("14.97"),
            inventory=self.inventory,
        )

        prices = {
            item.variant_id: item.unit_price for item in order.items.all()
        }
        self.assertEqual(
            prices, {first.pk: Decimal("5.13"), second.pk: Decimal("6.84")}
        )
        self.assertEqual(order.subtotal, Decimal("11.97"))

    def test_local_reservations_prevent_overbooking(self):
        line = CheckoutLine(product_id=self.product.pk, quantity=3)
        create_guest_order(
            checkout_key=uuid.uuid4(),
            email="first@example.com",
            address=self.address,
            lines=[line],
            shipping_total=Decimal("3.00"),
            expected_total=Decimal("19.20"),
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
                expected_total=Decimal("19.20"),
                inventory=EbayInventoryGateway(),
            )

    def test_reservation_error_reports_unreserved_quantity(self):
        Product.objects.filter(pk=self.product.pk).update(quantity=1)
        line = CheckoutLine(product_id=self.product.pk, quantity=1)
        create_guest_order(
            checkout_key=uuid.uuid4(),
            email="first@example.com",
            address=self.address,
            lines=[line],
            shipping_total=Decimal("3.00"),
            expected_total=Decimal("8.40"),
            inventory=EbayInventoryGateway(),
        )

        with self.assertRaisesMessage(InventoryUnavailable, "Only 0"):
            create_guest_order(
                checkout_key=uuid.uuid4(),
                email="second@example.com",
                address=self.address,
                lines=[line],
                shipping_total=Decimal("3.00"),
                expected_total=Decimal("8.40"),
                inventory=EbayInventoryGateway(),
            )

    def test_commit_honors_local_checkout_exclusion(self):
        order = self.create_order()
        Product.objects.filter(pk=self.product.pk).update(checkout_excluded=True)

        with self.assertRaisesMessage(InventoryUnavailable, "no longer available"):
            EbayInventoryGateway().commit(order.items.get().reservation)

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
                expected_total=Decimal("15.00"),
                inventory=self.inventory,
            )
        self.assertEqual(self.inventory.reserved, [])

    def test_changed_total_is_rejected_before_inventory_reservation(self):
        with self.assertRaisesMessage(InventoryUnavailable, "order total changed"):
            self.create_order(expected_total=Decimal("14.00"))

        self.assertEqual(Order.objects.count(), 0)
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
        self.assertEqual(purchase["amount"]["value"], "13.80")
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

    def test_capture_rejects_changed_paypal_recipient_and_added_address_line(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        shipping = paypal.created_payload["purchase_units"][0]["shipping"]
        for changed_field in ("name", "address_line_2"):
            with self.subTest(changed_field=changed_field):
                if changed_field == "name":
                    shipping["name"]["full_name"] = "Other Recipient"
                else:
                    shipping["address"]["address_line_2"] = "Apt 999"

                with self.assertRaisesMessage(PaymentDataError, "does not match"):
                    capture_paypal_order(order.pk, paypal, self.inventory)
                shipping["name"]["full_name"] = order.customer_name
                shipping["address"].pop("address_line_2", None)

        paypal.created_payload["purchase_units"][0]["amount"]["value"] = "NaN"
        with self.assertRaisesMessage(PaymentDataError, "amount is invalid"):
            capture_paypal_order(order.pk, paypal, self.inventory)

    def test_funding_decline_can_retry_then_complete_without_recommitting_inventory(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        order.refresh_from_db()
        paypal.instrument_declined = True

        with self.assertRaises(PayPalInstrumentDeclined):
            capture_paypal_order(order.pk, paypal, self.inventory)

        order.refresh_from_db()
        first_deadline = order.expires_at
        self.assertEqual(order.status, Order.Status.FUNDING_RETRY)
        self.assertEqual(order.paypal_status, "INSTRUMENT_DECLINED")
        self.assertGreater(first_deadline, timezone.now())
        self.assertEqual(len(self.inventory.committed), 1)

        paypal.instrument_declined = False
        result = capture_paypal_order(order.pk, paypal, self.inventory)

        self.assertEqual(result.status, Order.Status.PAID)
        self.assertEqual(len(self.inventory.committed), 1)

    def test_due_funding_decline_expires_and_releases_committed_inventory(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        paypal.instrument_declined = True
        with self.assertRaises(PayPalInstrumentDeclined):
            capture_paypal_order(order.pk, paypal, self.inventory)
        due = timezone.now()
        Order.objects.filter(pk=order.pk).update(expires_at=due - timedelta(seconds=1))

        result = reconcile_due_funding_retry(
            order.pk, paypal, self.inventory, due
        )

        self.assertEqual(result.status, Order.Status.EXPIRED)
        self.assertEqual(result.paypal_status, "APPROVED")
        self.assertEqual(paypal.capture_calls, 1)
        self.assertEqual(len(self.inventory.released), 1)

    def test_late_approval_for_expired_order_is_acknowledged_once(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        order.refresh_from_db()
        Order.objects.filter(pk=order.pk).update(status=Order.Status.EXPIRED)
        InventoryReservation.objects.filter(order_item__order=order).update(
            status=InventoryReservation.Status.RELEASED
        )
        event = {
            "id": "WH-LATE-APPROVAL",
            "event_type": "CHECKOUT.ORDER.APPROVED",
            "resource": {"id": order.paypal_order_id},
        }

        first = process_paypal_webhook({}, event, paypal, self.inventory)
        second = process_paypal_webhook({}, event, paypal, self.inventory)

        self.assertEqual(first.pk, order.pk)
        self.assertEqual(second.pk, order.pk)
        self.assertEqual(paypal.capture_calls, 0)
        self.assertEqual(
            OrderEvent.objects.filter(
                event_key="paypal-webhook:WH-LATE-APPROVAL"
            ).count(),
            1,
        )

    def test_pending_capture_keeps_committed_inventory_for_reconciliation(self):
        order = self.create_order()
        paypal = PayPalSpy()
        paypal.capture_status = "PENDING"
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)

        result = capture_paypal_order(order.pk, paypal, self.inventory)

        result.refresh_from_db()
        self.assertEqual(result.status, Order.Status.CAPTURE_PENDING)
        self.assertEqual(result.paypal_capture_id, "CAPTURE-1")
        self.assertEqual(result.paypal_status, "PENDING")
        self.assertIsNone(result.paid_at)
        self.assertEqual(len(self.inventory.committed), 1)
        self.assertEqual(self.inventory.released, [])

    def test_refund_before_capture_completion_stays_in_payment_reconciliation(self):
        order = self.create_order()
        paypal = PayPalSpy()
        paypal.capture_status = "PENDING"
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)

        process_paypal_webhook(
            {},
            {
                "id": "WH-EARLY-REFUND",
                "event_type": "PAYMENT.CAPTURE.REFUNDED",
                "resource": {
                    "id": "REFUND-EARLY",
                    "amount": {"currency_code": "USD", "value": "13.80"},
                    "links": [
                        {
                            "rel": "up",
                            "href": "https://api-m.paypal.com/v2/payments/captures/CAPTURE-1",
                        }
                    ],
                },
            },
            paypal,
            self.inventory,
        )

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CAPTURE_PENDING)
        self.assertIsNone(order.paid_at)
        self.assertEqual(order.refunded_total, order.total)

        paypal.order_status = "COMPLETED"
        paypal.capture_status = "REFUNDED"
        capture_paypal_order(order.pk, paypal, self.inventory)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.REFUNDED)
        self.assertIsNotNone(order.paid_at)

    def test_refunded_capture_waits_for_refund_webhook_details(self):
        order = self.create_order()
        paypal = PayPalSpy()
        paypal.capture_status = "PENDING"
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)
        paypal.order_status = "COMPLETED"
        paypal.capture_status = "REFUNDED"

        with self.assertRaisesMessage(PaymentDataError, "refund details"):
            capture_paypal_order(order.pk, paypal, self.inventory)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CAPTURE_PENDING)
        self.assertIsNone(order.paid_at)

    def test_declined_capture_restores_inventory_and_cancels_order(self):
        order = self.create_order()
        paypal = PayPalSpy()
        paypal.capture_status = "DECLINED"
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)

        result = capture_paypal_order(order.pk, paypal, self.inventory)

        result.refresh_from_db()
        self.assertEqual(result.status, Order.Status.CANCELLED)
        self.assertEqual(result.paypal_capture_id, "CAPTURE-1")
        self.assertEqual(result.paypal_status, "DECLINED")
        self.assertIsNone(result.paid_at)
        self.assertEqual(len(self.inventory.committed), 1)
        self.assertEqual(len(self.inventory.released), 1)
        self.assertEqual(
            result.items.get().reservation.status,
            InventoryReservation.Status.RELEASED,
        )

    def test_declined_capture_webhook_resolves_pending_payment(self):
        order = self.create_order()
        paypal = PayPalSpy()
        paypal.capture_status = "PENDING"
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)

        process_paypal_webhook(
            {},
            {
                "id": "WH-CAPTURE-DECLINED",
                "event_type": "PAYMENT.CAPTURE.DECLINED",
                "resource": {
                    "id": "CAPTURE-1",
                    "status": "DECLINED",
                    "amount": {"currency_code": "USD", "value": "13.80"},
                    "supplementary_data": {
                        "related_ids": {"order_id": "PAYPAL-ORDER-1"}
                    },
                },
            },
            paypal,
            self.inventory,
        )

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertEqual(order.paypal_status, "DECLINED")
        self.assertEqual(len(self.inventory.released), 1)

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
                "amount": {"currency_code": "USD", "value": "13.80"},
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

    def test_dispute_webhooks_reopen_review_and_ignore_stale_updates(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)
        order.refresh_from_db()
        committed_count = len(self.inventory.committed)
        resource = {
            "dispute_id": "PP-D-STORE-1",
            "disputed_transactions": [
                {"seller_transaction_id": order.paypal_capture_id}
            ],
            "reason": "ITEM_NOT_RECEIVED",
            "status": "WAITING_FOR_SELLER_RESPONSE",
            "dispute_amount": {"currency_code": "USD", "value": "5.00"},
            "dispute_life_cycle_stage": "INQUIRY",
            "dispute_channel": "INTERNAL",
            "seller_response_due_date": "2026-07-20T12:00:00Z",
            "create_time": "2026-07-14T00:00:00Z",
            "update_time": "2026-07-14T01:00:00Z",
        }

        process_paypal_webhook(
            {},
            {
                "id": "WH-DISPUTE-CREATED",
                "event_type": "CUSTOMER.DISPUTE.CREATED",
                "resource": resource,
            },
            paypal,
            self.inventory,
        )

        case = order.paypal_cases.get()
        self.assertEqual(case.kind, PayPalCase.Kind.DISPUTE)
        self.assertEqual(case.status, PayPalCase.Status.WAITING_FOR_SELLER_RESPONSE)
        self.assertEqual(case.reason, "ITEM_NOT_RECEIVED")
        self.assertEqual(case.amount, Decimal("5.00"))
        self.assertTrue(case.needs_review)
        self.assertEqual(
            case.seller_response_due_at.isoformat(),
            "2026-07-20T12:00:00+00:00",
        )

        case.needs_review = False
        case.reviewed_at = timezone.now()
        case.save(update_fields=("needs_review", "reviewed_at", "updated_at"))
        process_paypal_webhook(
            {},
            {
                "id": "WH-DISPUTE-UPDATED",
                "event_type": "CUSTOMER.DISPUTE.UPDATED",
                "resource": {
                    **resource,
                    "status": "UNDER_REVIEW",
                    "update_time": "2026-07-14T02:00:00Z",
                },
            },
            paypal,
            self.inventory,
        )

        case.refresh_from_db()
        self.assertEqual(case.status, PayPalCase.Status.UNDER_REVIEW)
        self.assertTrue(case.needs_review)
        self.assertIsNone(case.reviewed_at)

        process_paypal_webhook(
            {},
            {
                "id": "WH-DISPUTE-STALE",
                "event_type": "CUSTOMER.DISPUTE.UPDATED",
                "resource": {
                    **resource,
                    "status": "OPEN",
                    "update_time": "2026-07-14T00:30:00Z",
                },
            },
            paypal,
            self.inventory,
        )
        case.refresh_from_db()
        self.assertEqual(case.status, PayPalCase.Status.UNDER_REVIEW)

        resolved_event = {
            "id": "WH-DISPUTE-RESOLVED",
            "event_type": "CUSTOMER.DISPUTE.RESOLVED",
            "resource": {
                **resource,
                "status": "RESOLVED",
                "update_time": "2026-07-14T03:00:00Z",
                "dispute_outcome": {
                    "outcome_code": "RESOLVED_SELLER_FAVOUR"
                },
            },
        }
        process_paypal_webhook({}, resolved_event, paypal, self.inventory)
        process_paypal_webhook({}, resolved_event, paypal, self.inventory)

        case.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(order.paypal_cases.count(), 1)
        self.assertEqual(case.status, PayPalCase.Status.RESOLVED)
        self.assertEqual(case.outcome, "RESOLVED_SELLER_FAVOUR")
        self.assertTrue(case.needs_review)
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.refunded_total, Decimal("0.00"))
        self.assertEqual(len(self.inventory.committed), committed_count)
        self.assertEqual(self.inventory.released, [])
        self.assertEqual(
            order.events.filter(kind__startswith="CUSTOMER.DISPUTE.").count(),
            4,
        )

    def test_unrelated_paypal_dispute_is_ignored(self):
        result = process_paypal_webhook(
            {},
            {
                "id": "WH-OTHER-DISPUTE",
                "event_type": "CUSTOMER.DISPUTE.CREATED",
                "resource": {
                    "dispute_id": "PP-D-OTHER",
                    "disputed_transactions": [
                        {"seller_transaction_id": "NON-STORE-CAPTURE"}
                    ],
                    "reason": "ITEM_NOT_RECEIVED",
                    "status": "OPEN",
                    "dispute_amount": {
                        "currency_code": "USD",
                        "value": "10.00",
                    },
                    "dispute_life_cycle_stage": "INQUIRY",
                    "create_time": "2026-07-14T00:00:00Z",
                },
            },
            PayPalSpy(),
            self.inventory,
        )

        self.assertIsNone(result)
        self.assertFalse(PayPalCase.objects.exists())
        self.assertFalse(
            OrderEvent.objects.filter(
                event_key="paypal-webhook:WH-OTHER-DISPUTE"
            ).exists()
        )

    def test_capture_reversal_creates_review_case_without_financial_side_effects(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)
        order.refresh_from_db()
        event = {
            "id": "WH-CAPTURE-REVERSED",
            "event_type": "PAYMENT.CAPTURE.REVERSED",
            "resource": {
                "id": order.paypal_capture_id,
                "amount": {"currency_code": "USD", "value": "13.80"},
                "status_details": {"reason": "CHARGEBACK"},
                "create_time": "2026-07-14T00:00:00Z",
                "update_time": "2026-07-14T04:00:00Z",
            },
        }

        process_paypal_webhook({}, event, paypal, self.inventory)
        process_paypal_webhook({}, event, paypal, self.inventory)

        case = order.paypal_cases.get()
        order.refresh_from_db()
        self.assertEqual(case.kind, PayPalCase.Kind.REVERSAL)
        self.assertEqual(case.status, PayPalCase.Status.REVERSED)
        self.assertEqual(case.reason, "CHARGEBACK")
        self.assertEqual(case.amount, order.total)
        self.assertTrue(case.needs_review)
        self.assertEqual(order.paypal_status, "REVERSED")
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.refunded_total, Decimal("0.00"))
        self.assertEqual(order.refunds.count(), 0)
        self.assertEqual(order.paypal_cases.count(), 1)
        self.assertEqual(len(self.inventory.committed), 1)
        self.assertEqual(self.inventory.released, [])

        with self.assertRaisesMessage(OrderStateError, "PayPal case"):
            refund_order(order.pk, paypal)
        with self.assertRaisesMessage(OrderStateError, "PayPal case"):
            record_manual_shipment(order.pk, "USPS", "BLOCKED-TRACKING")
        self.assertEqual(paypal.refund_calls, 0)
        self.assertFalse(order.shipments.exists())

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
                "amount": {"currency_code": "USD", "value": "13.80"},
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

    def test_approval_webhook_acknowledges_a_handled_funding_decline(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        order.refresh_from_db()
        paypal.instrument_declined = True

        result = process_paypal_webhook(
            {},
            {
                "id": "WH-FUNDING-DECLINED",
                "event_type": "CHECKOUT.ORDER.APPROVED",
                "resource": {"id": order.paypal_order_id},
            },
            paypal,
            self.inventory,
        )

        order.refresh_from_db()
        self.assertEqual(result.pk, order.pk)
        self.assertEqual(order.status, Order.Status.FUNDING_RETRY)
        self.assertTrue(
            order.events.filter(event_key="paypal-webhook:WH-FUNDING-DECLINED").exists()
        )

    def test_webhook_requires_shape_only_for_supported_event_types(self):
        paypal = PayPalSpy()

        self.assertIsNone(
            process_paypal_webhook(
                {},
                {"id": "WH-IGNORED", "event_type": "UNSUBSCRIBED.EVENT"},
                paypal,
                self.inventory,
            )
        )
        self.assertIsNone(
            process_paypal_webhook(
                {},
                {"id": "WH-SHIPPING", "event_type": "SHIPPING.TRACKING.CREATED"},
                paypal,
                self.inventory,
            )
        )
        with self.assertRaises(PaymentDataError):
            process_paypal_webhook({}, {}, paypal, self.inventory)
        with self.assertRaises(PaymentDataError):
            process_paypal_webhook(
                {},
                {"id": "WH-MISSING", "event_type": "CHECKOUT.ORDER.APPROVED"},
                paypal,
                self.inventory,
            )

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
                return SimpleNamespace(
                    item_id=item_id,
                    listing_status="Active",
                    price=Decimal("6.00"),
                    currency="USD",
                    quantity=type(self).quantity,
                    variations=(),
                    volume_discounts=(),
                )

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

    def test_gateway_rejects_ended_listing_before_inventory_operation(self):
        order = self.create_order()
        reservation = order.items.get().reservation

        class EndedListingClient:
            def __enter__(self):
                return self

            def __exit__(self, exception_type, exception, traceback):
                return False

            def get_item(self, item_id):
                return SimpleNamespace(item_id=item_id, listing_status="Ended")

        with patch("catalog.inventory.EbayTradingClient", EndedListingClient):
            with self.assertRaises(InventoryUnavailable):
                EbayInventoryGateway().commit(reservation)

        self.assertFalse(
            InventoryOperation.objects.filter(
                idempotency_key=f"sale-{reservation.pk}"
            ).exists()
        )

    def test_gateway_rejects_locally_inactive_listing_before_client_call(self):
        order = self.create_order()
        reservation = order.items.get().reservation
        Product.objects.filter(pk=self.product.pk).update(active=False)

        with patch("catalog.inventory.EbayTradingClient") as client:
            with self.assertRaises(InventoryUnavailable):
                EbayInventoryGateway().commit(reservation)

        client.assert_not_called()
        self.assertFalse(
            InventoryOperation.objects.filter(
                idempotency_key=f"sale-{reservation.pk}"
            ).exists()
        )

    def test_capture_reduces_sku_less_variant_by_specifics(self):
        specifics = {"Combo": ["Moded Wide body with mount"]}
        variant = self.product.variants.create(
            source_key="missing-21368ac4ab1deb45e9c574bb",
            sku="",
            title="Moded Wide body with mount",
            specifics=specifics,
            price=Decimal("6.00"),
            quantity=5,
        )
        product = self.product
        self.line = CheckoutLine(
            product_id=self.product.pk,
            variant_id=variant.pk,
            quantity=2,
        )
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)

        class SkuLessVariationClient:
            quantity = 5
            revise_args = None

            def __enter__(self):
                return self

            def __exit__(self, exception_type, exception, traceback):
                return False

            def get_item(self, item_id):
                live_variant = SimpleNamespace(
                    source_key=variant.source_key,
                    sku="",
                    title=variant.title,
                    specifics=specifics,
                    price=variant.price,
                    quantity=type(self).quantity,
                    purchasable=True,
                )
                return ebay_listing(
                    product,
                    product.price,
                    product.currency,
                    type(self).quantity,
                    (live_variant,),
                )

            def revise_inventory_status(self, *args):
                raise AssertionError(
                    "SKU-less inventory must not use ReviseInventoryStatus"
                )

            def revise_variation_inventory(
                self,
                item_id,
                quantity,
                message_id,
                source_key,
                sent_specifics,
                price,
                currency,
            ):
                type(self).revise_args = (
                    item_id,
                    quantity,
                    message_id,
                    source_key,
                    sent_specifics,
                    price,
                    currency,
                )
                type(self).quantity = quantity
                return quantity

        with patch("catalog.inventory.EbayTradingClient", SkuLessVariationClient):
            result = capture_paypal_order(
                order.pk, paypal, EbayInventoryGateway()
            )

        variant.refresh_from_db()
        self.assertEqual(result.status, Order.Status.PAID)
        self.assertEqual(variant.quantity, 3)
        self.assertEqual(
            SkuLessVariationClient.revise_args,
            (
                self.product.ebay_item_id,
                3,
                f"sale-{order.items.get().reservation.pk}",
                variant.source_key,
                specifics,
                variant.price,
                self.product.currency,
            ),
        )

    def test_capture_rejects_live_variant_price_change_and_refreshes_catalog(self):
        variant = self.product.variants.create(
            source_key="SIZE-L",
            sku="SIZE-L",
            title="Large",
            price=Decimal("6.00"),
            quantity=5,
        )
        self.line = CheckoutLine(
            product_id=self.product.pk,
            variant_id=variant.pk,
            quantity=2,
        )
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        live_variant = SimpleNamespace(
            source_key=variant.source_key,
            sku=variant.sku,
            title=variant.title,
            specifics={},
            price=Decimal("9.00"),
            quantity=5,
            purchasable=True,
        )
        listing = ebay_listing(
            self.product,
            Decimal("9.00"),
            "USD",
            5,
            (live_variant,),
        )

        class ChangedPriceClient:
            def __enter__(self):
                return self

            def __exit__(self, exception_type, exception, traceback):
                return False

            def get_item(self, item_id):
                return listing

            def revise_inventory_status(self, *args):
                raise AssertionError("A changed quote must not reduce inventory")

        with patch("catalog.inventory.EbayTradingClient", ChangedPriceClient):
            with self.assertRaisesMessage(InventoryUnavailable, "price"):
                capture_paypal_order(order.pk, paypal, EbayInventoryGateway())

        order.refresh_from_db()
        variant.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertEqual(
            order.items.get().reservation.status,
            InventoryReservation.Status.RELEASED,
        )
        self.assertEqual(paypal.capture_calls, 0)
        self.assertEqual(variant.price, Decimal("9.00"))
        self.assertEqual(self.product.price, Decimal("9.00"))
        self.assertFalse(InventoryOperation.objects.exists())

    def test_gateway_rejects_live_currency_change_and_refreshes_catalog(self):
        order = self.create_order()
        reservation = order.items.get().reservation
        listing = ebay_listing(
            self.product,
            Decimal("6.00"),
            "EUR",
            5,
        )

        class ChangedCurrencyClient:
            def __enter__(self):
                return self

            def __exit__(self, exception_type, exception, traceback):
                return False

            def get_item(self, item_id):
                return listing

        with patch("catalog.inventory.EbayTradingClient", ChangedCurrencyClient):
            with self.assertRaisesMessage(InventoryUnavailable, "price"):
                EbayInventoryGateway().commit(reservation)

        self.product.refresh_from_db()
        self.assertEqual(self.product.currency, "EUR")
        self.assertFalse(InventoryOperation.objects.exists())

    def test_capture_rechecks_quote_at_the_inventory_write_boundary(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        listings = [
            ebay_listing(self.product, Decimal("6.00"), "USD", 5),
            ebay_listing(self.product, Decimal("8.00"), "USD", 5),
        ]

        class RacingPriceClient:
            def __enter__(self):
                return self

            def __exit__(self, exception_type, exception, traceback):
                return False

            def get_item(self, item_id):
                return listings.pop(0)

            def revise_inventory_status(self, *args):
                raise AssertionError("A changed quote must not reduce inventory")

        with patch("catalog.inventory.EbayTradingClient", RacingPriceClient):
            with self.assertRaisesMessage(EbayInventoryConflict, "price"):
                capture_paypal_order(order.pk, paypal, EbayInventoryGateway())

        order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertEqual(paypal.capture_calls, 0)
        self.assertEqual(self.product.price, Decimal("8.00"))
        self.assertFalse(InventoryOperation.objects.exists())

    def test_capture_rechecks_volume_discount_at_inventory_write_boundary(self):
        original_discounts = ({"min_quantity": 2, "percent_off": "10"},)
        changed_discounts = ({"min_quantity": 2, "percent_off": "5"},)
        self.product.volume_discounts = list(original_discounts)
        self.product.save(update_fields=("volume_discounts", "updated_at"))
        order = self.create_order(expected_total=Decimal("12.72"))
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        listings = [
            ebay_listing(
                self.product,
                Decimal("6.00"),
                "USD",
                5,
                volume_discounts=original_discounts,
            ),
            ebay_listing(
                self.product,
                Decimal("6.00"),
                "USD",
                5,
                volume_discounts=changed_discounts,
            ),
        ]

        class RacingDiscountClient:
            def __enter__(self):
                return self

            def __exit__(self, exception_type, exception, traceback):
                return False

            def get_item(self, item_id):
                return listings.pop(0)

            def revise_inventory_status(self, *args):
                raise AssertionError("A changed quote must not reduce inventory")

        with patch("catalog.inventory.EbayTradingClient", RacingDiscountClient):
            with self.assertRaisesMessage(EbayInventoryConflict, "price"):
                capture_paypal_order(order.pk, paypal, EbayInventoryGateway())

        order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertEqual(paypal.capture_calls, 0)
        self.assertEqual(self.product.volume_discounts, list(changed_discounts))
        self.assertFalse(InventoryOperation.objects.exists())

    def test_capture_rechecks_listing_status_at_inventory_write_boundary(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        active = ebay_listing(self.product, Decimal("6.00"), "USD", 5)
        ended = SimpleNamespace(**{**active.__dict__, "listing_status": "Ended"})
        listings = [active, ended]

        class RacingStatusClient:
            def __enter__(self):
                return self

            def __exit__(self, exception_type, exception, traceback):
                return False

            def get_item(self, item_id):
                return listings.pop(0)

            def revise_inventory_status(self, *args):
                raise AssertionError("An ended listing must not reduce inventory")

        with patch("catalog.inventory.EbayTradingClient", RacingStatusClient):
            with self.assertRaisesMessage(EbayInventoryConflict, "no longer available"):
                capture_paypal_order(order.pk, paypal, EbayInventoryGateway())

        order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertFalse(self.product.active)
        self.assertEqual(self.product.quantity, 0)
        self.assertEqual(paypal.capture_calls, 0)
        self.assertFalse(InventoryOperation.objects.exists())

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
                return SimpleNamespace(
                    item_id=item_id,
                    listing_status="Active",
                    quantity=type(self).quantity,
                    variations=(),
                )

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

    def test_gateway_rebases_release_after_prewrite_quantity_drift(self):
        order = self.create_order()
        reservation = order.items.get().reservation
        reservation.status = InventoryReservation.Status.COMMITTED
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

        class DriftingReleaseClient:
            quantities = [3, 2, 2, 2]
            revise_calls = 0

            def __enter__(self):
                return self

            def __exit__(self, exception_type, exception, traceback):
                return False

            def get_item(self, item_id):
                return SimpleNamespace(
                    item_id=item_id,
                    listing_status="Active",
                    quantity=type(self).quantities.pop(0),
                    variations=(),
                )

            def revise_inventory_status(self, item_id, quantity, message_id, sku=""):
                type(self).revise_calls += 1
                return quantity

        with patch("catalog.inventory.EbayTradingClient", DriftingReleaseClient):
            with self.assertRaises(EbayInventoryConflict):
                cancel_order(order.pk, EbayInventoryGateway())
            cancel_order(
                order.pk,
                EbayInventoryGateway(),
                capture_definitely_absent=True,
            )

        reservation.refresh_from_db()
        operation = InventoryOperation.objects.get(
            idempotency_key=f"release-{reservation.pk}"
        )
        self.assertEqual(reservation.status, InventoryReservation.Status.RELEASED)
        self.assertEqual(operation.expected_quantity, 2)
        self.assertEqual(operation.requested_quantity, 4)
        self.assertEqual(operation.status, InventoryOperation.Status.SUCCEEDED)
        self.assertEqual(DriftingReleaseClient.revise_calls, 1)

    def test_ambiguous_release_conflict_remains_terminal(self):
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
        InventoryOperation.objects.create(
            idempotency_key=f"release-{reservation.pk}",
            product=self.product,
            reason=InventoryOperation.Reason.RELEASE,
            expected_quantity=3,
            requested_quantity=5,
        )

        class AmbiguousReleaseClient:
            revise_calls = 0

            def __enter__(self):
                return self

            def __exit__(self, exception_type, exception, traceback):
                return False

            def get_item(self, item_id):
                return SimpleNamespace(
                    item_id=item_id,
                    listing_status="Active",
                    quantity=2,
                    variations=(),
                )

            def revise_inventory_status(self, *args):
                type(self).revise_calls += 1
                raise AssertionError("Ambiguous inventory must not be rebased")

        gateway = EbayInventoryGateway()
        with patch("catalog.inventory.EbayTradingClient", AmbiguousReleaseClient):
            with self.assertRaises(EbayInventoryConflict):
                gateway.release(reservation)
            with self.assertRaises(EbayInventoryConflict):
                gateway.release(reservation)

        operation = InventoryOperation.objects.get(
            idempotency_key=f"release-{reservation.pk}"
        )
        self.assertEqual(operation.status, InventoryOperation.Status.FAILED)
        self.assertEqual(AmbiguousReleaseClient.revise_calls, 0)

    def test_removed_variation_resolves_release_without_inventory_write(self):
        variant = self.product.variants.create(
            source_key="retired",
            sku="OLD-SKU",
            title="Retired option",
            price=self.product.price,
            quantity=3,
        )
        self.line = CheckoutLine(
            product_id=self.product.pk,
            variant_id=variant.pk,
            quantity=2,
        )
        order = self.create_order()
        reservation = order.items.get().reservation
        reservation.status = InventoryReservation.Status.COMMITTED
        reservation.save(update_fields=("status", "updated_at"))
        InventoryOperation.objects.create(
            idempotency_key=f"sale-{reservation.pk}",
            product=self.product,
            variant=variant,
            reason=InventoryOperation.Reason.SALE,
            expected_quantity=5,
            requested_quantity=3,
            verified_quantity=3,
            status=InventoryOperation.Status.SUCCEEDED,
        )
        live_variant = SimpleNamespace(
            source_key=variant.source_key, sku=variant.sku, quantity=3
        )
        listings = [
            ebay_listing(
                self.product,
                self.product.price,
                self.product.currency,
                3,
                (live_variant,),
            ),
            ebay_listing(
                self.product,
                self.product.price,
                self.product.currency,
                3,
            ),
        ]

        class RemovedVariationClient:
            def __enter__(self):
                return self

            def __exit__(self, exception_type, exception, traceback):
                return False

            def get_item(self, item_id):
                return listings.pop(0)

            def revise_inventory_status(self, *args):
                raise AssertionError("Removed inventory must not be restored")

        with patch("catalog.inventory.EbayTradingClient", RemovedVariationClient):
            cancel_order(order.pk, EbayInventoryGateway())

        reservation.refresh_from_db()
        variant.refresh_from_db()
        self.assertEqual(reservation.status, InventoryReservation.Status.RELEASED)
        self.assertFalse(variant.active)
        self.assertFalse(
            InventoryOperation.objects.filter(
                idempotency_key=f"release-{reservation.pk}"
            ).exists()
        )

    def test_ambiguous_release_survives_removed_variation(self):
        variant = self.product.variants.create(
            source_key="removed",
            sku="REMOVED-SKU",
            title="Removed option",
            price=self.product.price,
            quantity=3,
        )
        self.line = CheckoutLine(
            product_id=self.product.pk,
            variant_id=variant.pk,
            quantity=2,
        )
        order = self.create_order()
        reservation = order.items.get().reservation
        reservation.status = InventoryReservation.Status.RELEASING
        reservation.save(update_fields=("status", "updated_at"))
        InventoryOperation.objects.create(
            idempotency_key=f"sale-{reservation.pk}",
            product=self.product,
            variant=variant,
            reason=InventoryOperation.Reason.SALE,
            expected_quantity=5,
            requested_quantity=3,
            verified_quantity=3,
            status=InventoryOperation.Status.SUCCEEDED,
        )
        operation = InventoryOperation.objects.create(
            idempotency_key=f"release-{reservation.pk}",
            product=self.product,
            variant=variant,
            reason=InventoryOperation.Reason.RELEASE,
            expected_quantity=3,
            requested_quantity=5,
        )
        listing = ebay_listing(
            self.product,
            self.product.price,
            self.product.currency,
            3,
        )

        class RemovedVariationClient:
            def __enter__(self):
                return self

            def __exit__(self, exception_type, exception, traceback):
                return False

            def get_item(self, item_id):
                return listing

            def revise_inventory_status(self, *args):
                raise AssertionError("Ambiguous inventory must not be restored")

        with patch("catalog.inventory.EbayTradingClient", RemovedVariationClient):
            cancel_order(order.pk, EbayInventoryGateway())

        operation.refresh_from_db()
        reservation.refresh_from_db()
        self.assertEqual(operation.status, InventoryOperation.Status.SUCCEEDED)
        self.assertIsNone(operation.verified_quantity)
        self.assertEqual(reservation.status, InventoryReservation.Status.RELEASED)

    def test_ambiguous_release_survives_inactive_listing(self):
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
        operation = InventoryOperation.objects.create(
            idempotency_key=f"release-{reservation.pk}",
            product=self.product,
            reason=InventoryOperation.Reason.RELEASE,
            expected_quantity=3,
            requested_quantity=5,
        )
        listing = SimpleNamespace(
            item_id=self.product.ebay_item_id,
            listing_status="Ended",
        )

        class EndedListingClient:
            def __enter__(self):
                return self

            def __exit__(self, exception_type, exception, traceback):
                return False

            def get_item(self, item_id):
                return listing

            def revise_inventory_status(self, *args):
                raise AssertionError("Inactive inventory must not be restored")

        with patch("catalog.inventory.EbayTradingClient", EndedListingClient):
            cancel_order(order.pk, EbayInventoryGateway())

        operation.refresh_from_db()
        reservation.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(operation.status, InventoryOperation.Status.SUCCEEDED)
        self.assertIsNone(operation.verified_quantity)
        self.assertEqual(reservation.status, InventoryReservation.Status.RELEASED)
        self.assertFalse(self.product.active)

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

    def test_full_refund_idempotency_uses_refunded_total(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)
        refund_order(order.pk, paypal)
        Order.objects.filter(pk=order.pk).update(status=Order.Status.SHIPPED)

        refund_order(order.pk, paypal)

        self.assertEqual(paypal.refund_calls, 1)

    def test_pending_refund_is_visible_and_completed_by_webhook(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)
        paypal.refund_status = "PENDING"

        refund_order(order.pk, paypal)
        refund_order(order.pk, paypal)

        order.refresh_from_db()
        refund = order.refunds.get()
        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.assertEqual(order.refunded_total, Decimal("0.00"))
        self.assertEqual(paypal.refund_calls, 1)

        process_paypal_webhook(
            {},
            {
                "id": "WH-PENDING-REFUND",
                "event_type": "PAYMENT.CAPTURE.REFUNDED",
                "resource": {
                    "id": "REFUND-1",
                    "amount": {"currency_code": "USD", "value": "13.80"},
                    "links": [
                        {
                            "rel": "up",
                            "href": "https://api-m.paypal.com/v2/payments/captures/CAPTURE-1",
                        }
                    ],
                },
            },
            paypal,
            self.inventory,
        )

        order.refresh_from_db()
        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.COMPLETED)
        self.assertEqual(order.status, Order.Status.REFUNDED)
        self.assertEqual(order.refunded_total, order.total)

    def test_pending_refund_reconciles_and_terminal_failure_can_retry(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)
        paypal.refund_status = "PENDING"
        refund_order(order.pk, paypal)
        refund = order.refunds.get()

        paypal.refund_status = "FAILED"
        result = reconcile_pending_refund(refund.pk, paypal)

        self.assertEqual(result.status, Refund.Status.FAILED)
        paypal.refund_status = "COMPLETED"
        refund_order(order.pk, paypal)
        order.refresh_from_db()
        self.assertEqual(paypal.refund_calls, 2)
        self.assertEqual(order.status, Order.Status.REFUNDED)
        self.assertEqual(
            list(order.refunds.values_list("status", flat=True)),
            [Refund.Status.FAILED, Refund.Status.COMPLETED],
        )

    def test_pending_refund_poll_refreshes_its_health_timestamp(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)
        paypal.refund_status = "PENDING"
        refund_order(order.pk, paypal)
        refund = order.refunds.get()
        stale_at = timezone.now() - timedelta(hours=1)
        Refund.objects.filter(pk=refund.pk).update(updated_at=stale_at)

        result = reconcile_pending_refund(refund.pk, paypal)

        self.assertEqual(result.status, Refund.Status.PENDING)
        self.assertGreater(result.updated_at, stale_at)

    def test_paypal_tracking_poll_imports_and_updates_idempotently(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)
        tracker_id = "CAPTURE-1-TRACK-1"
        paypal.trackers[tracker_id] = {
            "transaction_id": "CAPTURE-1",
            "tracking_number": "TRACK-1",
            "carrier": "OTHER",
            "carrier_name_other": "Regional Courier",
            "status": "SHIPPED",
            "last_updated_time": "2026-07-13T10:00:00Z",
        }

        reconcile_paypal_tracking(order.pk, paypal)
        reconcile_paypal_tracking(order.pk, paypal)

        shipment = order.shipments.get()
        self.assertEqual(order.shipments.count(), 1)
        self.assertEqual(shipment.source, Shipment.Source.PAYPAL)
        self.assertEqual(shipment.carrier, "Regional Courier")
        self.assertEqual(shipment.status, Shipment.Status.SHIPPED)
        self.assertEqual(
            order.events.filter(kind="shipment.reconciled").count(), 1
        )

        paypal.trackers[tracker_id] = {
            **paypal.trackers[tracker_id],
            "carrier": "UPS",
            "status": "DELIVERED",
            "last_updated_time": "2026-07-13T11:00:00Z",
        }
        reconcile_paypal_tracking(order.pk, paypal)
        paypal.trackers[tracker_id] = {
            **paypal.trackers[tracker_id],
            "status": "SHIPPED",
            "last_updated_time": "2026-07-13T10:30:00Z",
        }
        reconcile_paypal_tracking(order.pk, paypal)

        shipment.refresh_from_db()
        self.assertEqual(order.shipments.count(), 1)
        self.assertEqual(shipment.carrier, "UPS")
        self.assertEqual(shipment.status, Shipment.Status.DELIVERED)
        events = order.events.filter(kind="shipment.reconciled")
        self.assertEqual(events.count(), 2)
        self.assertEqual(
            list(events.values_list("data__status", flat=True)),
            ["SHIPPED", "DELIVERED"],
        )

    def test_paypal_tracking_poll_accepts_numberless_local_pickup(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)
        paypal.trackers["CAPTURE-1-PICKUP"] = {
            "transaction_id": "CAPTURE-1",
            "status": "LOCAL_PICKUP",
            "last_updated_time": "2026-07-13T10:00:00Z",
        }

        reconcile_paypal_tracking(order.pk, paypal)

        order.refresh_from_db()
        shipment = order.shipments.get()
        self.assertEqual(order.status, Order.Status.FULFILLING)
        self.assertEqual(shipment.tracking_number, "")
        self.assertEqual(shipment.carrier, "")
        self.assertEqual(shipment.status, Shipment.Status.DELIVERED)

    def test_paypal_tracking_poll_accepts_missing_optional_update_time(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)
        paypal.trackers["CAPTURE-1-TRACK-NO-TIME"] = {
            "transaction_id": "CAPTURE-1",
            "tracking_number": "TRACK-NO-TIME",
            "carrier": "USPS",
            "status": "SHIPPED",
        }

        reconcile_paypal_tracking(order.pk, paypal)
        reconcile_paypal_tracking(order.pk, paypal)

        shipment = order.shipments.get()
        self.assertIsNone(shipment.provider_updated_at)
        self.assertIsNotNone(shipment.shipped_at)
        self.assertEqual(
            order.events.filter(kind="shipment.reconciled").count(), 1
        )

    def test_paypal_tracking_poll_preserves_watermark_after_timeless_update(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)
        tracker_id = "CAPTURE-1-TRACK-WATERMARK"
        paypal.trackers[tracker_id] = {
            "transaction_id": "CAPTURE-1",
            "tracking_number": "TRACK-WATERMARK",
            "carrier": "USPS",
            "status": "SHIPPED",
            "last_updated_time": "2026-07-13T11:00:00Z",
        }
        reconcile_paypal_tracking(order.pk, paypal)

        paypal.trackers[tracker_id].pop("last_updated_time")
        paypal.trackers[tracker_id]["status"] = "DELIVERED"
        reconcile_paypal_tracking(order.pk, paypal)

        paypal.trackers[tracker_id]["status"] = "SHIPPED"
        paypal.trackers[tracker_id]["last_updated_time"] = "2026-07-13T10:30:00Z"
        reconcile_paypal_tracking(order.pk, paypal)

        shipment = order.shipments.get()
        self.assertEqual(shipment.status, Shipment.Status.DELIVERED)
        self.assertEqual(
            shipment.provider_updated_at.isoformat(), "2026-07-13T11:00:00+00:00"
        )
        self.assertEqual(
            order.events.filter(kind="shipment.reconciled").count(), 2
        )

    def test_paypal_tracking_poll_rejects_mismatched_identity_and_shape(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)
        tracker_id = "CAPTURE-1-TRACK-IDENTITY"
        paypal.trackers[tracker_id] = {
            "transaction_id": "OTHER-CAPTURE",
            "tracking_number": "TRACK-IDENTITY",
            "carrier": "USPS",
            "status": "SHIPPED",
            "last_updated_time": "2026-07-13T10:00:00Z",
        }

        with self.assertRaisesMessage(PaymentDataError, "transaction does not match"):
            reconcile_paypal_tracking(order.pk, paypal)

        paypal.trackers[tracker_id]["transaction_id"] = "CAPTURE-1"
        paypal.trackers[tracker_id]["last_updated_time"] = "invalid"
        with self.assertRaisesMessage(PaymentDataError, "update time"):
            reconcile_paypal_tracking(order.pk, paypal)

        Order.objects.filter(pk=order.pk).update(paypal_capture_id="LOCAL-CAPTURE")
        with self.assertRaisesMessage(PaymentDataError, "capture identity"):
            reconcile_paypal_tracking(order.pk, paypal)
        self.assertFalse(order.shipments.exists())

    def test_paypal_tracking_query_follows_final_shipment_until_terminal(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)
        self.assertTrue(
            orders_needing_paypal_tracking().filter(pk=order.pk).exists()
        )
        tracker_id = "CAPTURE-1-TRACK-FINAL"
        paypal.trackers[tracker_id] = {
            "transaction_id": "CAPTURE-1",
            "tracking_number": "TRACK-FINAL",
            "carrier": "USPS",
            "status": "SHIPPED",
            "last_updated_time": "2026-07-13T10:00:00Z",
        }
        reconcile_paypal_tracking(order.pk, paypal)
        record_manual_shipment(
            order.pk,
            "USPS",
            "TRACK-FINAL",
            Shipment.Status.SHIPPED,
            completes_order=True,
        )

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.SHIPPED)
        self.assertTrue(
            orders_needing_paypal_tracking().filter(pk=order.pk).exists()
        )

        paypal.trackers[tracker_id] = {
            **paypal.trackers[tracker_id],
            "status": "DELIVERED",
            "last_updated_time": "2026-07-13T11:00:00Z",
        }
        reconcile_paypal_tracking(order.pk, paypal)

        self.assertFalse(
            orders_needing_paypal_tracking().filter(pk=order.pk).exists()
        )


    def test_manual_shipment_preserves_partial_refund_status(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)
        process_paypal_webhook(
            {},
            {
                "id": "WH-PARTIAL-REFUND",
                "event_type": "PAYMENT.CAPTURE.REFUNDED",
                "resource": {
                    "id": "REFUND-PARTIAL",
                    "amount": {"currency_code": "USD", "value": "5.00"},
                    "links": [
                        {
                            "method": "GET",
                            "rel": "up",
                            "href": "https://api-m.paypal.com/v2/payments/captures/CAPTURE-1",
                        }
                    ],
                },
            },
            paypal,
            self.inventory,
        )

        record_manual_shipment(order.pk, "USPS", "940000003", "shipped")

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PARTIALLY_REFUNDED)

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
                    "links": [
                        {
                            "method": "GET",
                            "rel": "up",
                            "href": "https://api-m.paypal.com/v2/payments/captures/CAPTURE-1",
                        }
                    ],
                },
            }

        process_paypal_webhook({}, event("WH-R1", "REFUND-1", "5.00"), paypal, self.inventory)
        process_paypal_webhook({}, event("WH-R2", "REFUND-2", "4.00"), paypal, self.inventory)
        process_paypal_webhook({}, event("WH-R1-LATE", "REFUND-1", "5.00"), paypal, self.inventory)

        order.refresh_from_db()
        self.assertEqual(order.refunded_total, Decimal("9.00"))
        self.assertEqual(Refund.objects.filter(order=order).count(), 2)

    def test_all_paypal_tracking_statuses_map_to_local_statuses(self):
        expected = {
            Shipment.Status.CANCELLED: {"CANCELLED", "VOID"},
            Shipment.Status.DELIVERED: {"DELIVERED", "LOCAL_PICKUP", "COMPLETED"},
            Shipment.Status.SHIPPED: {
                "SHIPPED",
                "DROPPED_OFF",
                "IN_TRANSIT",
                "DELIVERY_SCHEDULED",
            },
            Shipment.Status.LABEL_CREATED: {
                "SHIPMENT_CREATED",
                "LABEL_PRINTED",
                "IN_PROCESS",
                "NEW",
                "PROCESSED",
                "NOT_SHIPPED",
            },
            Shipment.Status.ON_HOLD: {
                "ON_HOLD",
                "RETURNED",
                "ERROR",
                "UNCONFIRMED",
                "PICKUP_FAILED",
                "DELIVERY_DELAYED",
                "DELIVERY_FAILED",
                "INRETURN",
            },
        }

        for local_status, paypal_statuses in expected.items():
            for paypal_status in paypal_statuses:
                with self.subTest(paypal_status=paypal_status):
                    self.assertEqual(_shipment_status(paypal_status), local_status)
        with self.assertRaises(PaymentDataError):
            _shipment_status("UNKNOWN")

    def test_paypal_refund_capture_identity_comes_from_up_link(self):
        resource = {
            "links": [
                {
                    "method": "GET",
                    "rel": "self",
                    "href": "https://api-m.paypal.com/v2/payments/refunds/3NG36268BJ600681V",
                },
                {
                    "method": "GET",
                    "rel": "up",
                    "href": "https://api-m.paypal.com/v2/payments/captures/27C890397M291943E",
                },
            ]
        }

        self.assertEqual(_refund_capture_id(resource), "27C890397M291943E")
        with self.assertRaises(PaymentDataError):
            _refund_capture_id({"links": resource["links"][:1]})
        with self.assertRaises(PaymentDataError):
            _refund_capture_id(
                {
                    "links": [
                        {
                            **resource["links"][1],
                            "href": "https:/v2/payments/captures/27C890397M291943E",
                        }
                    ]
                }
            )

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

    def test_partial_shipment_stays_in_fulfillment_until_final_package_ships(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)

        record_manual_shipment(
            order.pk,
            "USPS",
            "PARTIAL-1",
            Shipment.Status.SHIPPED,
            completes_order=False,
        )
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.FULFILLING)

        record_manual_shipment(
            order.pk,
            "UPS",
            "FINAL-2",
            Shipment.Status.SHIPPED,
            completes_order=True,
        )
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.SHIPPED)

    def test_manual_status_correction_clears_contradictory_shipment_dates(self):
        order = self.create_order()
        paypal = PayPalSpy()
        create_paypal_checkout(order.pk, "https://a", "https://b", paypal)
        capture_paypal_order(order.pk, paypal, self.inventory)
        shipment = record_manual_shipment(
            order.pk, "USPS", "CORRECT-1", Shipment.Status.DELIVERED
        )
        self.assertIsNotNone(shipment.delivered_at)

        shipment = record_manual_shipment(
            order.pk, "USPS", "CORRECT-1", Shipment.Status.ON_HOLD
        )

        self.assertIsNone(shipment.shipped_at)
        self.assertIsNone(shipment.delivered_at)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.FULFILLING)


class PayPalClientTests(TestCase):
    def test_tracker_id_is_percent_encoded_in_the_shipping_path(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path == "/v1/oauth2/token":
                return httpx.Response(200, json={"access_token": "TOKEN"})
            return httpx.Response(200, json={"transaction_id": "CAPTURE", "status": "SHIPPED"})

        client = PayPalClient(
            client_id="CLIENT",
            client_secret="SECRET",
            base_url="https://api-m.sandbox.paypal.com",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        client.get_tracker("CAPTURE/../TRACK?account_id=OTHER")

        self.assertEqual(
            requests[-1].url.raw_path,
            b"/v1/shipping/trackers/CAPTURE%2F..%2FTRACK%3Faccount_id%3DOTHER",
        )

    def test_capture_surfaces_recoverable_instrument_decline(self):
        def handler(request):
            if request.url.path == "/v1/oauth2/token":
                return httpx.Response(200, json={"access_token": "TOKEN"})
            return httpx.Response(
                422,
                json={
                    "name": "UNPROCESSABLE_ENTITY",
                    "details": [{"issue": "INSTRUMENT_DECLINED"}],
                },
            )

        client = PayPalClient(
            client_id="CLIENT",
            client_secret="SECRET",
            base_url="https://api-m.sandbox.paypal.com",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        with self.assertRaises(PayPalInstrumentDeclined):
            client.capture_order("ORDER", "CAPTURE")

    def test_refund_surfaces_paypal_issue_and_debug_id(self):
        def handler(request):
            if request.url.path == "/v1/oauth2/token":
                return httpx.Response(200, json={"access_token": "TOKEN"})
            return httpx.Response(
                422,
                json={
                    "name": "UNPROCESSABLE_ENTITY",
                    "message": "The requested action could not be performed.",
                    "debug_id": "DEBUG-REFUND-1",
                    "details": [
                        {
                            "issue": "REFUND_NOT_ALLOWED",
                            "description": "This capture cannot be refunded.",
                        }
                    ],
                },
            )

        client = PayPalClient(
            client_id="CLIENT",
            client_secret="SECRET",
            base_url="https://api-m.sandbox.paypal.com",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        with self.assertRaises(PayPalRefundError) as raised:
            client.refund_capture(
                "CAPTURE", "1.00", "USD", "FM-1-REFUND-1", "REFUND"
            )

        self.assertEqual(raised.exception.issue, "REFUND_NOT_ALLOWED")
        self.assertEqual(raised.exception.debug_id, "DEBUG-REFUND-1")
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("This capture cannot be refunded", str(raised.exception))
        self.assertIn("PayPal debug ID: DEBUG-REFUND-1", str(raised.exception))

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
            if path == "/v2/payments/refunds/REFUND" and request.method == "GET":
                return httpx.Response(200, json={"id": "REFUND", "status": "PENDING"})
            if path == "/v1/shipping/trackers/CAPTURE-TRACK":
                return httpx.Response(
                    200,
                    json={"transaction_id": "CAPTURE", "status": "SHIPPED"},
                )
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
        self.assertEqual(client.get_refund("REFUND")["status"], "PENDING")
        self.assertEqual(
            client.get_tracker("CAPTURE-TRACK")["transaction_id"], "CAPTURE"
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
        self.assertEqual(
            [request.url.path for request in requests].count("/v1/oauth2/token"), 1
        )
