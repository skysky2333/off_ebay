from django.conf import settings
from django.db import transaction
from django.db.models import Min, Q, Sum
from django.utils import timezone
from django.utils.text import slugify

from .account_state import account_closure_notification_id, record_account_closure
from .ebay import (
    ACTIVE_LISTING_STATUS,
    SUPPORTED_LISTING_TYPES,
    EbayInventoryConflict,
    EbayResponseError,
    EbayUserIdentity,
)
from .models import (
    EbayAccountClosure,
    EbayAccountIdentity,
    InventoryOperation,
    Product,
    ProductImage,
    ProductVariant,
    SyncRun,
)
from .pricing import direct_price


class EbayAccountIdentityUnavailable(Exception):
    pass


def _close_ebay_account(notification_id):
    from orders.models import OrderItem
    from storefront.models import StoreSettings

    store = StoreSettings.objects.select_for_update().get(pk=1)
    closure, _ = EbayAccountClosure.objects.get_or_create(
        pk=1, defaults={"notification_id": notification_id}
    )
    store.checkout_enabled = False
    store.save(update_fields=("checkout_enabled",))
    InventoryOperation.objects.all().delete()
    SyncRun.objects.all().delete()
    Product.objects.all().delete()
    EbayAccountIdentity.objects.all().delete()
    OrderItem.objects.update(ebay_item_id="", variation_sku="", image_url="")
    return closure


@transaction.atomic
def process_ebay_account_closure(notification_id, username, user_id, eias_token):
    existing = EbayAccountClosure.objects.filter(pk=1).first()
    if existing:
        record_account_closure(existing.notification_id)
        return existing
    from storefront.models import StoreSettings

    StoreSettings.objects.select_for_update().get(pk=1)
    identity = EbayAccountIdentity.objects.select_for_update().filter(pk=1).first()
    known_usernames = {settings.EBAY_SELLER_USERNAME.casefold()}
    if identity:
        known_usernames.add(identity.username.casefold())
    matches_username = (
        username.casefold() in known_usernames
        or user_id.casefold() in known_usernames
    )
    if not identity and not matches_username:
        from orders.models import OrderItem

        if (
            settings.EBAY_REFRESH_TOKEN
            or Product.objects.exists()
            or SyncRun.objects.exists()
            or OrderItem.objects.exclude(ebay_item_id="").exists()
        ):
            raise EbayAccountIdentityUnavailable(
                "Stable eBay seller identity has not been recorded."
            )
        return None
    if (
        not matches_username
        and (not identity or eias_token != identity.eias_token)
    ):
        return None
    record_account_closure(notification_id)
    return _close_ebay_account(notification_id)


@transaction.atomic
def enforce_recorded_ebay_account_closure():
    notification_id = account_closure_notification_id()
    if not notification_id:
        closure = EbayAccountClosure.objects.filter(pk=1).first()
        if closure:
            notification_id = closure.notification_id
            record_account_closure(notification_id)
    return _close_ebay_account(notification_id) if notification_id else None


class SyncRunTracker:
    def __init__(self, run):
        self.run = run

    def __enter__(self):
        return self.run

    def __exit__(self, exception_type, exception, traceback):
        if exception is not None:
            SyncRun.objects.filter(pk=self.run.pk).update(
                status=SyncRun.Status.FAILED,
                error=str(exception),
                completed_at=timezone.now(),
            )
        return False


def _slug(listing):
    return f"{slugify(listing.title)[:200]}-{listing.item_id}"


def _sync_variants(product, listing):
    source_keys = []
    for variation in listing.variations:
        source_keys.append(variation.source_key)
        variant, created = ProductVariant.objects.get_or_create(
            product=product,
            source_key=variation.source_key,
            defaults={
                "sku": variation.sku,
                "title": variation.title,
                "specifics": variation.specifics,
                "price": variation.price,
                "quantity": variation.quantity,
                "active": True,
                "purchasable": variation.purchasable,
            },
        )
        if not created:
            variant.sku = variation.sku
            variant.title = variation.title
            variant.specifics = variation.specifics
            variant.price = variation.price
            variant.quantity = variation.quantity
            variant.active = True
            variant.purchasable = variation.purchasable
            variant.save(
                update_fields=(
                    "sku",
                    "title",
                    "specifics",
                    "price",
                    "quantity",
                    "active",
                    "purchasable",
                )
            )
    product.variants.exclude(source_key__in=source_keys).update(
        active=False, quantity=0
    )


def _sync_images(product, listing):
    product.images.all().delete()
    ProductImage.objects.bulk_create(
        [
            ProductImage(
                product=product,
                url=image.url,
                position=position,
                variation_name=image.variation_name,
                variation_value=image.variation_value,
            )
            for position, image in enumerate(listing.images, start=1)
        ]
    )


def sync_listing(listing, observed_at):
    defaults = {
        "title": listing.title,
        "description": listing.description,
        "price": listing.price,
        "currency": listing.currency,
        "condition": listing.condition,
        "category_id": listing.category_id,
        "category_name": listing.category_name,
        "item_specifics": listing.item_specifics,
        "shipping": listing.shipping,
        "listing_url": listing.listing_url,
        "listing_type": listing.listing_type,
        "quantity": listing.quantity,
        "active": True,
        "ebay_started_at": listing.started_at,
        "ebay_ends_at": listing.ends_at,
        "last_synced_at": observed_at,
    }
    product, created = Product.objects.get_or_create(
        ebay_item_id=listing.item_id,
        defaults={
            **defaults,
            "slug": _slug(listing),
            "checkout_excluded": listing.item_id
            in settings.EBAY_CHECKOUT_EXCLUDED_ITEMS,
        },
    )
    if not created:
        if product.last_synced_at > observed_at:
            return
        for field, value in defaults.items():
            setattr(product, field, value)
        product.save(update_fields=(*defaults.keys(), "updated_at"))
    _sync_variants(product, listing)
    _sync_images(product, listing)


def sync_unavailable_listing(product, listing, observed_at):
    if listing.listing_status == ACTIVE_LISTING_STATUS:
        sync_listing(listing, observed_at)
        return
    ProductVariant.objects.filter(product=product).update(active=False, quantity=0)
    Product.objects.filter(pk=product.pk).update(
        active=False, quantity=0, last_synced_at=observed_at
    )


def complete_unavailable_release(operation, created, product, listing):
    now = timezone.now()
    sync_unavailable_listing(product, listing, now)
    if created:
        operation.delete()
        return
    operation.status = InventoryOperation.Status.SUCCEEDED
    operation.completed_at = now
    operation.save(update_fields=("status", "completed_at"))


def sync_catalog(client):
    if account_closure_notification_id() or EbayAccountClosure.objects.exists():
        raise EbayResponseError("The eBay seller account is closed.")
    run = SyncRun.objects.create(seller_username=settings.EBAY_SELLER_USERNAME)
    with SyncRunTracker(run):
        identity = client.verify_seller()
        if not isinstance(identity, EbayUserIdentity):
            raise EbayResponseError("eBay seller verification returned invalid identity")
        from storefront.models import StoreSettings

        account_closed = False
        with transaction.atomic():
            StoreSettings.objects.select_for_update().get(pk=1)
            account_closed = EbayAccountClosure.objects.exists()
            if not account_closed:
                EbayAccountIdentity.objects.update_or_create(
                    pk=1,
                    defaults={
                        "username": identity.username,
                        "eias_token": identity.eias_token,
                    },
                )
        if account_closed:
            SyncRun.objects.filter(pk=run.pk).delete()
            raise EbayResponseError("The eBay seller account is closed.")
        item_ids = client.active_item_ids()
        if len(item_ids) != len(set(item_ids)):
            raise EbayResponseError("GetMyeBaySelling returned duplicate item IDs")
        hydrated = []
        for item_id in item_ids:
            observed_at = timezone.now()
            hydrated.append((client.get_item(item_id), observed_at))
        for item_id, (listing, _) in zip(item_ids, hydrated, strict=True):
            if listing.item_id != item_id:
                raise EbayResponseError(
                    f"GetItem returned {listing.item_id} for requested item {item_id}"
                )
        supported = [
            entry
            for entry in hydrated
            if entry[0].listing_type in SUPPORTED_LISTING_TYPES
            and entry[0].listing_status == ACTIVE_LISTING_STATUS
        ]
        synced_at = timezone.now()
        account_closed = False
        with transaction.atomic():
            StoreSettings.objects.select_for_update().get(pk=1)
            account_closed = EbayAccountClosure.objects.exists()
            if not account_closed:
                list(Product.objects.select_for_update().values_list("pk", flat=True))
                for listing, observed_at in supported:
                    sync_listing(listing, observed_at)
                active_ids = [listing.item_id for listing, _ in supported]
                stale = Product.objects.filter(
                    active=True, last_synced_at__lt=run.started_at
                ).exclude(ebay_item_id__in=active_ids)
                deactivated_count = stale.count()
                ProductVariant.objects.filter(product__in=stale).update(
                    active=False, quantity=0
                )
                stale.update(active=False, quantity=0, last_synced_at=synced_at)
                run.status = SyncRun.Status.SUCCEEDED
                run.indexed_count = len(item_ids)
                run.imported_count = len(supported)
                run.deactivated_count = deactivated_count
                run.completed_at = timezone.now()
                run.save(
                    update_fields=(
                        "status",
                        "indexed_count",
                        "imported_count",
                        "deactivated_count",
                        "completed_at",
                    )
                )
        if account_closed:
            SyncRun.objects.filter(pk=run.pk).delete()
            raise EbayResponseError("The eBay seller account is closed.")
    return run


def set_inventory_quantity(
    client,
    *,
    product,
    expected_quantity,
    quantity,
    reason,
    idempotency_key,
    variant=None,
    expected_currency=None,
    expected_price=None,
):
    if (expected_currency is None) != (expected_price is None):
        raise ValueError("Expected currency and price must be provided together")
    if variant is not None and variant.product_id != product.id:
        raise ValueError("Variant does not belong to product")
    if variant is not None and not variant.sku:
        raise ValueError("Variations without an eBay SKU cannot be purchased")
    operation, created = InventoryOperation.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "product": product,
            "variant": variant,
            "reason": reason,
            "expected_quantity": expected_quantity,
            "requested_quantity": quantity,
        },
    )
    expected = (
        product.id,
        variant.id if variant else None,
        reason,
        expected_quantity,
        quantity,
    )
    actual = (
        operation.product_id,
        operation.variant_id,
        operation.reason,
        operation.expected_quantity,
        operation.requested_quantity,
    )
    if actual != expected:
        raise ValueError(
            "Idempotency key was already used for another inventory change"
        )
    if not created and operation.status == InventoryOperation.Status.SUCCEEDED:
        return operation
    if not created and operation.status == InventoryOperation.Status.FAILED:
        raise EbayInventoryConflict(operation.error)
    conflict = ""
    quote_changed = ""
    with transaction.atomic():
        product = Product.objects.select_for_update().get(pk=product.pk)
        if variant is not None:
            variant = ProductVariant.objects.select_for_update().get(pk=variant.pk)
        listing = client.get_item(product.ebay_item_id)
        if listing.item_id != product.ebay_item_id:
            raise EbayResponseError(
                f"GetItem returned {listing.item_id} for {product.ebay_item_id}"
            )
        if (
            reason == InventoryOperation.Reason.RELEASE
            and listing.listing_status != ACTIVE_LISTING_STATUS
        ):
            complete_unavailable_release(operation, created, product, listing)
            return None
        listing_inactive = listing.listing_status != ACTIVE_LISTING_STATUS
        if listing_inactive:
            sync_unavailable_listing(product, listing, timezone.now())
            listing_variant = None
            current_quantity = None
            current_price = None
        elif variant is None:
            listing_variant = None
            current_quantity = listing.quantity
            current_price = (
                direct_price(listing.price) if expected_price is not None else None
            )
        else:
            listing_variant = None
            matches = [item for item in listing.variations if item.sku == variant.sku]
            if reason == InventoryOperation.Reason.RELEASE and not matches:
                complete_unavailable_release(operation, created, product, listing)
                return None
            if len(matches) != 1:
                if not created or expected_price is None:
                    raise EbayResponseError(
                        f"GetItem did not return variation SKU {variant.sku}"
                    )
                current_quantity = None
                current_price = None
            else:
                listing_variant = matches[0]
                current_quantity = listing_variant.quantity
                current_price = (
                    direct_price(listing_variant.price)
                    if expected_price is not None
                    else None
                )
        quote_mismatch = listing_inactive or (
            expected_price is not None
            and (
                bool(listing.variations) != bool(variant)
                or listing.currency != expected_currency
                or current_price != expected_price
            )
        )
        if not created and current_quantity == quantity:
            if quote_mismatch:
                sync_listing(listing, timezone.now())
            verified = quantity
        elif quote_mismatch and (created or current_quantity == expected_quantity):
            if not listing_inactive:
                sync_listing(listing, timezone.now())
            operation.delete()
            quote_changed = (
                f"{product.title} is no longer available."
                if listing_inactive
                else (
                    f"The price for {product.title} changed. "
                    "Refresh your cart and review the updated total."
                )
            )
        elif current_quantity == expected_quantity:
            verified = client.revise_inventory_status(
                product.ebay_item_id,
                quantity,
                idempotency_key,
                variant.sku if variant else "",
            )
        else:
            conflict = (
                f"{product.title} is no longer available."
                if listing_inactive
                else (
                    f"Inventory mismatch: expected {expected_quantity}, "
                    f"found {current_quantity}"
                )
            )
            if created and reason == InventoryOperation.Reason.RELEASE:
                operation.delete()
            else:
                operation.status = InventoryOperation.Status.FAILED
                operation.error = conflict
                operation.completed_at = timezone.now()
                operation.save(update_fields=("status", "error", "completed_at"))
            verified = None
        if quote_changed:
            return_operation = None
        elif conflict:
            return_operation = operation
        elif variant is None:
            Product.objects.filter(pk=product.pk).update(
                quantity=verified, last_synced_at=timezone.now()
            )
        else:
            ProductVariant.objects.filter(pk=variant.pk).update(quantity=verified)
            inventory = ProductVariant.objects.filter(
                product=product, active=True
            ).aggregate(
                total=Sum("quantity"),
                price=Min(
                    "price",
                    filter=Q(purchasable=True, quantity__gt=0),
                ),
            )
            product_updates = {
                "quantity": inventory["total"] or 0,
                "last_synced_at": timezone.now(),
            }
            if inventory["price"] is not None:
                product_updates["price"] = inventory["price"]
            Product.objects.filter(pk=product.pk).update(**product_updates)
        if not conflict and not quote_changed:
            operation.status = InventoryOperation.Status.SUCCEEDED
            operation.verified_quantity = verified
            operation.completed_at = timezone.now()
            operation.save(
                update_fields=("status", "verified_quantity", "completed_at")
            )
            return_operation = operation
    if quote_changed:
        raise EbayInventoryConflict(quote_changed)
    if conflict:
        raise EbayInventoryConflict(conflict)
    return return_operation
