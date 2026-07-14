from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree

import httpx
from django.db.models import Prefetch
from django.test import TestCase, override_settings
from django.utils import timezone

from .ebay import (
    EbayInventoryConflict,
    EbayResponseError,
    EbayTradingClient,
    EbayUserIdentity,
    parse_listing,
)
from .models import InventoryOperation, Product, ProductVariant, SyncRun
from .services import process_ebay_account_closure, set_inventory_quantity, sync_catalog


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
class EbayClientTests(TestCase):
    def test_parse_listing_preserves_catalog_data_and_sku_less_variations(self):
        listing = parse_listing(ElementTree.fromstring(fixture("get_item.xml")))

        self.assertEqual(listing.item_id, "123456789012")
        self.assertEqual(listing.listing_status, "Active")
        self.assertEqual(str(listing.price), "17.00")
        self.assertEqual(listing.quantity, 4)
        self.assertNotIn("<script", listing.description)
        self.assertEqual(listing.item_specifics["Material"], ["Steel", "Rubber"])
        self.assertEqual(listing.shipping["services"][0]["cost"], "4.50")
        self.assertEqual(listing.images[0].url, "https://i.ebayimg.com/images/g/one.jpg")
        self.assertEqual(listing.images[-1].variation_value, "Red")
        self.assertEqual(listing.variations[0].title, "Red")
        self.assertTrue(listing.variations[0].purchasable)
        self.assertTrue(listing.variations[1].purchasable)
        self.assertTrue(listing.variations[1].source_key.startswith("missing-"))

    def test_parse_listing_uses_the_lowest_available_purchasable_variant_price(self):
        root = ElementTree.fromstring(fixture("get_item.xml"))
        namespace = {"e": "urn:ebay:apis:eBLBaseComponents"}
        variations = root.findall(".//e:Variations/e:Variation", namespace)
        variations[1].insert(0, ElementTree.Element(f"{{{namespace['e']}}}SKU"))
        variations[1].find("e:SKU", namespace).text = "BLUE-01"
        variations[1].find("e:SellingStatus/e:QuantitySold", namespace).text = "2"

        listing = parse_listing(root)

        self.assertEqual(listing.price, Decimal("19.00"))
        self.assertEqual(listing.variations[1].quantity, 0)
        self.assertTrue(listing.variations[1].purchasable)

    def test_parse_listing_labels_multi_dimension_variations(self):
        root = ElementTree.fromstring(fixture("get_item.xml"))
        namespace = {"e": "urn:ebay:apis:eBLBaseComponents"}
        specifics = root.find(
            ".//e:Variations/e:Variation/e:VariationSpecifics", namespace
        )
        entry = ElementTree.SubElement(
            specifics, f"{{{namespace['e']}}}NameValueList"
        )
        ElementTree.SubElement(entry, f"{{{namespace['e']}}}Name").text = "Size"
        ElementTree.SubElement(entry, f"{{{namespace['e']}}}Value").text = "Large"

        listing = parse_listing(root)

        self.assertEqual(
            listing.variations[0].title, "Color: Red / Size: Large"
        )

    def test_parse_listing_rejects_duplicate_variation_identity(self):
        root = ElementTree.fromstring(fixture("get_item.xml"))
        namespace = {"e": "urn:ebay:apis:eBLBaseComponents"}
        variations = root.findall(".//e:Variations/e:Variation", namespace)
        sku = ElementTree.Element(f"{{{namespace['e']}}}SKU")
        sku.text = "RED-01"
        variations[1].insert(0, sku)

        with self.assertRaisesMessage(
            EbayResponseError, "duplicate variation identity"
        ):
            parse_listing(root)

    def test_parse_listing_rejects_invalid_prices(self):
        namespace = {"e": "urn:ebay:apis:eBLBaseComponents"}
        for value in ("not-money", "NaN", "Infinity", "-1.00"):
            with self.subTest(value=value):
                root = ElementTree.fromstring(fixture("get_item.xml"))
                root.find(".//e:StartPrice", namespace).text = value

                with self.assertRaisesMessage(
                    EbayResponseError, "eBay response has invalid e:StartPrice amount"
                ):
                    parse_listing(root)

    def test_parse_listing_rejects_invalid_dispatch_time(self):
        namespace = {"e": "urn:ebay:apis:eBLBaseComponents"}
        for value in ("-1", "1.5", "soon", "\u0662"):
            with self.subTest(value=value):
                root = ElementTree.fromstring(fixture("get_item.xml"))
                root.find(".//e:DispatchTimeMax", namespace).text = value

                with self.assertRaisesMessage(
                    EbayResponseError, "eBay response has invalid DispatchTimeMax"
                ):
                    parse_listing(root)

    def test_parse_listing_constrains_description_markup(self):
        root = ElementTree.fromstring(fixture("get_item.xml"))
        namespace = {"e": "urn:ebay:apis:eBLBaseComponents"}
        root.find(".//e:Description", namespace).text = (
            '<h1>Imported title</h1><table><tr><td>Size</td></tr></table>'
            '<pre>fixed</pre><img src="http://example.com/insecure.jpg" width="2000">'
            '<img src="https://example.com/safe.jpg" width="2000" alt="Detail">'
            '<a href="javascript:alert(1)">unsafe link</a><script>bad()</script>'
        )

        description = parse_listing(root).description

        self.assertNotIn("<h1", description)
        self.assertNotIn("<script", description)
        self.assertNotIn("bad()", description)
        self.assertNotIn("width=", description)
        self.assertNotIn("http://example.com", description)
        self.assertNotIn("javascript:", description)
        self.assertIn("<table>", description)
        self.assertIn("<pre>fixed</pre>", description)
        self.assertNotIn("<img", description)

    def test_variant_title_is_limited_to_the_catalog_field(self):
        root = ElementTree.fromstring(fixture("get_item.xml"))
        namespace = {"e": "urn:ebay:apis:eBLBaseComponents"}
        value = root.find(
            ".//e:Variations/e:Variation/e:VariationSpecifics/"
            "e:NameValueList/e:Value",
            namespace,
        )
        value.text = "R" * 300

        listing = parse_listing(root)

        self.assertEqual(listing.variations[0].title, "R" * 255)

    def test_parse_listing_rejects_variation_currency_mismatch(self):
        root = ElementTree.fromstring(fixture("get_item.xml"))
        namespace = {"e": "urn:ebay:apis:eBLBaseComponents"}
        price = root.find(".//e:Variations/e:Variation/e:StartPrice", namespace)
        price.set("currencyID", "EUR")

        with self.assertRaisesMessage(
            EbayResponseError,
            "eBay variation currency EUR does not match listing currency USD",
        ):
            parse_listing(root)

    def test_parse_listing_rejects_non_https_picture_urls(self):
        namespace = {"e": "urn:ebay:apis:eBLBaseComponents"}
        for url in ("http://example.com/image.jpg", "javascript:alert(1)", "https://"):
            with self.subTest(url=url):
                root = ElementTree.fromstring(fixture("get_item.xml"))
                root.find(".//e:PictureDetails/e:PictureURL", namespace).text = url

                with self.assertRaisesMessage(
                    EbayResponseError, "eBay response has invalid HTTPS image URL"
                ):
                    parse_listing(root)

    def test_parse_listing_rejects_non_https_listing_urls(self):
        namespace = {"e": "urn:ebay:apis:eBLBaseComponents"}
        root = ElementTree.fromstring(fixture("get_item.xml"))
        root.find(".//e:ListingDetails/e:ViewItemURL", namespace).text = (
            "javascript:alert(1)"
        )

        with self.assertRaisesMessage(
            EbayResponseError, "eBay response has invalid HTTPS listing URL"
        ):
            parse_listing(root)

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

    @override_settings(EBAY_SELLER_USERNAME="fm2k244")
    def test_verifies_and_returns_stable_seller_identity(self):
        def handler(request):
            if request.url.path == "/token":
                return httpx.Response(200, json={"access_token": "access"})
            call = request.headers["x-ebay-api-call-name"]
            if call == "GetUser":
                return httpx.Response(
                    200,
                    text=(
                        '<GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
                        "<Ack>Success</Ack><User><UserID>fm2k244</UserID>"
                        "<EIASToken>seller-eias-token</EIASToken></User>"
                        "</GetUserResponse>"
                    ),
                )
            return httpx.Response(
                200,
                text=(
                    '<GetUserPreferencesResponse xmlns="urn:ebay:apis:'
                    'eBLBaseComponents"><Ack>Success</Ack>'
                    "<OutOfStockControlPreference>true</OutOfStockControlPreference>"
                    "</GetUserPreferencesResponse>"
                ),
            )

        with EbayTradingClient(transport=httpx.MockTransport(handler)) as client:
            identity = client.verify_seller()

        self.assertEqual(
            identity, EbayUserIdentity("fm2k244", "seller-eias-token")
        )

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

    def test_revises_sku_less_variation_inventory_by_specifics(self):
        listing = parse_listing(ElementTree.fromstring(fixture("get_item.xml")))
        option = listing.variations[1]

        def handler(request):
            if request.url.path == "/token":
                return httpx.Response(200, json={"access_token": "access"})
            call = request.headers["x-ebay-api-call-name"]
            if call == "ReviseFixedPriceItem":
                root = ElementTree.fromstring(request.content)
                namespace = {"e": "urn:ebay:apis:eBLBaseComponents"}
                self.assertEqual(
                    root.findtext("e:MessageID", namespaces=namespace),
                    "inventory-skuless-1",
                )
                self.assertEqual(
                    root.findtext(
                        "e:Item/e:Variations/e:Variation/e:StartPrice",
                        namespaces=namespace,
                    ),
                    "17.00",
                )
                self.assertEqual(
                    root.find(
                        "e:Item/e:Variations/e:Variation/e:StartPrice",
                        namespace,
                    ).attrib["currencyID"],
                    "USD",
                )
                self.assertEqual(
                    root.findtext(
                        "e:Item/e:Variations/e:Variation/e:Quantity",
                        namespaces=namespace,
                    ),
                    "1",
                )
                self.assertEqual(
                    root.findtext(
                        "e:Item/e:Variations/e:Variation/e:VariationSpecifics/"
                        "e:NameValueList/e:Name",
                        namespaces=namespace,
                    ),
                    "Color",
                )
                return httpx.Response(
                    200,
                    text=(
                        '<ReviseFixedPriceItemResponse xmlns="urn:ebay:apis:'
                        'eBLBaseComponents"><Ack>Success</Ack>'
                        "</ReviseFixedPriceItemResponse>"
                    ),
                )
            root = ElementTree.fromstring(fixture("get_item.xml"))
            namespace = {"e": "urn:ebay:apis:eBLBaseComponents"}
            variations = root.findall(".//e:Variations/e:Variation", namespace)
            variations[1].find("e:Quantity", namespace).text = "1"
            return httpx.Response(200, content=ElementTree.tostring(root))

        with EbayTradingClient(transport=httpx.MockTransport(handler)) as client:
            verified = client.revise_variation_inventory(
                listing.item_id,
                1,
                "inventory-skuless-1",
                option.source_key,
                option.specifics,
                option.price,
                listing.currency,
            )

        self.assertEqual(verified, 1)


class FakeClient:
    def __init__(self, listings, fail_on=""):
        self.listings = {listing.item_id: listing for listing in listings}
        self.fail_on = fail_on

    def verify_seller(self):
        return EbayUserIdentity("fm2k244", "seller-eias-token")

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
        self.state_directory = TemporaryDirectory()
        self.state_override = override_settings(
            EBAY_ACCOUNT_STATE_DIRECTORY=Path(self.state_directory.name)
        )
        self.state_override.enable()
        self.addCleanup(self.state_override.disable)
        self.addCleanup(self.state_directory.cleanup)
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
        self.assertTrue(existing.variants.get(sku="").purchasable)
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

    def test_account_closure_during_hydration_prevents_catalog_recreation(self):
        product("123456789012")

        class ClosingClient(FakeClient):
            def get_item(self, item_id):
                process_ebay_account_closure(
                    "notification-during-sync",
                    "fm2k244",
                    "seller-user-id",
                    "seller-eias-token",
                )
                return super().get_item(item_id)

        with self.assertRaisesMessage(
            EbayResponseError, "The eBay seller account is closed."
        ):
            sync_catalog(ClosingClient([self.listing]))

        self.assertFalse(Product.objects.exists())
        self.assertFalse(SyncRun.objects.exists())

    def test_sync_rejects_unsafe_image_before_persistence(self):
        existing = product("123456789012")
        existing.images.create(url="https://example.com/original.jpg", position=1)
        root = ElementTree.fromstring(fixture("get_item.xml"))
        namespace = {"e": "urn:ebay:apis:eBLBaseComponents"}
        root.find(".//e:PictureDetails/e:PictureURL", namespace).text = (
            "javascript:alert(1)"
        )

        class UnsafeImageClient(FakeClient):
            def get_item(self, item_id):
                return parse_listing(root)

        with self.assertRaisesMessage(
            EbayResponseError, "eBay response has invalid HTTPS image URL"
        ):
            sync_catalog(UnsafeImageClient([self.listing]))

        existing.refresh_from_db()
        self.assertEqual(
            list(existing.images.values_list("url", flat=True)),
            ["https://example.com/original.jpg"],
        )
        self.assertEqual(
            SyncRun.objects.latest("started_at").status, SyncRun.Status.FAILED
        )

    def test_sync_deactivates_listing_that_is_no_longer_active(self):
        existing = product("123456789012")
        ended = self.listing.__class__(
            **{**self.listing.__dict__, "listing_status": "Ended"}
        )

        run = sync_catalog(FakeClient([ended]))

        existing.refresh_from_db()
        self.assertFalse(existing.active)
        self.assertEqual(existing.quantity, 0)
        self.assertEqual(run.imported_count, 0)
        self.assertEqual(run.deactivated_count, 1)

    def test_sync_does_not_overwrite_newer_listing_state(self):
        existing = product("123456789012", quantity=4)
        existing.images.create(url="https://example.com/old.jpg", position=1)
        variant = existing.variants.create(
            source_key="RED-01",
            sku="RED-01",
            title="Old option",
            price="10.00",
            quantity=4,
        )

        class RacingClient(FakeClient):
            def get_item(self, item_id):
                Product.objects.filter(pk=existing.pk).update(
                    title="Newer title",
                    price="99.00",
                    quantity=1,
                    active=False,
                    last_synced_at=timezone.now(),
                )
                existing.images.update(url="https://example.com/newer.jpg")
                existing.variants.filter(pk=variant.pk).update(
                    title="Newer option",
                    price="99.00",
                    quantity=1,
                    active=False,
                )
                return super().get_item(item_id)

        sync_catalog(RacingClient([self.listing]))

        existing.refresh_from_db()
        variant.refresh_from_db()
        self.assertEqual(existing.title, "Newer title")
        self.assertEqual(existing.price, Decimal("99.00"))
        self.assertEqual(existing.quantity, 1)
        self.assertFalse(existing.active)
        self.assertEqual(
            list(existing.images.values_list("url", flat=True)),
            ["https://example.com/newer.jpg"],
        )
        self.assertEqual(existing.variants.count(), 1)
        self.assertEqual(variant.title, "Newer option")
        self.assertEqual(variant.price, Decimal("99.00"))
        self.assertEqual(variant.quantity, 1)
        self.assertFalse(variant.active)

    def test_older_run_does_not_deactivate_product_refreshed_after_it_started(self):
        existing = product("123456789012")

        class RacingClient(FakeClient):
            def active_item_ids(self):
                Product.objects.filter(pk=existing.pk).update(
                    last_synced_at=timezone.now()
                )
                return []

        run = sync_catalog(RacingClient([]))

        existing.refresh_from_db()
        self.assertTrue(existing.active)
        self.assertEqual(existing.quantity, 3)
        self.assertEqual(run.deactivated_count, 0)

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

    def test_variant_inventory_update_recomputes_available_product_price(self):
        current = product("123456789012", price="19.00", quantity=4)
        listing = self.listing
        red, unavailable = listing.variations
        blue = unavailable.__class__(
            **{
                **unavailable.__dict__,
                "sku": "BLUE-01",
                "purchasable": True,
            }
        )
        listing = listing.__class__(
            **{**listing.__dict__, "variations": (red, blue)}
        )
        red_variant = current.variants.create(
            source_key=red.source_key,
            sku=red.sku,
            title=red.title,
            price=red.price,
            quantity=red.quantity,
        )
        current.variants.create(
            source_key=blue.source_key,
            sku=blue.sku,
            title=blue.title,
            price=blue.price,
            quantity=blue.quantity,
        )

        class InventoryClient:
            def get_item(self, item_id):
                return listing

            def revise_inventory_status(self, item_id, quantity, message_id, sku=""):
                return quantity

        set_inventory_quantity(
            InventoryClient(),
            product=current,
            variant=red_variant,
            expected_quantity=red.quantity,
            quantity=0,
            reason=InventoryOperation.Reason.SALE,
            idempotency_key="sell-cheapest-variant",
        )

        current.refresh_from_db()
        self.assertEqual(current.price, blue.price)
        self.assertEqual(current.quantity, blue.quantity)

    def test_sku_less_variant_inventory_uses_specifics_update(self):
        current = product("123456789012", price="17.00", quantity=4)
        option = self.listing.variations[1]
        variant = current.variants.create(
            source_key=option.source_key,
            sku=option.sku,
            title=option.title,
            specifics=option.specifics,
            price=option.price,
            quantity=option.quantity,
            purchasable=True,
        )

        class InventoryClient:
            args = None

            def get_item(self, item_id):
                return self.listing

            def revise_inventory_status(self, *args):
                raise AssertionError("SKU-less inventory must use variation specifics")

            def revise_variation_inventory(
                self,
                item_id,
                quantity,
                message_id,
                source_key,
                specifics,
                price,
                currency,
            ):
                self.args = (
                    item_id,
                    quantity,
                    message_id,
                    source_key,
                    specifics,
                    price,
                    currency,
                )
                return quantity

        client = InventoryClient()
        client.listing = self.listing
        set_inventory_quantity(
            client,
            product=current,
            variant=variant,
            expected_quantity=option.quantity,
            quantity=1,
            reason=InventoryOperation.Reason.SALE,
            idempotency_key="sell-skuless-variant",
        )

        variant.refresh_from_db()
        self.assertEqual(variant.quantity, 1)
        self.assertEqual(
            client.args,
            (
                current.ebay_item_id,
                1,
                "sell-skuless-variant",
                option.source_key,
                option.specifics,
                option.price,
                self.listing.currency,
            ),
        )

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

    def test_pending_sale_revalidates_quote_before_first_inventory_write(self):
        current = product("123456789012", price="10.00", quantity=3)
        listing = self.listing.__class__(
            **{
                **self.listing.__dict__,
                "price": Decimal("20.00"),
                "quantity": 3,
                "variations": (),
            }
        )
        operation = InventoryOperation.objects.create(
            idempotency_key="pending-sale-quote",
            product=current,
            reason=InventoryOperation.Reason.SALE,
            expected_quantity=3,
            requested_quantity=1,
        )

        class InventoryClient:
            def get_item(self, item_id):
                return listing

            def revise_inventory_status(self, *args):
                raise AssertionError("A changed quote must not reduce inventory")

        with self.assertRaisesMessage(EbayInventoryConflict, "price"):
            set_inventory_quantity(
                InventoryClient(),
                product=current,
                expected_quantity=3,
                quantity=1,
                reason=InventoryOperation.Reason.SALE,
                idempotency_key=operation.idempotency_key,
                expected_currency="USD",
                expected_price=Decimal("10.00"),
            )

        current.refresh_from_db()
        self.assertEqual(current.price, Decimal("20.00"))
        self.assertFalse(InventoryOperation.objects.filter(pk=operation.pk).exists())

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
        self.assertEqual(list(Product.objects.purchasable()), [current])

    def test_available_quantity_uses_prefetched_variants(self):
        current = product("123456789012", quantity=8)
        current.variants.create(
            source_key="available",
            sku="AVAILABLE",
            title="Available variation",
            price="10.00",
            quantity=3,
        )
        current.variants.create(
            source_key="unavailable",
            sku="",
            title="Unavailable variation",
            price="8.00",
            quantity=5,
            purchasable=False,
        )
        prefetched = Product.objects.prefetch_related(
            Prefetch(
                "variants", queryset=ProductVariant.objects.with_availability()
            )
        ).get(pk=current.pk)

        with self.assertNumQueries(0):
            self.assertEqual(prefetched.available_quantity, 3)
            self.assertTrue(prefetched.is_purchasable)

    def test_non_usd_product_is_not_purchasable(self):
        current = product("123456789012", currency="EUR")

        self.assertFalse(current.is_purchasable)
        self.assertNotIn(current, Product.objects.purchasable())

    def test_active_sku_less_variant_with_specifics_is_available(self):
        current = product("123456789012", quantity=1)
        current.variants.create(
            source_key="missing-sku",
            sku="",
            title="Combo option",
            specifics={"Combo": ["Body with mount"]},
            price="10.00",
            quantity=1,
            purchasable=True,
        )

        self.assertEqual(current.available_quantity, 1)
        self.assertTrue(current.is_purchasable)
        self.assertIn(current, Product.objects.purchasable())
