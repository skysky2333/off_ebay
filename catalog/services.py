from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.text import slugify

from .ebay import EbayInventoryConflict, EbayResponseError, SUPPORTED_LISTING_TYPES
from .models import (
    InventoryOperation,
    Product,
    ProductImage,
    ProductVariant,
    SyncRun,
)


class SyncRunTracker:
    def __init__(self, run):
        self.run = run

    def __enter__(self):
        return self.run

    def __exit__(self, exception_type, exception, traceback):
        if exception is not None:
            self.run.status = SyncRun.Status.FAILED
            self.run.error = str(exception)
            self.run.completed_at = timezone.now()
            self.run.save(update_fields=("status", "error", "completed_at"))
        return False


def _slug(listing):
    return f"{slugify(listing.title)[:200]}-{listing.item_id}"


def _sync_variants(product, listing, preserve_inventory):
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
            variant.active = True
            variant.purchasable = variation.purchasable
            fields = ["sku", "title", "specifics", "price", "active", "purchasable"]
            if not preserve_inventory:
                variant.quantity = variation.quantity
                fields.append("quantity")
            variant.save(update_fields=fields)
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


def _sync_listing(listing, observed_at):
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
        preserve_inventory = product.last_synced_at > observed_at
        if preserve_inventory:
            defaults.pop("quantity")
            defaults.pop("last_synced_at")
        for field, value in defaults.items():
            setattr(product, field, value)
        product.save(update_fields=(*defaults.keys(), "updated_at"))
    else:
        preserve_inventory = False
    _sync_variants(product, listing, preserve_inventory)
    _sync_images(product, listing)


def sync_catalog(client):
    run = SyncRun.objects.create(seller_username=settings.EBAY_SELLER_USERNAME)
    with SyncRunTracker(run):
        client.verify_seller()
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
        ]
        synced_at = timezone.now()
        with transaction.atomic():
            list(Product.objects.select_for_update().values_list("pk", flat=True))
            for listing, observed_at in supported:
                _sync_listing(listing, observed_at)
            active_ids = [listing.item_id for listing, _ in supported]
            stale = Product.objects.filter(active=True).exclude(
                ebay_item_id__in=active_ids
            )
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
):
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
    with transaction.atomic():
        product = Product.objects.select_for_update().get(pk=product.pk)
        if variant is not None:
            variant = ProductVariant.objects.select_for_update().get(pk=variant.pk)
        listing = client.get_item(product.ebay_item_id)
        if listing.item_id != product.ebay_item_id:
            raise EbayResponseError(
                f"GetItem returned {listing.item_id} for {product.ebay_item_id}"
            )
        if variant is None:
            current_quantity = listing.quantity
        else:
            matches = [item for item in listing.variations if item.sku == variant.sku]
            if len(matches) != 1:
                raise EbayResponseError(
                    f"GetItem did not return variation SKU {variant.sku}"
                )
            current_quantity = matches[0].quantity
        if current_quantity == expected_quantity:
            verified = client.revise_inventory_status(
                product.ebay_item_id,
                quantity,
                idempotency_key,
                variant.sku if variant else "",
            )
        elif not created and current_quantity == quantity:
            verified = quantity
        else:
            conflict = (
                f"Inventory mismatch: expected {expected_quantity}, found {current_quantity}"
            )
            operation.status = InventoryOperation.Status.FAILED
            operation.error = conflict
            operation.completed_at = timezone.now()
            operation.save(update_fields=("status", "error", "completed_at"))
            verified = None
        if conflict:
            return_operation = operation
        elif variant is None:
            Product.objects.filter(pk=product.pk).update(
                quantity=verified, last_synced_at=timezone.now()
            )
        else:
            ProductVariant.objects.filter(pk=variant.pk).update(quantity=verified)
            total = ProductVariant.objects.filter(
                product=product, active=True
            ).aggregate(total=Sum("quantity"))["total"] or 0
            Product.objects.filter(pk=product.pk).update(
                quantity=total, last_synced_at=timezone.now()
            )
        if not conflict:
            operation.status = InventoryOperation.Status.SUCCEEDED
            operation.verified_quantity = verified
            operation.completed_at = timezone.now()
            operation.save(
                update_fields=("status", "verified_quantity", "completed_at")
            )
            return_operation = operation
    if conflict:
        raise EbayInventoryConflict(conflict)
    return return_operation
