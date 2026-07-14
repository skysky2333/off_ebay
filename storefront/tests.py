import json
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from catalog.models import Product
from orders.models import InventoryReservation, Order, PayPalCase, Shipment
from orders.paypal import PayPalInstrumentDeclined
from orders.services import (
    CheckoutLine,
    IdempotencyConflict,
    ShippingAddress,
    create_guest_order as create_guest_order_service,
)
from .views import server_error

from .cart import Cart
from .forms import CheckoutForm
from .models import StoreSettings


class PayPalDouble:
    payloads = {}
    statuses = {}
    events = []
    fail_capture_once = False
    instrument_declined_once = False

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
        if type(self).instrument_declined_once:
            type(self).instrument_declined_once = False
            raise PayPalInstrumentDeclined("PayPal declined the selected funding source.")
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
    EBAY_TRADING_ENDPOINT="https://api.sandbox.ebay.com/ws/api.dll",
    EBAY_TOKEN_ENDPOINT="https://api.sandbox.ebay.com/identity/v1/oauth2/token",
    EBAY_NOTIFICATION_PUBLIC_KEY_ENDPOINT=(
        "https://api.sandbox.ebay.com/commerce/notification/v1/public_key"
    ),
    EBAY_CLIENT_ID="ebay-client",
    EBAY_CLIENT_SECRET="ebay-secret",
    EBAY_REFRESH_TOKEN="ebay-refresh",
    EBAY_COMPATIBILITY_LEVEL="1423",
    PAYPAL_CLIENT_ID="paypal-client",
    PAYPAL_CLIENT_SECRET="paypal-secret",
    PAYPAL_WEBHOOK_ID="paypal-webhook",
    PAYPAL_API_BASE_URL="https://api-m.sandbox.paypal.com",
    SUPPORT_EMAIL="support@example.com",
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
        PayPalDouble.instrument_declined_once = False

    def add_product(self, variant_id=""):
        data = {"quantity": "1"}
        if variant_id:
            data["variant_id"] = str(variant_id)
        return self.client.post(
            reverse("storefront:cart_add", kwargs={"slug": self.product.slug}),
            data,
        )

    def checkout_data(self):
        response = self.client.get(reverse("storefront:checkout"))
        return {
            "quote_fingerprint": response.context["quote_fingerprint"],
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

    def create_status_order(self, status, refunded_total=Decimal("0.00")):
        return Order.objects.create(
            checkout_key=uuid.uuid4(),
            checkout_fingerprint="0" * 64,
            status=status,
            customer_email="buyer@example.com",
            customer_name="Ada Buyer",
            shipping_line_1="1 Main Street",
            shipping_city="Baltimore",
            shipping_region="MD",
            shipping_postal_code="21201",
            shipping_country_code="US",
            subtotal=Decimal("12.00"),
            shipping_total=Decimal("8.00"),
            total=Decimal("20.00"),
            refunded_total=refunded_total,
            expires_at=timezone.now(),
        )

    @override_settings(
        STORE_NAME="Configured Store", SUPPORT_EMAIL="help@example.com"
    )
    def test_server_error_uses_configuration_without_request_context(self):
        response = server_error(RequestFactory().get("/broken/"))

        self.assertEqual(response.status_code, 500)
        self.assertContains(
            response,
            "Store unavailable | Configured Store",
            status_code=500,
        )
        self.assertContains(response, ">Configured Store</a>", status_code=500)
        self.assertContains(
            response,
            'href="mailto:help@example.com"',
            status_code=500,
        )

    @override_settings(
        DEBUG=False,
        STORE_NAME="Configured Store",
        SUPPORT_EMAIL="help@example.com",
    )
    def test_csrf_failure_uses_customer_safe_error_page(self):
        client = Client(enforce_csrf_checks=True)

        response = client.post(
            reverse("storefront:cart_add", kwargs={"slug": self.product.slug}),
            {"quantity": "1"},
        )

        self.assertContains(
            response, "Your secure session expired.", status_code=403
        )
        self.assertContains(response, "Configured Store", status_code=403)
        self.assertNotContains(response, "CSRF verification failed", status_code=403)

    @override_settings(DEBUG=False)
    def test_ajax_csrf_failure_returns_actionable_json(self):
        response = Client(enforce_csrf_checks=True).post(
            reverse("storefront:paypal_create"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {
                "error": (
                    "Your secure session expired. Refresh this page and try again."
                )
            },
        )

    def hold_product(self, quantity=1, variant=None, status=None):
        order = create_guest_order_service(
            checkout_key=uuid.uuid4(),
            email="holder@example.com",
            address=ShippingAddress(
                name="Inventory Holder",
                line_1="1 Main Street",
                city="Baltimore",
                region="MD",
                postal_code="21201",
                country_code="US",
            ),
            lines=[
                CheckoutLine(
                    product_id=self.product.pk,
                    variant_id=variant.pk if variant else None,
                    quantity=quantity,
                )
            ],
            shipping_total=Decimal("0.00"),
            expected_total=(
                variant.direct_price if variant else self.product.direct_price
            )
            * quantity,
            inventory=InventoryDouble(),
        )
        reservation = order.items.get().reservation
        if status:
            reservation.status = status
            reservation.save(update_fields=("status", "updated_at"))
        return order, reservation

    def test_catalog_product_cart_and_checkout_render(self):
        self.product.images.create(
            url="https://i.ebayimg.com/images/g/lens-2.jpg", position=2
        )
        self.product.shipping = {"dispatch_time_max": "2"}
        self.product.save(update_fields=("shipping", "updated_at"))
        catalog = self.client.get(reverse("storefront:catalog"))
        detail = self.client.get(
            reverse("storefront:product_detail", kwargs={"slug": self.product.slug})
        )
        self.add_product()
        cart = self.client.get(reverse("storefront:cart"))
        checkout = self.client.get(reverse("storefront:checkout"))

        self.assertContains(catalog, "Camera lens")
        self.assertContains(catalog, "Buy directly here for 10% off")
        self.assertContains(catalog, "Direct</span> $10.80")
        self.assertContains(catalog, "eBay</span> <s>$12.00</s>")
        self.assertContains(catalog, f'href="{self.product.listing_url}"')
        self.assertContains(catalog, "View details")
        self.assertContains(catalog, "Add to cart")
        self.assertContains(catalog, "View on eBay")
        self.assertContains(detail, "Clean optics")
        self.assertContains(detail, "data-product-price>10.80</span>")
        self.assertContains(detail, "data-ebay-price>12.00</span>")
        self.assertContains(detail, "Prefer eBay? View this item on eBay")
        self.assertContains(detail, "M42")
        self.assertContains(detail, "data-gallery-link")
        self.assertContains(detail, "data-image-viewer")
        self.assertContains(detail, "data-image-viewer-image")
        self.assertContains(
            detail,
            'data-image-src="https://i.ebayimg.com/images/g/lens.jpg"',
        )
        self.assertContains(detail, 'role="group" aria-label="Product images"')
        self.assertContains(detail, "data-image-label=\"Open full-size image 2\"")
        self.assertContains(detail, "Ships within 2 business days.")
        self.assertLess(
            detail.content.index(b"gallery__stage"),
            detail.content.index(b"gallery__thumbs"),
        )
        self.assertContains(cart, "$10.80")
        self.assertContains(cart, "<dt>Tax</dt><dd>$0.00</dd>", html=True)
        self.assertIn("no-store", cart.headers["Cache-Control"])
        self.assertContains(checkout, "United States")
        self.assertContains(checkout, "Privacy details")
        self.assertContains(checkout, "1 item")
        self.assertContains(checkout, "<dt>Tax</dt><dd>$0.00</dd>", html=True)
        self.assertContains(checkout, 'aria-describedby="customer-email-help"')
        self.assertLess(
            checkout.content.index(b"order-summary"),
            checkout.content.index(b"checkout-main"),
        )
        self.assertContains(
            checkout,
            'class="paypal-message js-only" data-paypal-loading role="status" aria-live="polite" aria-atomic="true"',
        )
        self.assertContains(checkout, 'type="tel"')
        self.assertContains(
            checkout,
            "<noscript><p class=\"paypal-message\" role=\"status\">JavaScript is required to use PayPal checkout. Enable JavaScript, then refresh this page.</p></noscript>",
            html=True,
        )
        self.assertNotContains(checkout, 'target="_blank"')
        self.assertNotContains(checkout, "status link is sent")
        self.assertIn("no-store", checkout.headers["Cache-Control"])
        self.assertContains(cart, "cart-count")
        self.assertIsInstance(uuid.UUID(self.client.session["checkout_key"]), uuid.UUID)

        privacy = self.client.get(reverse("storefront:privacy"))
        self.assertContains(privacy, "Essential cookies")
        self.assertContains(privacy, "does not use analytics or advertising cookies")

    def test_catalog_card_add_action_routes_variations_to_option_selection(self):
        catalog_url = reverse("storefront:catalog")
        detail_url = reverse(
            "storefront:product_detail", kwargs={"slug": self.product.slug}
        )
        add_url = reverse(
            "storefront:cart_add", kwargs={"slug": self.product.slug}
        )

        direct_catalog = self.client.get(catalog_url)

        self.assertContains(direct_catalog, f'action="{add_url}"')
        self.assertContains(
            direct_catalog, '<input type="hidden" name="quantity" value="1">'
        )

        self.product.variants.create(
            source_key="FINISH-BLACK",
            sku="FINISH-BLACK",
            title="Black finish",
            specifics={"Finish": ["Black"]},
            price=Decimal("12.00"),
            quantity=1,
        )
        variation_catalog = self.client.get(catalog_url)
        detail = self.client.get(detail_url)

        self.assertNotContains(variation_catalog, f'action="{add_url}"')
        self.assertContains(
            variation_catalog, f'href="{detail_url}#purchase"'
        )
        self.assertContains(detail, 'id="purchase"')

    def test_public_read_routes_support_head(self):
        order = self.create_status_order(Order.Status.PAID)
        order.paid_at = timezone.now()
        order.save(update_fields=("paid_at", "updated_at"))
        client = Client()
        urls = (
            reverse("storefront:catalog"),
            reverse(
                "storefront:product_detail", kwargs={"slug": self.product.slug}
            ),
            reverse("storefront:cart"),
            reverse("storefront:checkout"),
            reverse(
                "storefront:order_confirmation",
                kwargs={"token": order.status_token},
            ),
            reverse("storefront:order_status", kwargs={"token": order.status_token}),
            reverse("storefront:health"),
            reverse("storefront:privacy"),
            reverse("storefront:robots"),
        )

        for url in urls:
            with self.subTest(url=url):
                get_response = client.get(url)
                head_response = client.head(url)

                self.assertEqual(head_response.status_code, get_response.status_code)
                self.assertEqual(head_response.content, b"")

        rejected = client.post(reverse("storefront:health"))
        self.assertEqual(rejected.status_code, 405)
        self.assertEqual(rejected.headers["Allow"], "GET, HEAD")

    def test_head_requests_never_mutate_checkout_session(self):
        self.add_product()
        checkout_session = dict(self.client.session)

        self.assertEqual(
            self.client.head(reverse("storefront:checkout")).status_code, 200
        )
        self.assertEqual(dict(self.client.session), checkout_session)

        order = self.create_status_order(Order.Status.PAID)
        order.paid_at = timezone.now()
        order.save(update_fields=("paid_at", "updated_at"))
        session = self.client.session
        session[Cart.checkout_order_key] = order.pk
        session[Cart.checkout_key] = "checkout-key"
        session.save()
        paid_session = dict(self.client.session)
        urls = (
            reverse("storefront:cart"),
            reverse("storefront:checkout"),
            reverse(
                "storefront:order_confirmation", kwargs={"token": order.status_token}
            ),
            reverse("storefront:order_status", kwargs={"token": order.status_token}),
        )

        for url in urls:
            with self.subTest(url=url):
                self.client.head(url)
                self.assertEqual(dict(self.client.session), paid_session)

        order.status = Order.Status.REFUNDED
        order.refunded_total = order.total
        order.refunded_at = timezone.now()
        order.save(
            update_fields=("status", "refunded_total", "refunded_at", "updated_at")
        )
        self.client.head(
            reverse(
                "storefront:order_confirmation", kwargs={"token": order.status_token}
            )
        )
        self.assertEqual(dict(self.client.session), paid_session)

    def test_checkout_form_rejects_unicode_digits_in_zip_code(self):
        form = CheckoutForm({"shipping_postal_code": "\u0662\u0661\u0662\u0660\u0661"})

        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["shipping_postal_code"],
            ["Enter a valid U.S. ZIP code."],
        )

    def test_checkout_form_normalizes_nine_digit_zip_code(self):
        form = CheckoutForm(
            {
                "customer_email": "buyer@example.com",
                "customer_phone": "",
                "customer_name": "Ada Buyer",
                "shipping_line_1": "1 Main Street",
                "shipping_line_2": "",
                "shipping_city": "Baltimore",
                "shipping_region": "MD",
                "shipping_postal_code": "212011234",
                "shipping_country_code": "US",
            }
        )

        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["shipping_postal_code"], "21201-1234")

    def test_cart_stepper_disables_only_the_reached_boundary(self):
        self.add_product()
        line_id = f"product:{self.product.pk}"

        cart = self.client.get(reverse("storefront:cart"))

        self.assertContains(
            cart,
            f'aria-label="Decrease quantity for Camera lens" aria-controls="quantity-{line_id}" disabled',
        )
        self.assertContains(
            cart,
            f'aria-label="Increase quantity for Camera lens" aria-controls="quantity-{line_id}">',
        )

        self.client.post(
            reverse("storefront:cart_update", kwargs={"line_id": line_id}),
            {"quantity": "2"},
        )
        cart = self.client.get(reverse("storefront:cart"))

        self.assertContains(
            cart,
            f'aria-label="Decrease quantity for Camera lens" aria-controls="quantity-{line_id}">',
        )
        self.assertContains(
            cart,
            f'aria-label="Increase quantity for Camera lens" aria-controls="quantity-{line_id}" disabled',
        )

    def test_non_usd_product_never_enters_checkout(self):
        self.product.currency = "EUR"
        self.product.save(update_fields=("currency", "updated_at"))

        catalog = self.client.get(reverse("storefront:catalog"))
        detail = self.client.get(
            reverse("storefront:product_detail", kwargs={"slug": self.product.slug})
        )
        add = self.client.post(
            reverse("storefront:cart_add", kwargs={"slug": self.product.slug}),
            {"quantity": "1"},
            follow=True,
        )

        self.assertNotContains(catalog, self.product.title)
        self.assertEqual(detail.status_code, 404)
        self.assertContains(add, "This item is no longer available.")
        self.assertEqual(self.client.session.get("cart", {}), {})

        self.product.currency = "USD"
        self.product.save(update_fields=("currency", "updated_at"))
        self.add_product()
        self.product.currency = "EUR"
        self.product.save(update_fields=("currency", "updated_at"))

        cart = self.client.get(reverse("storefront:cart"))
        checkout = self.client.get(reverse("storefront:checkout"))

        self.assertContains(cart, "Remove or update unavailable items")
        self.assertNotContains(cart, "Continue to checkout")
        self.assertRedirects(checkout, reverse("storefront:cart"))

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
        self.assertContains(detail, 'data-direct-price="13.50"')
        self.assertContains(detail, 'data-ebay-price="15.00"')
        self.assertContains(checkout, "Red finish")
        self.assertContains(checkout, "$13.50")

    def test_reserved_stock_is_hidden_but_remains_available_to_its_cart(self):
        order, reservation = self.hold_product(quantity=2)

        catalog = self.client.get(reverse("storefront:catalog"))
        detail = self.client.get(
            reverse("storefront:product_detail", kwargs={"slug": self.product.slug})
        )
        blocked_add = self.add_product()

        self.assertNotContains(catalog, self.product.title)
        self.assertEqual(detail.status_code, 404)
        self.assertRedirects(blocked_add, reverse("storefront:catalog"))
        self.assertEqual(self.client.session.get("cart", {}), {})

        line_id = f"product:{self.product.pk}"
        other = Client()
        other_session = other.session
        other_session[Cart.session_key] = {
            line_id: {
                "product_id": self.product.pk,
                "variant_id": None,
                "quantity": 2,
            }
        }
        other_session.save()

        stale_cart = other.get(reverse("storefront:cart"))

        self.assertContains(stale_cart, "Remove or update unavailable items")
        self.assertNotContains(stale_cart, "Continue to checkout")

        session = self.client.session
        session[Cart.session_key] = {
            line_id: {
                "product_id": self.product.pk,
                "variant_id": None,
                "quantity": 2,
            }
        }
        session[Cart.checkout_order_key] = order.pk
        session.save()

        held_cart = self.client.get(reverse("storefront:cart"))

        self.assertContains(held_cart, 'max="2"')
        self.assertContains(held_cart, "Continue to checkout")

        reservation.status = InventoryReservation.Status.COMMITTED
        reservation.save(update_fields=("status", "updated_at"))

        self.assertTrue(
            Product.objects.purchasable().filter(pk=self.product.pk).exists()
        )

    def test_committing_variant_holds_reduce_detail_and_cart_availability(self):
        held_variant = self.product.variants.create(
            source_key="held",
            sku="HELD-1",
            title="Held finish",
            specifics={},
            price=Decimal("12.00"),
            quantity=2,
        )
        available_variant = self.product.variants.create(
            source_key="available",
            sku="AVAILABLE-1",
            title="Available finish",
            specifics={},
            price=Decimal("14.00"),
            quantity=1,
        )
        self.hold_product(
            quantity=2,
            variant=held_variant,
            status=InventoryReservation.Status.COMMITTING,
        )

        detail = self.client.get(
            reverse("storefront:product_detail", kwargs={"slug": self.product.slug})
        )
        blocked_add = self.add_product(held_variant.pk)

        self.assertEqual(detail.context["product"].available_quantity, 1)
        self.assertNotContains(detail, "Held finish")
        self.assertContains(detail, "Available finish")
        self.assertContains(detail, 'data-quantity="1"')
        self.assertEqual(blocked_add.status_code, 302)
        self.assertEqual(self.client.session.get("cart", {}), {})

        line_id = f"variant:{held_variant.pk}"
        session = self.client.session
        session[Cart.session_key] = {
            line_id: {
                "product_id": self.product.pk,
                "variant_id": held_variant.pk,
                "quantity": 1,
            }
        }
        session.save()

        stale_cart = self.client.get(reverse("storefront:cart"))

        self.assertContains(stale_cart, "Remove or update unavailable items")
        self.assertNotContains(stale_cart, "Continue to checkout")

        self.hold_product(
            variant=available_variant,
            status=InventoryReservation.Status.COMMITTING,
        )

        self.assertFalse(
            Product.objects.purchasable().filter(pk=self.product.pk).exists()
        )

    def test_held_cheapest_variant_does_not_set_the_displayed_price(self):
        self.product.price = Decimal("10.00")
        self.product.save(update_fields=("price", "updated_at"))
        cheapest = self.product.variants.create(
            source_key="cheapest",
            sku="CHEAP-1",
            title="Reserved finish",
            price=Decimal("10.00"),
            quantity=1,
        )
        self.product.variants.create(
            source_key="available",
            sku="AVAILABLE-1",
            title="Available finish",
            price=Decimal("20.00"),
            quantity=1,
        )
        self.hold_product(variant=cheapest)

        catalog = self.client.get(reverse("storefront:catalog"))
        detail = self.client.get(
            reverse("storefront:product_detail", kwargs={"slug": self.product.slug})
        )

        self.assertContains(catalog, "Direct</span> $18.00")
        self.assertContains(catalog, "eBay</span> <s>$20.00</s>")
        self.assertContains(detail, "data-product-price>18.00</span>")
        self.assertContains(detail, "data-ebay-price>20.00</span>")
        self.assertNotContains(detail, "Reserved finish")

    def test_variant_quantity_max_supports_no_javascript_purchase(self):
        self.product.variants.create(
            source_key="red",
            sku="RED-1",
            title="Red finish",
            specifics={"Color": ["Red"]},
            price=Decimal("15.00"),
            quantity=2,
        )
        blue = self.product.variants.create(
            source_key="blue",
            sku="",
            title="Blue finish",
            specifics={"Color": ["Blue"]},
            price=Decimal("16.00"),
            quantity=4,
        )

        detail = self.client.get(
            reverse("storefront:product_detail", kwargs={"slug": self.product.slug})
        )
        added = self.client.post(
            reverse("storefront:cart_add", kwargs={"slug": self.product.slug}),
            {"variant_id": blue.pk, "quantity": "4"},
        )

        self.assertContains(detail, 'name="quantity" value="1" min="1" max="4"')
        self.assertRedirects(added, reverse("storefront:cart"))
        self.assertEqual(
            self.client.session["cart"][f"variant:{blue.pk}"]["quantity"], 4
        )

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
        self.assertContains(cart, "Decrease quantity for Camera lens")
        self.assertContains(cart, "Increase quantity for Camera lens")
        self.assertContains(cart, 'aria-controls="quantity-variant:')
        self.assertNotContains(cart, "Continue to checkout")
        self.assertRedirects(checkout, reverse("storefront:cart"))

    def test_deleted_product_is_pruned_from_cart_and_checkout(self):
        product_id = self.product.pk
        line_id = f"product:{product_id}"
        self.add_product()
        self.product.delete()

        cart = self.client.get(reverse("storefront:cart"))

        self.assertEqual(cart.status_code, 200)
        self.assertContains(cart, "Your cart is empty")
        self.assertEqual(self.client.session["cart"], {})

        session = self.client.session
        session["cart"] = {
            line_id: {
                "product_id": product_id,
                "variant_id": None,
                "quantity": 1,
            }
        }
        session.save()

        checkout = self.client.get(reverse("storefront:checkout"))

        self.assertRedirects(checkout, reverse("storefront:cart"))
        self.assertEqual(self.client.session["cart"], {})

    def test_order_status_distinguishes_delivery_refunds_and_payment_transition(self):
        delivered = self.create_status_order(Order.Status.SHIPPED)
        Shipment.objects.create(
            order=delivered,
            carrier="USPS",
            tracking_number="940000000000",
            status=Shipment.Status.DELIVERED,
        )
        delivered_response = self.client.get(
            reverse("storefront:order_status", kwargs={"token": delivered.status_token})
        )

        self.assertContains(delivered_response, "Your order was delivered.")
        self.assertContains(delivered_response, "status-chip--delivered")

        pickup = self.create_status_order(Order.Status.SHIPPED)
        Shipment.objects.create(
            order=pickup,
            carrier="",
            tracking_number="",
            status=Shipment.Status.DELIVERED,
            source=Shipment.Source.PAYPAL,
        )
        pickup_response = self.client.get(
            reverse("storefront:order_status", kwargs={"token": pickup.status_token})
        )

        self.assertContains(pickup_response, "Delivery status")
        self.assertContains(pickup_response, "Delivered")
        self.assertNotContains(pickup_response, "<dt> tracking</dt>")

        secondary = Shipment.objects.create(
            order=delivered,
            carrier="UPS",
            tracking_number="1Z0000000000000000",
            status=Shipment.Status.CANCELLED,
        )
        cancelled_response = self.client.get(
            reverse("storefront:order_status", kwargs={"token": delivered.status_token})
        )

        self.assertContains(cancelled_response, "Your order was delivered.")

        secondary.status = Shipment.Status.SHIPPED
        secondary.save(update_fields=("status",))
        split_response = self.client.get(
            reverse("storefront:order_status", kwargs={"token": delivered.status_token})
        )

        self.assertContains(split_response, "A shipment was delivered.")
        self.assertNotContains(split_response, "status-chip--delivered")

        partial = self.create_status_order(
            Order.Status.PARTIALLY_REFUNDED, Decimal("5.00")
        )
        partial_response = self.client.get(
            reverse("storefront:order_status", kwargs={"token": partial.status_token})
        )

        self.assertContains(partial_response, "Your partial refund was processed.")
        self.assertContains(partial_response, "Original total")
        self.assertContains(partial_response, "-$5.00 USD")
        self.assertContains(partial_response, "$15.00 USD")

        Shipment.objects.create(
            order=partial,
            carrier="USPS",
            tracking_number="PARTIAL-DELIVERED",
            status=Shipment.Status.DELIVERED,
        )
        partial_delivered_response = self.client.get(
            reverse("storefront:order_status", kwargs={"token": partial.status_token})
        )
        self.assertContains(partial_delivered_response, "Your order was delivered.")
        self.assertContains(partial_delivered_response, "Partially refunded")
        self.assertContains(
            partial_delivered_response, "Your partial refund was processed."
        )
        self.assertContains(partial_delivered_response, "-$5.00 USD")

        refunded = self.create_status_order(Order.Status.REFUNDED, Decimal("20.00"))
        refunded_response = self.client.get(
            reverse("storefront:order_status", kwargs={"token": refunded.status_token})
        )

        self.assertContains(refunded_response, "Your order was refunded.")
        self.assertContains(refunded_response, "$0.00 USD")

        Shipment.objects.create(
            order=refunded,
            carrier="USPS",
            tracking_number="REFUNDED-DELIVERED",
            status=Shipment.Status.DELIVERED,
        )
        refunded_delivered_response = self.client.get(
            reverse("storefront:order_status", kwargs={"token": refunded.status_token})
        )

        self.assertContains(refunded_delivered_response, "Your order was delivered.")
        self.assertContains(refunded_delivered_response, ">Refunded</span>")
        self.assertContains(refunded_delivered_response, "Your refund was processed.")

        pending = self.create_status_order(Order.Status.CAPTURE_PENDING)
        pending_response = self.client.get(
            reverse("storefront:order_status", kwargs={"token": pending.status_token})
        )

        self.assertNotContains(pending_response, 'http-equiv="refresh"')
        self.assertContains(pending_response, "Check status")
        self.assertContains(
            pending_response,
            reverse(
                "storefront:order_status", kwargs={"token": pending.status_token}
            ),
        )

    def test_private_order_status_surfaces_paypal_cases_without_internal_details(self):
        disputed = self.create_status_order(Order.Status.PAID)
        disputed.paid_at = timezone.now()
        disputed.save(update_fields=("paid_at", "updated_at"))
        dispute = PayPalCase.objects.create(
            order=disputed,
            kind=PayPalCase.Kind.DISPUTE,
            paypal_case_id="PP-D-CUSTOMER",
            status=PayPalCase.Status.WAITING_FOR_SELLER_RESPONSE,
            reason="UNAUTHORIZED_TRANSACTION",
            stage="INQUIRY",
            amount=Decimal("10.00"),
            currency="USD",
            last_event_type="CUSTOMER.DISPUTE.CREATED",
        )
        status_url = reverse(
            "storefront:order_status", kwargs={"token": disputed.status_token}
        )

        dispute_response = self.client.get(status_url)
        confirmation_response = self.client.get(
            reverse(
                "storefront:order_confirmation",
                kwargs={"token": disputed.status_token},
            )
        )

        self.assertContains(dispute_response, "PayPal review")
        self.assertContains(dispute_response, "A PayPal case is under review.")
        self.assertNotContains(dispute_response, "UNAUTHORIZED_TRANSACTION")
        self.assertNotContains(dispute_response, "Waiting for seller response")
        self.assertRedirects(confirmation_response, status_url)

        dispute.status = PayPalCase.Status.RESOLVED
        dispute.outcome = "RESOLVED_SELLER_FAVOUR"
        dispute.save(update_fields=("status", "outcome", "updated_at"))
        resolved_response = self.client.get(status_url)

        self.assertNotContains(resolved_response, "PayPal review")
        self.assertNotContains(resolved_response, "A PayPal case is under review.")

        reversed_order = self.create_status_order(Order.Status.PAID)
        reversed_order.paid_at = timezone.now()
        reversed_order.save(update_fields=("paid_at", "updated_at"))
        PayPalCase.objects.create(
            order=reversed_order,
            kind=PayPalCase.Kind.REVERSAL,
            paypal_case_id="CAPTURE-CUSTOMER",
            status=PayPalCase.Status.REVERSED,
            reason="CHARGEBACK",
            amount=Decimal("20.00"),
            currency="USD",
            last_event_type="PAYMENT.CAPTURE.REVERSED",
        )

        reversal_response = self.client.get(
            reverse(
                "storefront:order_status",
                kwargs={"token": reversed_order.status_token},
            )
        )

        self.assertContains(reversal_response, "Payment reversed")
        self.assertContains(reversal_response, "PayPal reversed this payment.")
        self.assertNotContains(reversal_response, "CHARGEBACK")

    def test_private_order_pages_hide_cancelled_shipments(self):
        order = self.create_status_order(Order.Status.PAID)
        order.paid_at = timezone.now()
        order.save(update_fields=("paid_at", "updated_at"))
        Shipment.objects.create(
            order=order,
            carrier="USPS",
            tracking_number="ACTIVE-TRACKING",
            status=Shipment.Status.SHIPPED,
        )
        Shipment.objects.create(
            order=order,
            carrier="UPS",
            tracking_number="CANCELLED-TRACKING",
            status=Shipment.Status.CANCELLED,
        )

        responses = (
            self.client.get(
                reverse(
                    "storefront:order_confirmation",
                    kwargs={"token": order.status_token},
                )
            ),
            self.client.get(
                reverse(
                    "storefront:order_status", kwargs={"token": order.status_token}
                )
            ),
        )

        for response in responses:
            self.assertContains(response, "ACTIVE-TRACKING")
            self.assertNotContains(response, "CANCELLED-TRACKING")

    def test_refunded_confirmation_redirects_to_current_status(self):
        order = self.create_status_order(Order.Status.REFUNDED, Decimal("20.00"))
        order.paid_at = timezone.now()
        order.save(update_fields=("paid_at", "updated_at"))
        status_url = reverse(
            "storefront:order_status", kwargs={"token": order.status_token}
        )

        response = self.client.get(
            reverse(
                "storefront:order_confirmation", kwargs={"token": order.status_token}
            )
        )

        self.assertRedirects(response, status_url)

        session = self.client.session
        session[Cart.checkout_order_key] = order.pk
        session.save()

        recovered = self.client.get(status_url, follow=True)

        self.assertEqual(recovered.status_code, 200)
        self.assertContains(recovered, "Your order was refunded.")
        self.assertNotIn(Cart.checkout_order_key, self.client.session)

    def test_inactive_historical_variant_does_not_hide_single_sku_product(self):
        self.product.variants.create(
            source_key="retired",
            sku="OLD-1",
            title="Retired option",
            price=Decimal("15.00"),
            quantity=0,
            active=False,
        )

        catalog = self.client.get(reverse("storefront:catalog"))
        detail = self.client.get(
            reverse("storefront:product_detail", kwargs={"slug": self.product.slug})
        )

        self.assertContains(catalog, self.product.title)
        self.assertEqual(detail.status_code, 200)

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
        self.assertEqual(order.total, Decimal("18.80"))
        confirmation = self.client.get(captured.json()["redirect_url"])
        self.assertContains(confirmation, order.reference)
        self.assertContains(confirmation, "data-copy-order-link")
        self.assertContains(
            confirmation,
            'data-copy-order-status role="status" aria-live="polite" hidden',
        )
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
    def test_changed_shipping_price_starts_a_new_order(self):
        self.add_product()
        first = self.client.post(reverse("storefront:paypal_create"), self.checkout_data())
        StoreSettings.objects.filter(pk=1).update(flat_shipping_amount=Decimal("9.00"))

        second = self.client.post(reverse("storefront:paypal_create"), self.checkout_data())

        self.assertEqual(first.json()["paypal_order_id"], "PAYPAL-ORDER-1")
        self.assertEqual(second.json()["paypal_order_id"], "PAYPAL-ORDER-2")
        self.assertEqual(PayPalDouble.payloads["PAYPAL-ORDER-2"]["purchase_units"][0]["amount"]["value"], "19.80")
        self.assertEqual(
            Order.objects.order_by("created_at").first().status,
            Order.Status.CANCELLED,
        )

    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    @patch("storefront.views.PayPalClient", PayPalDouble)
    def test_changed_product_price_starts_a_new_order(self):
        self.add_product()
        first = self.client.post(reverse("storefront:paypal_create"), self.checkout_data())
        Product.objects.filter(pk=self.product.pk).update(price=Decimal("13.00"))

        second = self.client.post(reverse("storefront:paypal_create"), self.checkout_data())

        self.assertEqual(first.json()["paypal_order_id"], "PAYPAL-ORDER-1")
        self.assertEqual(second.json()["paypal_order_id"], "PAYPAL-ORDER-2")
        self.assertEqual(PayPalDouble.payloads["PAYPAL-ORDER-2"]["purchase_units"][0]["amount"]["value"], "19.70")

    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    @patch("storefront.views.PayPalClient", PayPalDouble)
    def test_first_paypal_order_requires_the_total_rendered_at_checkout(self):
        self.add_product()
        checkout_data = self.checkout_data()
        Product.objects.filter(pk=self.product.pk).update(price=Decimal("13.00"))

        response = self.client.post(
            reverse("storefront:paypal_create"), checkout_data
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"],
            "The order total changed. Refresh checkout and review the updated total.",
        )
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(PayPalDouble.payloads, {})

    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    @patch("storefront.views.PayPalClient", PayPalDouble)
    def test_successful_capture_response_can_be_retried_until_confirmation(self):
        self.add_product()
        created = self.client.post(reverse("storefront:paypal_create"), self.checkout_data())
        payload = json.dumps({"paypal_order_id": created.json()["paypal_order_id"]})

        first = self.client.post(
            reverse("storefront:paypal_capture"), payload, content_type="application/json"
        )
        second = self.client.post(
            reverse("storefront:paypal_capture"), payload, content_type="application/json"
        )

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(self.client.session["checkout_order_id"], Order.objects.get().pk)
        recovery = self.client.get(reverse("storefront:checkout"))
        self.assertRedirects(recovery, first.json()["redirect_url"])
        self.assertNotIn("checkout_order_id", self.client.session)

    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    @patch("storefront.views.PayPalClient", PayPalDouble)
    def test_status_page_recovers_checkout_completed_asynchronously(self):
        self.add_product()
        self.client.post(reverse("storefront:paypal_create"), self.checkout_data())
        order = Order.objects.get()
        Order.objects.filter(pk=order.pk).update(
            status=Order.Status.PAID,
            paid_at=timezone.now(),
            paypal_capture_id="ASYNC-CAPTURE",
            paypal_status="COMPLETED",
        )

        response = self.client.get(
            reverse("storefront:order_status", kwargs={"token": order.status_token})
        )

        self.assertRedirects(
            response,
            reverse(
                "storefront:order_confirmation",
                kwargs={"token": order.status_token},
            ),
        )
        self.assertEqual(self.client.session.get("cart", {}), {})
        self.assertNotIn("checkout_order_id", self.client.session)

    @override_settings(ORDER_RESERVATION_MINUTES=0)
    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    @patch("storefront.views.PayPalClient", PayPalDouble)
    def test_order_expiring_during_paypal_creation_returns_checkout_error(self):
        self.add_product()

        response = self.client.post(
            reverse("storefront:paypal_create"), self.checkout_data()
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"], "The inventory reservation has expired."
        )
        self.assertEqual(Order.objects.get().status, Order.Status.CANCELLED)
        self.assertTrue(self.client.session.get("cart"))
        self.assertNotIn("checkout_order_id", self.client.session)

    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    @patch("storefront.views.PayPalClient", PayPalDouble)
    def test_instrument_decline_keeps_checkout_available_for_paypal_restart(self):
        self.add_product()
        created = self.client.post(reverse("storefront:paypal_create"), self.checkout_data())
        PayPalDouble.instrument_declined_once = True

        response = self.client.post(
            reverse("storefront:paypal_capture"),
            json.dumps({"paypal_order_id": created.json()["paypal_order_id"]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INSTRUMENT_DECLINED")
        order = Order.objects.get()
        self.assertEqual(order.status, Order.Status.FUNDING_RETRY)
        self.assertIn("cart", self.client.session)
        self.assertIn("checkout_order_id", self.client.session)

        retry_page = self.client.get(reverse("storefront:checkout"))
        retry_data = self.checkout_data()
        retry_data["customer_name"] = "Changed Buyer"
        restarted = self.client.post(
            reverse("storefront:paypal_create"), retry_data
        )
        mutation = self.client.post(
            reverse(
                "storefront:cart_update",
                kwargs={"line_id": f"product:{self.product.pk}"},
            ),
            {"quantity": "2"},
        )

        self.assertEqual(restarted.json()["paypal_order_id"], order.paypal_order_id)
        self.assertEqual(Order.objects.count(), 1)
        self.assertContains(
            retry_page, "These details are fixed for this PayPal checkout."
        )
        self.assertNotContains(retry_page, 'name="customer_name"')
        self.assertContains(retry_page, order.customer_name)
        self.assertRedirects(
            mutation,
            reverse("storefront:order_status", kwargs={"token": order.status_token}),
        )
        status_page = self.client.get(
            reverse("storefront:order_status", kwargs={"token": order.status_token})
        )
        self.assertContains(status_page, "Choose another payment method.")
        self.assertContains(status_page, "Return to checkout")

    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    @patch("storefront.views.PayPalClient", PayPalDouble)
    def test_funding_retry_uses_the_fixed_order_quote_and_owned_inventory(self):
        self.add_product()
        created = self.client.post(
            reverse("storefront:paypal_create"), self.checkout_data()
        )
        PayPalDouble.instrument_declined_once = True
        self.client.post(
            reverse("storefront:paypal_capture"),
            json.dumps({"paypal_order_id": created.json()["paypal_order_id"]}),
            content_type="application/json",
        )
        order = Order.objects.get()
        self.product.active = False
        self.product.checkout_excluded = True
        self.product.currency = "EUR"
        self.product.price = Decimal("99.00")
        self.product.quantity = 0
        self.product.save(
            update_fields=(
                "active",
                "checkout_excluded",
                "currency",
                "price",
                "quantity",
                "updated_at",
            )
        )
        StoreSettings.objects.filter(pk=1).update(
            flat_shipping_amount=Decimal("99.00"), checkout_enabled=False
        )

        cart = self.client.get(reverse("storefront:cart"))
        checkout = self.client.get(reverse("storefront:checkout"))

        self.assertRedirects(
            cart,
            reverse("storefront:order_status", kwargs={"token": order.status_token}),
        )
        self.assertEqual(checkout.status_code, 200)
        self.assertEqual(checkout.context["items"][0].available_quantity, 1)
        self.assertEqual(checkout.context["items"][0].unit_price, Decimal("10.80"))
        self.assertEqual(checkout.context["shipping"], Decimal("8.00"))
        self.assertEqual(checkout.context["total"], order.total)
        self.assertContains(checkout, order.customer_email)
        self.assertNotContains(checkout, 'name="customer_email"')

        retry_data = self.checkout_data()
        retry_data["shipping_line_1"] = "Changed address"
        resumed = self.client.post(
            reverse("storefront:paypal_create"), retry_data
        )
        self.assertEqual(resumed.json()["paypal_order_id"], order.paypal_order_id)
        self.assertEqual(Order.objects.count(), 1)

    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    @patch("storefront.views.PayPalClient", PayPalDouble)
    def test_payment_processing_variant_resumes_from_its_fixed_quote(self):
        variant = self.product.variants.create(
            source_key="fixed",
            sku="FIXED-1",
            title="Fixed option",
            price=Decimal("15.00"),
            quantity=1,
        )
        self.add_product(variant.pk)
        checkout_data = self.checkout_data()
        created = self.client.post(
            reverse("storefront:paypal_create"), checkout_data
        )
        order = Order.objects.get()
        order.status = Order.Status.PAYMENT_PROCESSING
        order.save(update_fields=("status", "updated_at"))
        variant.active = False
        variant.price = Decimal("90.00")
        variant.quantity = 0
        variant.save(update_fields=("active", "price", "quantity"))
        self.product.active = False
        self.product.currency = "EUR"
        self.product.save(update_fields=("active", "currency", "updated_at"))
        StoreSettings.objects.filter(pk=1).update(
            flat_shipping_amount=Decimal("90.00")
        )

        checkout = self.client.get(reverse("storefront:checkout"))

        self.assertRedirects(
            checkout,
            reverse("storefront:order_status", kwargs={"token": order.status_token}),
        )

        checkout_data["customer_name"] = "Changed Buyer"
        resumed = self.client.post(
            reverse("storefront:paypal_create"), checkout_data
        )
        self.assertEqual(resumed.status_code, 200)
        self.assertIn("redirect_url", resumed.json())
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.items.get().unit_price, Decimal("13.50"))
        self.assertEqual(order.shipping_total, Decimal("8.00"))
        self.assertEqual(Order.objects.count(), 1)

    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    @patch("storefront.views.PayPalClient", PayPalDouble)
    def test_stale_first_checkout_snapshots_share_one_order(self):
        self.add_product()
        data = self.checkout_data()
        snapshot = dict(self.client.session)
        other = Client()
        other_session = other.session
        other_session.update(snapshot)
        other_session.save()

        first = self.client.post(reverse("storefront:paypal_create"), data)
        second = other.post(reverse("storefront:paypal_create"), data)

        self.assertEqual(first.json()["paypal_order_id"], "PAYPAL-ORDER-1")
        self.assertEqual(second.json()["paypal_order_id"], "PAYPAL-ORDER-1")
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(
            str(Order.objects.get().checkout_key), snapshot["checkout_key"]
        )

    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    @patch("storefront.views.PayPalClient", PayPalDouble)
    def test_stale_conflicting_checkout_recovers_on_retry(self):
        self.add_product()
        first_data = self.checkout_data()
        snapshot = dict(self.client.session)
        other = Client()
        other_session = other.session
        other_session.update(snapshot)
        other_session.save()
        second_data = {**first_data, "shipping_city": "Annapolis"}

        self.client.post(reverse("storefront:paypal_create"), first_data)
        conflict = other.post(reverse("storefront:paypal_create"), second_data)
        self.assertTrue(other.session[Cart.checkout_conflict_key])
        self.assertEqual(
            other.session[Cart.checkout_order_key],
            Order.objects.order_by("created_at").first().pk,
        )
        retried = other.post(reverse("storefront:paypal_create"), second_data)

        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(retried.json()["paypal_order_id"], "PAYPAL-ORDER-2")
        self.assertEqual(Order.objects.count(), 2)
        self.assertEqual(
            Order.objects.order_by("created_at").first().status,
            Order.Status.CANCELLED,
        )

    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    @patch("storefront.views.PayPalClient", PayPalDouble)
    def test_conflict_session_write_does_not_break_approved_capture(self):
        self.add_product()
        created = self.client.post(
            reverse("storefront:paypal_create"), self.checkout_data()
        )
        order = Order.objects.get()
        session = self.client.session
        session[Cart.checkout_order_key] = order.pk
        session[Cart.checkout_conflict_key] = True
        session[Cart.checkout_form_key] = "conflicting-request"
        session.save()

        response = self.client.post(
            reverse("storefront:paypal_capture"),
            json.dumps({"paypal_order_id": created.json()["paypal_order_id"]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)

    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    def test_simultaneous_conflict_recovers_order_created_after_initial_lookup(self):
        self.add_product()
        data = self.checkout_data()

        def concurrent_order(**kwargs):
            create_guest_order_service(
                **{
                    **kwargs,
                    "address": ShippingAddress(
                        name="Other Buyer",
                        line_1="2 Main Street",
                        city="Baltimore",
                        region="MD",
                        postal_code="21201",
                        country_code="US",
                    ),
                }
            )
            raise IdempotencyConflict("Checkout key was already used for another order.")

        with patch("storefront.views.create_guest_order", side_effect=concurrent_order):
            response = self.client.post(reverse("storefront:paypal_create"), data)

        order = Order.objects.get()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.session[Cart.checkout_order_key], order.pk)
        self.assertTrue(self.client.session[Cart.checkout_conflict_key])

    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    @patch("storefront.views.PayPalClient", PayPalDouble)
    def test_cart_add_cancels_awaiting_payment_attempt(self):
        self.add_product()
        self.client.post(reverse("storefront:paypal_create"), self.checkout_data())

        self.add_product()

        self.assertEqual(Order.objects.get().status, Order.Status.CANCELLED)
        self.assertEqual(
            self.client.session["cart"][f"product:{self.product.pk}"]["quantity"], 2
        )
        self.assertNotIn("checkout_order_id", self.client.session)

    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    @patch("storefront.views.PayPalClient", PayPalDouble)
    def test_cart_update_cancels_awaiting_payment_attempt(self):
        self.add_product()
        self.client.post(reverse("storefront:paypal_create"), self.checkout_data())
        line_id = f"product:{self.product.pk}"

        self.client.post(
            reverse("storefront:cart_update", kwargs={"line_id": line_id}),
            {"quantity": "2"},
        )

        self.assertEqual(Order.objects.get().status, Order.Status.CANCELLED)
        self.assertEqual(self.client.session["cart"][line_id]["quantity"], 2)
        self.assertNotIn("checkout_order_id", self.client.session)

    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    @patch("storefront.views.PayPalClient", PayPalDouble)
    def test_cart_remove_cancels_awaiting_payment_attempt(self):
        self.add_product()
        self.client.post(reverse("storefront:paypal_create"), self.checkout_data())

        self.client.post(
            reverse(
                "storefront:cart_remove",
                kwargs={"line_id": f"product:{self.product.pk}"},
            )
        )

        self.assertEqual(Order.objects.get().status, Order.Status.CANCELLED)
        self.assertEqual(self.client.session["cart"], {})
        self.assertNotIn("checkout_order_id", self.client.session)

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
        checkout_data = self.checkout_data()
        created = self.client.post(reverse("storefront:paypal_create"), self.checkout_data())
        PayPalDouble.fail_capture_once = True

        with self.assertRaisesMessage(RuntimeError, "capture response lost"):
            self.client.post(
                reverse("storefront:paypal_capture"),
                json.dumps({"paypal_order_id": created.json()["paypal_order_id"]}),
                content_type="application/json",
            )
        reconciled = self.client.post(
            reverse("storefront:paypal_create"), checkout_data
        )

        self.assertEqual(reconciled.status_code, 200)
        self.assertIn("redirect_url", reconciled.json())
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Order.objects.get().status, Order.Status.PAID)
        self.assertEqual(self.client.session.get("cart", {}), {})

    @patch("storefront.views.EbayInventoryGateway", InventoryDouble)
    @patch("storefront.views.PayPalClient", PayPalDouble)
    def test_cart_mutation_preserves_ambiguous_payment_session(self):
        self.add_product()
        created = self.client.post(
            reverse("storefront:paypal_create"), self.checkout_data()
        )
        PayPalDouble.fail_capture_once = True
        with self.assertRaisesMessage(RuntimeError, "capture response lost"):
            self.client.post(
                reverse("storefront:paypal_capture"),
                json.dumps({"paypal_order_id": created.json()["paypal_order_id"]}),
                content_type="application/json",
            )
        order = Order.objects.get()
        line_id = f"product:{self.product.pk}"

        response = self.client.post(
            reverse("storefront:cart_update", kwargs={"line_id": line_id}),
            {"quantity": "2"},
        )

        self.assertRedirects(
            response,
            reverse("storefront:order_status", kwargs={"token": order.status_token}),
        )
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CAPTURE_PENDING)
        self.assertEqual(self.client.session["checkout_order_id"], order.pk)
        self.assertEqual(self.client.session["cart"][line_id]["quantity"], 1)

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
        invalid_webhook = self.client.post(
            reverse("storefront:paypal_webhook"),
            "[]",
            content_type="application/json",
        )
        invalid_utf8_capture = self.client.post(
            reverse("storefront:paypal_capture"),
            b"\xff",
            content_type="application/json",
        )
        invalid_utf8_webhook = self.client.post(
            reverse("storefront:paypal_webhook"),
            b"\xff",
            content_type="application/json",
        )
        invalid_quantity = self.client.post(
            reverse("storefront:cart_add", kwargs={"slug": self.product.slug}),
            {"quantity": "not-a-number"},
            follow=True,
        )

        self.assertEqual(invalid_variant.status_code, 302)
        self.assertEqual(invalid_capture.status_code, 400)
        self.assertEqual(invalid_webhook.status_code, 400)
        self.assertEqual(invalid_utf8_capture.status_code, 400)
        self.assertEqual(invalid_utf8_webhook.status_code, 400)
        self.assertContains(invalid_quantity, "Choose a valid quantity.")
        self.assertNotContains(invalid_quantity, "invalid literal")

    def test_oversized_quantity_returns_controlled_error(self):
        response = self.client.post(
            reverse("storefront:cart_add", kwargs={"slug": self.product.slug}),
            {"quantity": "1" * 4301},
            follow=True,
        )

        self.assertContains(response, "Choose a valid quantity.")
        self.assertNotContains(response, "4300 digits")

    @patch("storefront.views.PayPalClient")
    def test_verified_webhook_rejects_missing_shape_and_ignores_unknown_type(
        self, client_class
    ):
        client_class.return_value.__enter__.return_value.verify_webhook_signature.return_value = True
        headers = {
            "HTTP_PAYPAL_AUTH_ALGO": "SHA256withRSA",
            "HTTP_PAYPAL_CERT_URL": "https://api.paypal.com/cert.pem",
            "HTTP_PAYPAL_TRANSMISSION_ID": "TRANSMISSION-1",
            "HTTP_PAYPAL_TRANSMISSION_SIG": "signature",
            "HTTP_PAYPAL_TRANSMISSION_TIME": "2026-07-13T00:00:00Z",
        }

        malformed = self.client.post(
            reverse("storefront:paypal_webhook"),
            json.dumps({}),
            content_type="application/json",
            **headers,
        )
        ignored = self.client.post(
            reverse("storefront:paypal_webhook"),
            json.dumps({"id": "EVENT-2", "event_type": "UNSUBSCRIBED.EVENT"}),
            content_type="application/json",
            **headers,
        )

        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(ignored.status_code, 200)

    @patch("storefront.views.PayPalClient")
    def test_invalid_paypal_webhook_signature_returns_bad_request(self, client_class):
        client_class.return_value.__enter__.return_value.verify_webhook_signature.return_value = False
        response = self.client.post(
            reverse("storefront:paypal_webhook"),
            json.dumps({"id": "EVENT-1", "event_type": "ignored", "resource": {}}),
            content_type="application/json",
            HTTP_PAYPAL_AUTH_ALGO="SHA256withRSA",
            HTTP_PAYPAL_CERT_URL="https://api.paypal.com/cert.pem",
            HTTP_PAYPAL_TRANSMISSION_ID="TRANSMISSION-1",
            HTTP_PAYPAL_TRANSMISSION_SIG="signature",
            HTTP_PAYPAL_TRANSMISSION_TIME="2026-07-13T00:00:00Z",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"], "PayPal webhook signature is invalid."
        )

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
