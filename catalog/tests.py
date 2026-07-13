from pathlib import Path
from xml.etree import ElementTree

import httpx
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from .ebay import EbayInventoryConflict, EbayTradingClient, parse_listing
from .models import InventoryOperation, Product, SyncRun
from .services import set_inventory_quantity, sync_catalog


FIXTURES = Path(__file__).parent / "testdata"


def fixture(name):
    return (FIXTURES / name).read_bytes()


@override_settings(
    EBAY_CLIENT_ID="client",
    EBAY_CLIENT_SECRET="secret",
    EBAY_REFRESH_TOKEN="refresh",
    EBAY_COMPATIBILITY_LEVEL="1423",
    EBAY_TOKEN_ENDPOINT="https://api.example/token",
    EBAY_TRADING_ENDPOINT="https://api.example/trading",
)
class EbayClientTests(SimpleTestCase):
    def test_parse_listing_preserves_catalog_data_and_disables_missing_sku(self):
        listing = parse_listing(ElementTree.fromstring(fixture("get_item.xml")))

        self.assertEqual(listing.item_id, "123456789012")
        self.assertEqual(str(listing.price), "17.00")
        self.assertEqual(listing.quantity, 4)
        self.assertNotIn("<script", listing.description)
        self.assertEqual(listing.item_specifics["Material"], ["Steel", "Rubber"])
        self.assertEqual(listing.shipping["services"][0]["cost"], "4.50")
        self.assertEqual(listing.images[0].url, "https://i.ebayimg.com/images/g/one.jpg")
        self.assertEqual(listing.images[-1].variation_value, "Red")
        self.assertTrue(listing.variations[0].purchasable)
        self.assertFalse(listing.variations[1].purchasable)

    def test_refreshes_oauth_and_paginates_active_inventory(self):
        pages = iter(
            [
                fixture("get_my_ebay_page_1.xml"),
                fixture("get_my_ebay_page_2.xml"),
            ]
        )

        def handler(request):
            if request.url.path == "/token":
                self.assertTrue(request.headers["authorization"].startswith("Basic "))
                return httpx.Response(200, json={"access_token": "access"})
            self.assertEqual(request.headers["x-ebay-api-iaf-token"], "access")
            self.assertEqual(
                request.headers["x-ebay-api-call-name"], "GetMyeBaySelling"
            )
            return httpx.Response(200, content=next(pages))

        with EbayTradingClient(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(client.active_item_ids(), ["111", "222"])

    def test_revises_variation_inventory_and_verifies_get_item(self):
        def handler(request):
            if request.url.path == "/token":
                return httpx.Response(200, json={"access_token": "access"})
            call = request.headers["x-ebay-api-call-name"]
            if call == "ReviseInventoryStatus":
                root = ElementTree.fromstring(request.content)
                namespace = {"e": "urn:ebay:apis:eBLBaseComponents"}
                self.assertEqual(
                    root.findtext("e:InventoryStatus/e:SKU", namespaces=namespace),
                    "RED-01",
                )
                self.assertEqual(
                    root.findtext("e:InventoryStatus/e:Quantity", namespaces=namespace),
                    "2",
                )
                return httpx.Response(
                    200,
                    text=(
                        '<ReviseInventoryStatusResponse xmlns="urn:ebay:apis:'
                        'eBLBaseComponents"><Ack>Success</Ack>'
                        "</ReviseInventoryStatusResponse>"
                    ),
                )
            return httpx.Response(200, content=fixture("get_item.xml"))

        with EbayTradingClient(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(
                client.revise_inventory_status(
                    "123456789012", 2, "inventory-1", "RED-01"
                ),
                2,
            )


class FakeClient:
    def __init__(self, listings, fail_on=""):
        self.listings = {listing.item_id: listing for listing in listings}
        self.fail_on = fail_on

    def verify_seller(self):
        return None

    def active_item_ids(self):
        return list(self.listings)

    def get_item(self, item_id):
        if item_id == self.fail_on:
            raise RuntimeError("detail sync failed")
        return self.listings[item_id]


def product(item_id="999", **overrides):
    values = {
        "ebay_item_id": item_id,
        "slug": f"product-{item_id}",
        "title": "Existing product",
        "price": "10.00",
        "currency": "USD",
        "listing_url": f"https://www.ebay.com/itm/{item_id}",
        "listing_type": "FixedPriceItem",
        "quantity": 3,
        "active": True,
        "last_synced_at": timezone.now(),
    }
    values.update(overrides)
    return Product.objects.create(**values)


@override_settings(
    EBAY_SELLER_USERNAME="fm2k244", EBAY_CHECKOUT_EXCLUDED_ITEMS={"123456789012"}
)
class CatalogSyncTests(TestCase):
    def setUp(self):
        self.listing = parse_listing(ElementTree.fromstring(fixture("get_item.xml")))

    def test_full_sync_imports_variants_images_and_preserves_local_exclusion(self):
        existing = product(
            "123456789012", checkout_excluded=True, title="Locally cached title"
        )
        stale = product("999")

        run = sync_catalog(FakeClient([self.listing]))

        existing.refresh_from_db()
        stale.refresh_from_db()
        self.assertEqual(run.status, SyncRun.Status.SUCCEEDED)
        self.assertEqual(existing.title, "Precision Tool Set")
        self.assertTrue(existing.checkout_excluded)
        self.assertEqual(existing.images.count(), 3)
        self.assertEqual(existing.variants.count(), 2)
        self.assertFalse(existing.variants.get(sku="").purchasable)
        self.assertFalse(stale.active)
        self.assertEqual(stale.quantity, 0)

    def test_detail_failure_never_deactivates_or_partially_updates_catalog(self):
        existing = product("123456789012", title="Original")
        second = self.listing.__class__(
            **{**self.listing.__dict__, "item_id": "222"}
        )

        with self.assertRaisesMessage(RuntimeError, "detail sync failed"):
            sync_catalog(FakeClient([self.listing, second], fail_on="222"))

        existing.refresh_from_db()
        self.assertEqual(existing.title, "Original")
        self.assertTrue(existing.active)
        self.assertEqual(
            SyncRun.objects.latest("started_at").status, SyncRun.Status.FAILED
        )

    def test_sync_does_not_overwrite_inventory_changed_during_get_item(self):
        existing = product("123456789012", quantity=4)
        listing = self.listing.__class__(
            **{**self.listing.__dict__, "variations": (), "quantity": 4}
        )

        class RacingClient(FakeClient):
            def get_item(self, item_id):
                Product.objects.filter(pk=existing.pk).update(
                    quantity=1, last_synced_at=timezone.now()
                )
                return super().get_item(item_id)

        sync_catalog(RacingClient([listing]))

        existing.refresh_from_db()
        self.assertEqual(existing.quantity, 1)

    def test_inventory_operation_is_durable_and_updates_verified_quantity(self):
        current = product("123456789012")

        class InventoryClient:
            def get_item(self, item_id):
                return self.listing

            def revise_inventory_status(self, item_id, quantity, message_id, sku=""):
                self.args = (item_id, quantity, message_id, sku)
                return quantity

        client = InventoryClient()
        client.listing = parse_listing(
            ElementTree.fromstring(fixture("get_item.xml"))
        )
        operation = set_inventory_quantity(
            client,
            product=current,
            expected_quantity=4,
            quantity=1,
            reason=InventoryOperation.Reason.RESERVE,
            idempotency_key="order-1",
        )

        current.refresh_from_db()
        self.assertEqual(client.args, ("123456789012", 1, "order-1", ""))
        self.assertEqual(current.quantity, 1)
        self.assertEqual(operation.status, InventoryOperation.Status.SUCCEEDED)
        self.assertEqual(operation.verified_quantity, 1)

    def test_inventory_retry_recognizes_a_lost_success_without_replaying(self):
        current = product("123456789012", quantity=3)

        class InventoryClient:
            def get_item(self, item_id):
                return self.listing

            def revise_inventory_status(self, *args):
                raise AssertionError("A confirmed quantity must not be replayed")

        client = InventoryClient()
        client.listing = parse_listing(
            ElementTree.fromstring(fixture("get_item.xml"))
        )
        client.listing = client.listing.__class__(
            **{**client.listing.__dict__, "variations": (), "quantity": 1}
        )
        operation = InventoryOperation.objects.create(
            idempotency_key="lost-response",
            product=current,
            reason=InventoryOperation.Reason.RESERVE,
            expected_quantity=3,
            requested_quantity=1,
        )

        result = set_inventory_quantity(
            client,
            product=current,
            expected_quantity=3,
            quantity=1,
            reason=InventoryOperation.Reason.RESERVE,
            idempotency_key="lost-response",
        )

        operation.refresh_from_db()
        current.refresh_from_db()
        self.assertEqual(result, operation)
        self.assertEqual(operation.status, InventoryOperation.Status.SUCCEEDED)
        self.assertEqual(current.quantity, 1)

    def test_new_inventory_operation_never_claims_an_existing_target(self):
        current = product("123456789012", quantity=3)

        class InventoryClient:
            def get_item(self, item_id):
                return self.listing

            def revise_inventory_status(self, *args):
                raise AssertionError("Ambiguous inventory must not be rewritten")

        client = InventoryClient()
        listing = parse_listing(ElementTree.fromstring(fixture("get_item.xml")))
        client.listing = listing.__class__(
            **{**listing.__dict__, "variations": (), "quantity": 1}
        )

        with self.assertRaises(EbayInventoryConflict):
            set_inventory_quantity(
                client,
                product=current,
                expected_quantity=3,
                quantity=1,
                reason=InventoryOperation.Reason.SALE,
                idempotency_key="new-ambiguous-target",
            )

    def test_stale_variations_do_not_hide_a_single_sku_listing(self):
        current = product("123456789012", quantity=2)
        current.variants.create(
            source_key="old",
            sku="OLD",
            title="Old variation",
            price="10.00",
            quantity=0,
            active=False,
        )

        self.assertEqual(current.available_quantity, 2)
        self.assertTrue(current.is_purchasable)
