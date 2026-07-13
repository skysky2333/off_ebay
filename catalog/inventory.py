from django.db import transaction
from django.db.models import Sum

from orders.inventory import InventoryUnavailable
from orders.models import InventoryReservation

from .ebay import EbayTradingClient
from .models import InventoryOperation, Product, ProductVariant
from .services import set_inventory_quantity


def _listing_quantity(listing, variant):
    if variant is None:
        return listing.quantity
    matches = [item for item in listing.variations if item.sku == variant.sku]
    if len(matches) != 1:
        raise InventoryUnavailable(f"eBay no longer has variation SKU {variant.sku}.")
    return matches[0].quantity


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
                or not variant.sku
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

        reserved = reservations.aggregate(total=Sum("quantity"))["total"] or 0
        if reserved > available:
            raise InventoryUnavailable(f"Only {available} of {item.title} remain.")

    def commit(self, reservation):
        reservation = InventoryReservation.objects.select_related(
            "order_item__product", "order_item__variant"
        ).get(pk=reservation.pk)
        item = reservation.order_item
        if item.product is None or (item.variation_sku and item.variant is None):
            raise InventoryUnavailable("The catalog source for this item is unavailable.")
        key = f"sale-{reservation.pk}"
        operation = InventoryOperation.objects.filter(idempotency_key=key).first()
        with EbayTradingClient() as client:
            if operation:
                expected = operation.expected_quantity
                target = operation.requested_quantity
            else:
                listing = client.get_item(item.ebay_item_id)
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
