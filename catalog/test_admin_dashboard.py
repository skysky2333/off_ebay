import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import RequestFactory
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from orders.models import Order, Refund, Shipment
from storefront.models import StoreSettings

from .models import InventoryOperation, Product, SyncRun


@override_settings(
    EBAY_CLIENT_ID="ebay-client",
    EBAY_CLIENT_SECRET="ebay-secret",
    EBAY_REFRESH_TOKEN="ebay-refresh",
    EBAY_COMPATIBILITY_LEVEL="1423",
)
class AdminDashboardTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            "dashboard-operator", "operator@example.com", "admin-test-password"
        )
        self.client.force_login(self.user)

    def order(self, currency="USD"):
        key = uuid.uuid4().hex
        return Order.objects.create(
            checkout_key=uuid.uuid4(),
            checkout_fingerprint=key,
            status=Order.Status.PAID,
            customer_email=f"{key}@example.com",
            customer_name="Ada Buyer",
            shipping_line_1="1 Main Street",
            shipping_city="Baltimore",
            shipping_region="MD",
            shipping_postal_code="21201",
            shipping_country_code="US",
            currency=currency,
            subtotal=Decimal("10.00"),
            shipping_total=Decimal("0.00"),
            total=Decimal("10.00"),
            expires_at=timezone.now() + timedelta(minutes=30),
            paid_at=timezone.now(),
        )

    def test_admin_index_shows_operational_state_and_recent_orders(self):
        order = self.order()
        SyncRun.objects.create(
            status=SyncRun.Status.SUCCEEDED,
            indexed_count=1,
            imported_count=1,
            seller_username="fm2k244",
            completed_at=timezone.now(),
        )

        response = self.client.get(reverse("admin:index"))

        self.assertContains(response, "Operations")
        self.assertContains(response, "Needs fulfillment")
        self.assertContains(response, order.reference)
        self.assertContains(response, "Sync catalog now")
        self.assertContains(response, "Succeeded")
        self.assertContains(response, ">Open</a>")
        self.assertNotContains(response, 'class="changelink"')

    def test_fulfillment_queue_includes_only_unshipped_partial_refunds(self):
        paid = self.order()
        partial = self.order()
        shipped_partial = self.order()
        split = self.order()
        Order.objects.filter(pk__in={partial.pk, shipped_partial.pk}).update(
            status=Order.Status.PARTIALLY_REFUNDED,
            refunded_total=Decimal("5.00"),
        )
        Order.objects.filter(pk=shipped_partial.pk).update(shipped_at=timezone.now())
        Shipment.objects.create(
            order=shipped_partial,
            carrier="USPS",
            tracking_number="FINAL-PARTIAL",
            status=Shipment.Status.SHIPPED,
        )
        Order.objects.filter(pk=split.pk).update(
            status=Order.Status.SHIPPED, shipped_at=timezone.now()
        )
        Shipment.objects.create(
            order=split,
            carrier="USPS",
            tracking_number="SPLIT-SHIPPED",
            status=Shipment.Status.SHIPPED,
        )
        Shipment.objects.create(
            order=split,
            carrier="USPS",
            tracking_number="SPLIT-LABEL",
            status=Shipment.Status.LABEL_CREATED,
        )

        dashboard = self.client.get(reverse("admin:index"))
        queue = self.client.get(
            reverse("admin:orders_order_changelist"), {"fulfillment": "needed"}
        )

        self.assertContains(dashboard, "?fulfillment=needed")
        self.assertContains(queue, paid.reference)
        self.assertContains(queue, partial.reference)
        self.assertContains(queue, split.reference)
        self.assertNotContains(queue, shipped_partial.reference)

    def test_dashboard_marks_an_old_successful_sync_as_stale(self):
        run = SyncRun.objects.create(
            status=SyncRun.Status.SUCCEEDED,
            seller_username="fm2k244",
            completed_at=timezone.now(),
        )
        SyncRun.objects.filter(pk=run.pk).update(
            completed_at=timezone.now() - timedelta(hours=1)
        )

        response = self.client.get(reverse("admin:index"))

        self.assertContains(response, "Stale")
        self.assertNotContains(response, "operations-state--succeeded\">Succeeded")

    def test_dashboard_marks_an_abandoned_running_sync_as_stalled(self):
        run = SyncRun.objects.create(seller_username="fm2k244")
        SyncRun.objects.filter(pk=run.pk).update(
            started_at=timezone.now() - timedelta(hours=1)
        )

        response = self.client.get(reverse("admin:index"))

        self.assertContains(response, "Stalled")
        self.assertContains(response, "operations-state--stalled")
        self.assertContains(response, "Sync catalog now")

    @override_settings(
        EBAY_CLIENT_ID="",
        EBAY_CLIENT_SECRET="",
        EBAY_REFRESH_TOKEN="",
        EBAY_COMPATIBILITY_LEVEL="",
    )
    def test_dashboard_explains_when_sync_setup_is_missing(self):
        response = self.client.get(reverse("admin:index"))

        self.assertContains(response, "Setup required")
        self.assertContains(response, "Configure the eBay credentials")
        self.assertNotContains(response, "Sync catalog now")

    def test_dashboard_surfaces_all_refunds_requiring_review(self):
        order = self.order()
        for status in (
            Refund.Status.PENDING,
            Refund.Status.FAILED,
            Refund.Status.CANCELLED,
        ):
            Refund.objects.create(
                order=order,
                paypal_refund_id=f"REFUND-{status}",
                amount=Decimal("10.00"),
                status=status,
            )
        resolved = self.order()
        resolved.status = Order.Status.REFUNDED
        resolved.refunded_total = resolved.total
        resolved.save(update_fields=("status", "refunded_total", "updated_at"))
        Refund.objects.create(
            order=resolved,
            paypal_refund_id="REFUND-RESOLVED-FAILURE",
            amount=resolved.total,
            status=Refund.Status.FAILED,
        )

        response = self.client.get(reverse("admin:index"))
        review = self.client.get(
            reverse("admin:orders_refund_changelist"),
            {"refund_review": "needed"},
        )

        self.assertContains(response, "Refund review")
        self.assertContains(response, "2 unsuccessful")
        self.assertContains(
            response,
            f'{reverse("admin:orders_refund_changelist")}?refund_review=needed',
        )
        self.assertEqual(review.status_code, 200)
        self.assertContains(review, "REFUND-pending")
        self.assertContains(review, "REFUND-failed")
        self.assertContains(review, "REFUND-cancelled")
        self.assertNotContains(review, "REFUND-RESOLVED-FAILURE")

    def test_payment_review_metric_links_to_a_working_filtered_queue(self):
        review = self.order()
        review.status = Order.Status.FUNDING_RETRY
        review.save(update_fields=("status", "updated_at"))
        ordinary = self.order()

        dashboard = self.client.get(reverse("admin:index"))
        queue = self.client.get(
            reverse("admin:orders_order_changelist"),
            {"payment_review": "needed"},
        )

        self.assertContains(dashboard, "?payment_review=needed")
        self.assertEqual(queue.status_code, 200)
        self.assertContains(queue, review.reference)
        self.assertNotContains(queue, ordinary.reference)

    def test_pending_refund_is_not_counted_as_needing_fulfillment(self):
        order = self.order()
        Refund.objects.create(
            order=order,
            paypal_refund_id="REFUND-PENDING",
            amount=order.total,
            status=Refund.Status.PENDING,
        )

        queue = self.client.get(
            reverse("admin:orders_order_changelist"), {"fulfillment": "needed"}
        )

        self.assertNotContains(queue, order.reference)

    def test_dashboard_and_sync_respect_model_permissions(self):
        order = self.order()
        limited_user = get_user_model().objects.create_user(
            "limited-operator",
            "limited@example.com",
            "admin-test-password",
            is_staff=True,
        )
        self.client.force_login(limited_user)

        dashboard = self.client.get(reverse("admin:index"))
        sync = self.client.post(reverse("admin:catalog_syncrun_sync"))

        self.assertEqual(dashboard.status_code, 200)
        self.assertNotContains(dashboard, order.reference)
        self.assertNotContains(dashboard, "Sync catalog now")
        self.assertEqual(sync.status_code, 403)

    def test_dashboard_treats_change_permission_as_view_access(self):
        order = self.order()
        Refund.objects.create(
            order=order,
            paypal_refund_id="REFUND-PENDING",
            amount=order.total,
            status=Refund.Status.PENDING,
        )
        SyncRun.objects.create(
            status=SyncRun.Status.SUCCEEDED,
            seller_username="fm2k244",
            completed_at=timezone.now(),
        )
        operator = get_user_model().objects.create_user(
            "change-operator",
            "change@example.com",
            "admin-test-password",
            is_staff=True,
        )
        operator.user_permissions.add(
            Permission.objects.get(codename="change_order"),
            Permission.objects.get(codename="change_refund"),
            Permission.objects.get(codename="change_syncrun"),
        )
        self.client.force_login(operator)

        response = self.client.get(reverse("admin:index"))

        self.assertContains(response, order.reference)
        self.assertContains(response, "Refund review")
        self.assertContains(response, "Catalog sync")
        self.assertContains(response, "Sync catalog now")

    def test_read_only_store_viewer_sees_checkout_metric(self):
        viewer = get_user_model().objects.create_user(
            "store-viewer",
            "store-viewer@example.com",
            "admin-test-password",
            is_staff=True,
        )
        viewer.user_permissions.add(
            Permission.objects.get(codename="view_storesettings")
        )
        self.client.force_login(viewer)

        response = self.client.get(reverse("admin:index"))

        self.assertContains(response, "<span>Checkout</span>", html=True)
        self.assertContains(
            response,
            reverse("admin:storefront_storesettings_change", args=[1]),
        )

    def test_missing_store_settings_links_authorized_operator_to_creation(self):
        StoreSettings.objects.all().delete()

        dashboard = self.client.get(reverse("admin:index"))
        add_url = reverse("admin:storefront_storesettings_add")

        self.assertContains(dashboard, f'href="{add_url}"')
        self.assertEqual(self.client.get(add_url).status_code, 200)

    def test_store_settings_explain_shipping_and_effective_checkout(self):
        response = self.client.get(
            reverse("admin:storefront_storesettings_change", args=[1])
        )

        self.assertContains(response, "Flat shipping amount (USD)")
        self.assertContains(response, "Charged once per order")
        self.assertContains(response, "Checkout opens only when eBay, PayPal")

    def test_dashboard_labels_order_currency_and_mobile_cells(self):
        order = self.order(currency="EUR")

        response = self.client.get(reverse("admin:index"))

        self.assertContains(response, "EUR 10.00")
        self.assertNotContains(response, "$10.00")
        self.assertContains(response, 'data-label="Order"')
        self.assertContains(response, 'data-label="Total"')

    @patch("catalog.admin.sync_catalog")
    @patch("catalog.admin.EbayTradingClient")
    def test_manual_sync_is_post_only_and_uses_catalog_service(
        self, client_class, sync_catalog
    ):
        sync_url = reverse("admin:catalog_syncrun_sync")
        ebay_client = client_class.return_value.__enter__.return_value
        sync_catalog.return_value = SimpleNamespace(
            imported_count=3, deactivated_count=1
        )

        self.assertEqual(self.client.get(sync_url).status_code, 405)
        response = self.client.post(sync_url, follow=True)

        sync_catalog.assert_called_once_with(ebay_client)
        self.assertContains(response, "Catalog sync completed: 3 imported, 1 deactivated.")

    def test_product_admin_prioritizes_store_control_and_real_links(self):
        product = Product.objects.create(
            ebay_item_id="123456789012",
            slug="clean-product-123456789012",
            title="Clean product",
            price=Decimal("25.00"),
            currency="USD",
            listing_url="https://www.ebay.com/itm/123456789012",
            listing_type="FixedPriceItem",
            quantity=1,
            last_synced_at=timezone.now(),
        )

        response = self.client.get(
            reverse("admin:catalog_product_change", args=[product.pk])
        )

        self.assertContains(response, "Store availability")
        self.assertContains(response, "Customer content")
        self.assertNotContains(response, "Product variants")
        self.assertContains(response, "Product images")
        self.assertContains(response, "Immediately remove this listing")
        self.assertContains(
            response,
            reverse("storefront:product_detail", kwargs={"slug": product.slug}),
        )
        self.assertContains(response, product.listing_url)
        self.assertLess(
            response.content.index(b'name="checkout_excluded"'),
            response.content.index(b">Listing</h2>"),
        )
        self.assertLess(
            response.content.index(b">Listing</h2>"),
            response.content.index(b">Customer content</h2>"),
        )

        product.checkout_excluded = True
        product.save(update_fields=("checkout_excluded", "updated_at"))
        unavailable = self.client.get(
            reverse("admin:catalog_product_change", args=[product.pk])
        )

        self.assertContains(unavailable, "Not available in store")
        self.assertNotContains(
            unavailable,
            reverse("storefront:product_detail", kwargs={"slug": product.slug}),
        )

        product.variants.create(
            source_key="RED",
            sku="RED",
            title="Red",
            specifics={"Color": ["Red"]},
            price=product.price,
            quantity=1,
        )
        with_variant = self.client.get(
            reverse("admin:catalog_product_change", args=[product.pk])
        )

        self.assertContains(with_variant, "Product variants")

    def test_catalog_audit_records_are_view_only(self):
        product = Product.objects.create(
            ebay_item_id="123456789012",
            slug="clean-product-123456789012",
            title="Clean product",
            price=Decimal("25.00"),
            listing_url="https://www.ebay.com/itm/123456789012",
            listing_type="FixedPriceItem",
            quantity=1,
            last_synced_at=timezone.now(),
        )
        sync = SyncRun.objects.create(seller_username="fm2k244")
        operation = InventoryOperation.objects.create(
            idempotency_key="admin-read-only",
            product=product,
            reason=InventoryOperation.Reason.RECONCILE,
            expected_quantity=1,
            requested_quantity=1,
        )
        request = RequestFactory().get("/admin/")
        request.user = self.user

        for model, instance in ((SyncRun, sync), (InventoryOperation, operation)):
            with self.subTest(model=model.__name__):
                model_admin = admin.site._registry[model]
                self.assertFalse(model_admin.has_change_permission(request, instance))
                response = self.client.get(
                    reverse(
                        f"admin:catalog_{model._meta.model_name}_change",
                        args=[instance.pk],
                    )
                )
                self.assertNotContains(response, 'name="_save"')
