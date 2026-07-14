from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from orders.inventory import InventoryUnavailable
from orders.models import InventoryReservation

from .ebay import ACTIVE_LISTING_STATUS, EbayResponseError, EbayTradingClient
from .models import InventoryOperation, Product, ProductVariant
from .pricing import direct_price
from .services import set_inventory_quantity, sync_listing, sync_unavailable_listing


def _listing_variant(listing, variant):
    matches = [
        item for item in listing.variations if item.source_key == variant.source_key
    ]
    if len(matches) != 1:
        raise InventoryUnavailable(f"eBay no longer has option {variant.title}.")
    return matches[0]


def _listing_quantity(listing, variant):
    if variant is None:
        return listing.quantity
    return _listing_variant(listing, variant).quantity


def _listing_price(listing, variant):
    if variant is None:
        return listing.price
    return _listing_variant(listing, variant).price


def _pricing_quantity(item):
    return item.order.items.filter(product_id=item.product_id).aggregate(
        total=Sum("quantity")
    )["total"]


class EbayInventoryGateway:
    @transaction.atomic
    def reserve(self, reservation):
        reservation = (
            InventoryReservation.objects.select_for_update()
            .select_related("order_item")
            .get(pk=reservation.pk)
        )
        item = reservation.order_item
        product = Product.objects.select_for_update().get(pk=item.product_id)
        if (
            not product.active
            or product.checkout_excluded
            or product.ebay_item_id != item.ebay_item_id
        ):
            raise InventoryUnavailable(f"{item.title} is no longer available.")

        reservations = InventoryReservation.objects.filter(
            status__in={
                InventoryReservation.Status.RESERVED,
                InventoryReservation.Status.COMMITTING,
            },
            order_item__product=product,
        )
        if item.variant_id:
            variant = ProductVariant.objects.select_for_update().get(pk=item.variant_id)
            if (
                variant.product_id != product.pk
                or not variant.active
                or not variant.purchasable
                or variant.sku != item.variation_sku
            ):
                raise InventoryUnavailable(f"{item.title} is no longer available.")
            available = variant.quantity
            reservations = reservations.filter(order_item__variant=variant)
        else:
            if product.variants.filter(active=True).exists():
                raise InventoryUnavailable("A product option is required.")
            available = product.quantity
            reservations = reservations.filter(order_item__variant__isnull=True)

        reserved_elsewhere = (
            reservations.exclude(pk=reservation.pk).aggregate(total=Sum("quantity"))[
                "total"
            ]
            or 0
        )
        if reserved_elsewhere + reservation.quantity > available:
            remaining = max(available - reserved_elsewhere, 0)
            raise InventoryUnavailable(f"Only {remaining} of {item.title} remain.")

    def commit(self, reservation):
        reservation = InventoryReservation.objects.select_related(
            "order_item__order", "order_item__product", "order_item__variant"
        ).get(pk=reservation.pk)
        item = reservation.order_item
        if item.product is None or (item.variation_sku and item.variant is None):
            raise InventoryUnavailable("The catalog source for this item is unavailable.")
        key = f"sale-{reservation.pk}"
        pricing_quantity = _pricing_quantity(item)
        operation = InventoryOperation.objects.filter(idempotency_key=key).first()
        if operation is None and not Product.objects.filter(
            pk=item.product_id, active=True, checkout_excluded=False
        ).exists():
            raise InventoryUnavailable(f"{item.title} is no longer available.")
        with EbayTradingClient() as client:
            if operation:
                expected = operation.expected_quantity
                target = operation.requested_quantity
            else:
                listing = client.get_item(item.ebay_item_id)
                if (
                    listing.item_id != item.ebay_item_id
                    or listing.listing_status != ACTIVE_LISTING_STATUS
                ):
                    raise InventoryUnavailable(f"{item.title} is no longer available.")
                if bool(listing.variations) != bool(item.variant):
                    sync_listing(listing, timezone.now())
                    raise InventoryUnavailable(
                        f"The options for {item.title} changed. Refresh your cart and review it again."
                    )
                if item.variant and item.variant.sku != item.variation_sku:
                    raise InventoryUnavailable(f"{item.title} is no longer available.")
                if (
                    listing.currency != item.order.currency
                    or direct_price(
                        _listing_price(listing, item.variant),
                        pricing_quantity,
                        listing.volume_discounts,
                    )
                    != item.unit_price
                ):
                    sync_listing(listing, timezone.now())
                    raise InventoryUnavailable(
                        f"The price for {item.title} changed. Refresh your cart and review the updated total."
                    )
                expected = _listing_quantity(listing, item.variant)
                if expected < reservation.quantity:
                    raise InventoryUnavailable(f"{item.title} is no longer available.")
                target = expected - reservation.quantity
                operation = InventoryOperation.objects.filter(idempotency_key=key).first()
                if operation:
                    expected = operation.expected_quantity
                    target = operation.requested_quantity
            set_inventory_quantity(
                client,
                product=item.product,
                variant=item.variant,
                expected_quantity=expected,
                quantity=target,
                reason=InventoryOperation.Reason.SALE,
                idempotency_key=key,
                expected_currency=item.order.currency,
                expected_price=item.unit_price,
                price_quantity=pricing_quantity,
            )

    def release(self, reservation):
        sale_succeeded = InventoryOperation.objects.filter(
            idempotency_key=f"sale-{reservation.pk}",
            status=InventoryOperation.Status.SUCCEEDED,
        ).exists()
        if (
            reservation.status != InventoryReservation.Status.COMMITTED
            and not sale_succeeded
        ):
            return
        reservation = InventoryReservation.objects.select_related(
            "order_item__product", "order_item__variant"
        ).get(pk=reservation.pk)
        item = reservation.order_item
        if item.product is None or (item.variation_sku and item.variant is None):
            raise InventoryUnavailable("The catalog source for this item is unavailable.")
        key = f"release-{reservation.pk}"
        operation = InventoryOperation.objects.filter(idempotency_key=key).first()
        with EbayTradingClient() as client:
            if operation:
                expected = operation.expected_quantity
                target = operation.requested_quantity
            else:
                listing = client.get_item(item.ebay_item_id)
                if listing.item_id != item.ebay_item_id:
                    raise EbayResponseError(
                        f"GetItem returned {listing.item_id} for {item.ebay_item_id}"
                    )
                variant_missing = item.variant and not any(
                    candidate.source_key == item.variant.source_key
                    for candidate in listing.variations
                )
                if listing.listing_status != ACTIVE_LISTING_STATUS or variant_missing:
                    sync_unavailable_listing(item.product, listing, timezone.now())
                    return
                expected = _listing_quantity(listing, item.variant)
                target = expected + reservation.quantity
                operation = InventoryOperation.objects.filter(idempotency_key=key).first()
                if operation:
                    expected = operation.expected_quantity
                    target = operation.requested_quantity
            set_inventory_quantity(
                client,
                product=item.product,
                variant=item.variant,
                expected_quantity=expected,
                quantity=target,
                reason=InventoryOperation.Reason.RELEASE,
                idempotency_key=key,
            )
