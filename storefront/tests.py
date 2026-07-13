import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from catalog.models import Product
from orders.models import Order

from .models import StoreSettings


class PayPalDouble:
    payloads = {}
    statuses = {}
    events = []
    fail_capture_once = False

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        return False

    def create_order(self, payload, request_id):
        paypal_order_id = f"PAYPAL-ORDER-{len(type(self).payloads) + 1}"
        type(self).payloads[paypal_order_id] = payload
        return {"id": paypal_order_id, "status": "CREATED"}

    def get_order(self, paypal_order_id):
        return {
            "id": paypal_order_id,
            "intent": "CAPTURE",
            "status": type(self).statuses.get(paypal_order_id, "APPROVED"),
            "purchase_units": [type(self).payloads[paypal_order_id]["purchase_units"][0]],
        }

    def capture_order(self, paypal_order_id, request_id):
        type(self).events.append("paypal.capture")
        if type(self).fail_capture_once:
            type(self).fail_capture_once = False
            raise RuntimeError("capture response lost")
        amount = type(self).payloads[paypal_order_id]["purchase_units"][0]["amount"]
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
                                    "currency_code": amount["currency_code"],
                                    "value": amount["value"],
                                },
                            }
                        ]
                    }
                }
            ],
        }


class InventoryDouble:
    def reserve(self, reservation):
        return None

    def commit(self, reservation):
        PayPalDouble.events.append("inventory.commit")

    def release(self, reservation):
        return None


@override_settings(
    EBAY_CLIENT_ID="ebay-client",
    EBAY_CLIENT_SECRET="ebay-secret",
    EBAY_REFRESH_TOKEN="ebay-refresh",
    EBAY_COMPATIBILITY_LEVEL="1423",
    PAYPAL_CLIENT_ID="paypal-client",
    PAYPAL_CLIENT_SECRET="paypal-secret",
)
class StorefrontTests(TestCase):
    def setUp(self):
        StoreSettings.objects.update_or_create(
            pk=1,
            defaults={
                "flat_shipping_amount": Decimal("8.00"),
                "checkout_enabled": True,
            },
        )
        self.product = Product.objects.create(
            ebay_item_id="123456789",
            slug="camera-lens-123456789",
            title="Camera lens",
            description="<p>Clean optics.</p>",
            price=Decimal("12.00"),
            currency="USD",
            condition="Used",
            category_name="Lenses",
            item_specifics={"Mount": ["M42"]},
            listing_url="https://www.ebay.com/itm/123456789",
            listing_type="FixedPriceItem",
            quantity=2,
            last_synced_at=timezone.now(),
        )
        self.product.images.create(
            url="https://i.ebayimg.com/images/g/lens.jpg", position=1
        )
        PayPalDouble.payloads = {}
        PayPalDouble.statuses = {}
        PayPalDouble.events = []
        PayPalDouble.fail_capture_once = False

    def add_product(self, variant_id=""):
        data = {"quantity": "1"}
        if variant_id:
            data["variant_id"] = str(variant_id)
        return self.client.post(
            reverse("storefront:cart_add", kwargs={"slug": self.product.slug}),
            data,
        )

    def checkout_data(self):
        return {
            "customer_email": "buyer@example.com",
            "customer_phone": "",
            "customer_name": "Ada Buyer",
            "shipping_line_1": "1 Main Street",
            "shipping_line_2": "",
            "shipping_city": "Baltimore",
            "shipping_region": "MD",
            "shipping_postal_code": "21201",
            "shipping_country_code": "US",
        }

    def test_catalog_product_cart_and_checkout_render(self):
        self.product.images.create(
            url="https://i.ebayimg.com/images/g/lens-2.jpg", position=2
        )
        catalog = self.client.get(reverse("storefront:catalog"))
        detail = self.client.get(
            reverse("storefront:product_detail", kwargs={"slug": self.product.slug})
        )
        self.add_product()
        cart = self.client.get(reverse("storefront:cart"))
        checkout = self.client.get(reverse("storefront:checkout"))

        self.assertContains(catalog, "Camera lens")
        self.assertContains(detail, "Clean optics")
        self.assertContains(detail, "M42")
        self.assertLess(
            detail.content.index(b"gallery__thumbs"),
            detail.content.index(b"gallery__stage"),
        )
        self.assertContains(cart, "$12.00")
        self.assertContains(checkout, "United States")
        self.assertContains(checkout, "Privacy details")
        self.assertNotContains(checkout, "status link is sent")
        self.assertIn("no-store", checkout.headers["Cache-Control"])
        self.assertContains(cart, "cart-count")

    def test_variant_price_and_name_are_visible(self):
        variant = self.product.variants.create(
            source_key="red",
            sku="RED-1",
            title="Red finish",
            specifics={"Color": ["Red"]},
            price=Decimal("15.00"),
            quantity=1,
        )

        detail = self.client.get(
            reverse("storefront:product_detail", kwargs={"slug": self.product.slug})
        )
        self.add_product(variant.pk)
        checkout = self.client.get(reverse("storefront:checkout"))

        self.assertContains(detail, "From")
        self.assertContains(detail, "gallery--single")
        self.assertContains(detail, 'data-price="15.00"')
        self.assertContains(checkout, "Red finish")
        self.assertContains(checkout, "$15.00")

    def test_stale_variant_blocks_checkout(self):
        variant = self.product.variants.create(
            source_key="red",
            sku="RED-1",
            title="Red finish",
            specifics={},
            price=Decimal("15.00"),
            quantity=1,
        )
        self.add_product(variant.pk)
        variant.purchasable = False
        variant.save(update_fields=("purchasable",))

        cart = self.client.get(reverse("storefront:cart"))
        checkout = self.client.get(reverse("storefront:checkout"))

        self.assertContains(cart, "Remove or update unavailable items")
        self.assertNotContains(cart, "Continue to checkout")
        self.assertRedirects(checkout, reverse("storefront:cart"))

    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    @patch("storefront.views.PayPalClient", PayPalDouble)
    def test_paypal_checkout_commits_inventory_before_capture(self):
        self.add_product()
        created = self.client.post(reverse("storefront:paypal_create"), self.checkout_data())

        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["paypal_order_id"], "PAYPAL-ORDER-1")
        captured = self.client.post(
            reverse("storefront:paypal_capture"),
            json.dumps({"paypal_order_id": "PAYPAL-ORDER-1"}),
            content_type="application/json",
        )

        self.assertEqual(captured.status_code, 200)
        self.assertEqual(PayPalDouble.events, ["inventory.commit", "paypal.capture"])
        order = Order.objects.get()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.total, Decimal("20.00"))
        confirmation = self.client.get(captured.json()["redirect_url"])
        self.assertContains(confirmation, order.reference)
        self.assertIn("no-store", confirmation.headers["Cache-Control"])
        self.assertEqual(self.client.session.get("cart", {}), {})

    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    @patch("storefront.views.PayPalClient", PayPalDouble)
    def test_changed_shipping_details_start_a_new_order(self):
        self.add_product()
        first = self.client.post(reverse("storefront:paypal_create"), self.checkout_data())
        changed = self.checkout_data()
        changed["shipping_line_1"] = "2 Main Street"
        second = self.client.post(reverse("storefront:paypal_create"), changed)

        self.assertEqual(first.json()["paypal_order_id"], "PAYPAL-ORDER-1")
        self.assertEqual(second.json()["paypal_order_id"], "PAYPAL-ORDER-2")
        self.assertEqual(Order.objects.count(), 2)
        self.assertEqual(
            Order.objects.order_by("created_at").first().status, Order.Status.CANCELLED
        )

    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    @patch("storefront.views.PayPalClient", PayPalDouble)
    def test_voided_paypal_attempt_is_cleared_for_the_next_click(self):
        self.add_product()
        first = self.client.post(reverse("storefront:paypal_create"), self.checkout_data())
        PayPalDouble.statuses[first.json()["paypal_order_id"]] = "VOIDED"

        ended = self.client.post(reverse("storefront:paypal_create"), self.checkout_data())
        retried = self.client.post(reverse("storefront:paypal_create"), self.checkout_data())

        self.assertEqual(ended.status_code, 409)
        self.assertEqual(ended.json()["error"], "The previous PayPal checkout has ended.")
        self.assertEqual(retried.json()["paypal_order_id"], "PAYPAL-ORDER-2")

    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    @patch("storefront.views.PayPalClient", PayPalDouble)
    def test_ambiguous_capture_is_reconciled_without_a_new_order(self):
        self.add_product()
        created = self.client.post(reverse("storefront:paypal_create"), self.checkout_data())
        PayPalDouble.fail_capture_once = True

        with self.assertRaisesMessage(RuntimeError, "capture response lost"):
            self.client.post(
                reverse("storefront:paypal_capture"),
                json.dumps({"paypal_order_id": created.json()["paypal_order_id"]}),
                content_type="application/json",
            )
        reconciled = self.client.post(
            reverse("storefront:paypal_create"), self.checkout_data()
        )

        self.assertEqual(reconciled.status_code, 200)
        self.assertIn("redirect_url", reconciled.json())
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Order.objects.get().status, Order.Status.PAID)
        self.assertEqual(self.client.session.get("cart", {}), {})

    def test_malformed_public_input_returns_controlled_errors(self):
        invalid_variant = self.client.post(
            reverse("storefront:cart_add", kwargs={"slug": self.product.slug}),
            {"quantity": "1", "variant_id": "invalid"},
        )
        invalid_capture = self.client.post(
            reverse("storefront:paypal_capture"),
            "[]",
            content_type="application/json",
        )

        self.assertEqual(invalid_variant.status_code, 302)
        self.assertEqual(invalid_capture.status_code, 400)

    def test_health_and_robots_block_indexing(self):
        health = self.client.get(reverse("storefront:health"))
        robots = self.client.get(reverse("storefront:robots"))

        self.assertEqual(health.json(), {"status": "ok"})
        self.assertEqual(health.headers["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(robots.content, b"User-agent: *\nDisallow: /\n")

    def test_store_settings_is_a_singleton(self):
        settings = StoreSettings(flat_shipping_amount=Decimal("9.00"))
        settings.save()

        self.assertEqual(settings.pk, 1)
        self.assertEqual(StoreSettings.objects.count(), 1)
