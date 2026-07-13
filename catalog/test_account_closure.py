import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from orders.models import Order, OrderItem
from storefront.models import StoreSettings

from .account_state import account_closure_notification_id
from .ebay import EbayResponseError, EbayTradingClient
from .models import (
    EbayAccountClosure,
    EbayAccountIdentity,
    InventoryOperation,
    Product,
    SyncRun,
)
from .services import (
    EbayAccountIdentityUnavailable,
    process_ebay_account_closure,
    sync_catalog,
)


@override_settings(EBAY_SELLER_USERNAME="fm2k244")
class EbayAccountClosureTests(TestCase):
    def setUp(self):
        self.state_directory = TemporaryDirectory()
        self.state_override = override_settings(
            EBAY_ACCOUNT_STATE_DIRECTORY=Path(self.state_directory.name)
        )
        self.state_override.enable()
        self.addCleanup(self.state_override.disable)
        self.addCleanup(self.state_directory.cleanup)
        now = timezone.now()
        self.store, _ = StoreSettings.objects.update_or_create(
            pk=1,
            defaults={"flat_shipping_amount": "4.00", "checkout_enabled": True},
        )
        self.product = Product.objects.create(
            ebay_item_id="123456789012",
            slug="precision-tool-set-123456789012",
            title="Precision Tool Set",
            description="Listing description",
            price="19.00",
            currency="USD",
            condition="New",
            listing_url="https://www.ebay.com/itm/123456789012",
            listing_type="FixedPriceItem",
            quantity=2,
            last_synced_at=now,
        )
        self.variant = self.product.variants.create(
            source_key="RED-01",
            sku="RED-01",
            title="Red",
            specifics={"Color": ["Red"]},
            price="19.00",
            quantity=2,
        )
        self.product.images.create(
            url="https://i.ebayimg.com/images/g/one.jpg", position=1
        )
        SyncRun.objects.create(seller_username="fm2k244")
        EbayAccountIdentity.objects.create(
            username="fm2k244", eias_token="seller-eias-token"
        )
        InventoryOperation.objects.create(
            idempotency_key="sale-1",
            product=self.product,
            variant=self.variant,
            reason=InventoryOperation.Reason.SALE,
            expected_quantity=2,
            requested_quantity=1,
        )
        self.order = Order.objects.create(
            checkout_key=uuid.uuid4(),
            checkout_fingerprint="f" * 64,
            customer_email="friend@example.com",
            customer_name="Friend Buyer",
            shipping_line_1="1 Main Street",
            shipping_city="Baltimore",
            shipping_region="MD",
            shipping_postal_code="21201",
            shipping_country_code="US",
            subtotal="19.00",
            shipping_total="4.00",
            total="23.00",
            expires_at=now + timedelta(minutes=30),
        )
        self.item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            variant=self.variant,
            ebay_item_id=self.product.ebay_item_id,
            variation_sku=self.variant.sku,
            variation_title=self.variant.title,
            title=self.product.title,
            condition=self.product.condition,
            image_url=self.product.images.get().url,
            quantity=1,
            unit_price="19.00",
        )

    def test_matching_seller_closes_integration_and_preserves_order_snapshot(self):
        closure = process_ebay_account_closure(
            "notification-1", "FM2K244", "seller-user-id", "seller-eias-token"
        )

        self.store.refresh_from_db()
        self.item.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(closure.notification_id, "notification-1")
        self.assertFalse(self.store.checkout_enabled)
        self.assertFalse(Product.objects.exists())
        self.assertFalse(InventoryOperation.objects.exists())
        self.assertFalse(SyncRun.objects.exists())
        self.assertFalse(EbayAccountIdentity.objects.exists())
        self.assertIsNone(self.item.product_id)
        self.assertIsNone(self.item.variant_id)
        self.assertEqual(self.item.ebay_item_id, "")
        self.assertEqual(self.item.variation_sku, "")
        self.assertEqual(self.item.image_url, "")
        self.assertEqual(self.item.title, "Precision Tool Set")
        self.assertEqual(self.item.variation_title, "Red")
        self.assertEqual(self.item.condition, "New")
        self.assertEqual(self.item.quantity, 1)
        self.assertEqual(self.item.unit_price, Decimal("19.00"))
        self.assertEqual(self.order.total, Decimal("23.00"))

    def test_unrelated_account_is_ignored(self):
        result = process_ebay_account_closure(
            "notification-2", "another-user", "another-id", "another-eias-token"
        )

        self.store.refresh_from_db()
        self.assertIsNone(result)
        self.assertTrue(self.store.checkout_enabled)
        self.assertTrue(Product.objects.exists())
        self.assertFalse(EbayAccountClosure.objects.exists())

    def test_unclassifiable_modern_notification_is_not_acknowledged(self):
        EbayAccountIdentity.objects.all().delete()

        with self.assertRaisesMessage(
            EbayAccountIdentityUnavailable,
            "Stable eBay seller identity has not been recorded.",
        ):
            process_ebay_account_closure(
                "notification-unclassified",
                "immutable-user-id",
                "immutable-user-id",
                "unknown-eias-token",
            )

        self.assertTrue(Product.objects.exists())
        self.assertFalse(EbayAccountClosure.objects.exists())

    def test_duplicate_notification_is_idempotent(self):
        first = process_ebay_account_closure(
            "notification-3", "fm2k244", "seller-user-id", "seller-eias-token"
        )
        second = process_ebay_account_closure(
            "notification-3", "immutable-id", "immutable-id", "seller-eias-token"
        )

        self.assertEqual(first, second)
        self.assertEqual(EbayAccountClosure.objects.count(), 1)

    def test_modern_immutable_username_matches_stable_eias_token(self):
        closure = process_ebay_account_closure(
            "notification-modern",
            "immutable-user-id",
            "immutable-user-id",
            "seller-eias-token",
        )

        self.assertEqual(closure.notification_id, "notification-modern")
        self.assertFalse(Product.objects.exists())

    def test_external_marker_reapplies_closure_after_database_restore(self):
        process_ebay_account_closure(
            "notification-restored",
            "fm2k244",
            "seller-user-id",
            "seller-eias-token",
        )
        EbayAccountClosure.objects.all().delete()
        StoreSettings.objects.filter(pk=1).update(checkout_enabled=True)
        Product.objects.create(
            ebay_item_id="restored-item",
            slug="restored-item",
            title="Restored item",
            price="10.00",
            listing_url="https://www.ebay.com/itm/restored-item",
            listing_type="FixedPriceItem",
            quantity=1,
            last_synced_at=timezone.now(),
        )

        call_command("enforce_ebay_account_closure")

        self.assertEqual(
            EbayAccountClosure.objects.get().notification_id, "notification-restored"
        )
        self.assertFalse(Product.objects.exists())
        self.assertFalse(StoreSettings.objects.get(pk=1).checkout_enabled)

    def test_database_closure_seeds_external_marker(self):
        EbayAccountClosure.objects.create(notification_id="notification-database")

        call_command("enforce_ebay_account_closure")

        self.assertEqual(
            account_closure_notification_id(), "notification-database"
        )
        self.assertFalse(Product.objects.exists())

    def test_closed_account_prevents_new_ebay_clients(self):
        EbayAccountClosure.objects.create(notification_id="notification-4")

        with self.assertRaisesMessage(
            ImproperlyConfigured, "The eBay seller account is closed."
        ):
            EbayTradingClient()

    def test_closed_account_prevents_catalog_sync(self):
        EbayAccountClosure.objects.create(notification_id="notification-5")
        sync_run_count = SyncRun.objects.count()

        class Client:
            def verify_seller(self):
                raise AssertionError("Closed account must not call eBay")

        with self.assertRaisesMessage(
            EbayResponseError, "The eBay seller account is closed."
        ):
            sync_catalog(Client())

        self.assertEqual(SyncRun.objects.count(), sync_run_count)
