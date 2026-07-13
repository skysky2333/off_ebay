import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from catalog.ebay import EbayInventoryConflict
from catalog.models import InventoryOperation, Product, ProductVariant

from .inventory import InventoryGateway, InventoryUnavailable
from .models import (
    InventoryReservation,
    Order,
    OrderEvent,
    OrderItem,
    Refund,
    Shipment,
)
from .paypal import PayPalClient


class OrderStateError(ValueError):
    pass


class PayPalOrderInactive(OrderStateError):
    pass


class IdempotencyConflict(ValueError):
    pass


class PaymentDataError(ValueError):
    pass


class WebhookVerificationError(ValueError):
    pass


@dataclass(frozen=True)
class ShippingAddress:
    name: str
    line_1: str
    city: str
    region: str
    postal_code: str
    country_code: str
    line_2: str = ""
    phone: str = ""


@dataclass(frozen=True)
class CheckoutLine:
    product_id: int
    quantity: int
    variant_id: int | None = None


def _money(value):
    return format(value, ".2f")


def _checkout_fingerprint(email, address, currency, shipping_total, lines):
    payload = {
        "email": email,
        "address": asdict(address),
        "currency": currency,
        "shipping_total": _money(shipping_total),
        "lines": [
            {
                **asdict(line),
            }
            for line in lines
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _event(order, kind, source, data=None, event_key=None):
    return OrderEvent.objects.create(
        order=order,
        kind=kind,
        source=source,
        event_key=event_key,
        data=data or {},
    )


def _validate_us_destination(country_code, region):
    if country_code.upper() != "US":
        raise ValueError("Only United States shipping addresses are supported.")
    if not region.strip():
        raise ValueError("A state or territory is required for United States shipping.")


@transaction.atomic
def create_guest_order(
    *,
    checkout_key: uuid.UUID,
    email: str,
    address: ShippingAddress,
    lines: list[CheckoutLine],
    shipping_total: Decimal,
    inventory: InventoryGateway,
):
    if not lines:
        raise ValueError("An order requires at least one item.")
    if any(not line.product_id for line in lines):
        raise ValueError("Every order item requires a catalog product.")
    if shipping_total < 0:
        raise ValueError("Shipping cannot be negative.")
    if any(line.quantity <= 0 for line in lines):
        raise ValueError("Item quantities must be positive.")
    identities = [(line.product_id, line.variant_id) for line in lines]
    if len(identities) != len(set(identities)):
        raise ValueError("Duplicate checkout lines are not allowed.")
    _validate_us_destination(address.country_code, address.region)

    currency = "USD"
    fingerprint = _checkout_fingerprint(email, address, currency, shipping_total, lines)
    existing = Order.objects.select_for_update().filter(checkout_key=checkout_key).first()
    if existing:
        if existing.checkout_fingerprint != fingerprint:
            raise IdempotencyConflict("Checkout key was already used for another order.")
        return existing

    products = {
        product.pk: product
        for product in Product.objects.select_for_update()
        .order_by("pk")
        .prefetch_related("images", "variants")
        .filter(pk__in=sorted({line.product_id for line in lines}))
    }
    variants = {
        variant.pk: variant
        for variant in ProductVariant.objects.select_for_update().filter(
            pk__in=sorted({line.variant_id for line in lines if line.variant_id})
        ).order_by("pk")
    }
    snapshots = []
    currencies = set()
    for line in lines:
        product = products.get(line.product_id)
        if product is None or not product.active or product.checkout_excluded:
            raise InventoryUnavailable("An item is no longer available.")
        active_variants = [variant for variant in product.variants.all() if variant.active]
        variant = variants.get(line.variant_id) if line.variant_id else None
        if variant:
            if (
                variant.product_id != product.pk
                or not variant.active
                or not variant.purchasable
                or not variant.sku
            ):
                raise InventoryUnavailable("A selected product option is unavailable.")
            price = variant.price
        else:
            if active_variants:
                raise InventoryUnavailable("A product option is required.")
            price = product.price
        currencies.add(product.currency)
        image = product.images.first()
        snapshots.append((line, product, variant, price, image.url if image else ""))
    if currencies != {"USD"}:
        raise ValueError("Checkout requires USD catalog prices.")

    subtotal = sum(
        (price * line.quantity for line, _, _, price, _ in snapshots),
        start=Decimal("0.00"),
    )
    expires_at = timezone.now() + timedelta(minutes=settings.ORDER_RESERVATION_MINUTES)
    order = Order(
        checkout_key=checkout_key,
        checkout_fingerprint=fingerprint,
        customer_email=email,
        customer_name=address.name,
        customer_phone=address.phone,
        shipping_line_1=address.line_1,
        shipping_line_2=address.line_2,
        shipping_city=address.city,
        shipping_region=address.region,
        shipping_postal_code=address.postal_code,
        shipping_country_code=address.country_code.upper(),
        currency=currency.upper(),
        subtotal=subtotal,
        shipping_total=shipping_total,
        total=subtotal + shipping_total,
        expires_at=expires_at,
    )
    order.full_clean(validate_unique=False)
    order.save()

    for line, product, variant, price, image_url in snapshots:
        item = OrderItem(
            order=order,
            product=product,
            variant=variant,
            ebay_item_id=product.ebay_item_id,
            variation_sku=variant.sku if variant else "",
            variation_title=variant.title if variant else "",
            title=product.title,
            condition=product.condition,
            image_url=image_url,
            quantity=line.quantity,
            unit_price=price,
        )
        item.full_clean(validate_unique=False)
        item.save()
        reservation = InventoryReservation.objects.create(
            order_item=item,
            quantity=line.quantity,
            expires_at=expires_at,
        )
        inventory.reserve(reservation)

    _event(
        order,
        "order.created",
        OrderEvent.Source.SYSTEM,
        {"total": _money(order.total), "currency": order.currency},
        f"order:{order.pk}:created",
    )
    return order


def _paypal_payload(order, return_url, cancel_url):
    items = [
        {
            "name": " - ".join(filter(None, (item.title, item.variation_title)))[:127],
            "quantity": str(item.quantity),
            "sku": item.variation_sku or item.ebay_item_id,
            "category": "PHYSICAL_GOODS",
            "unit_amount": {
                "currency_code": order.currency,
                "value": _money(item.unit_price),
            },
        }
        for item in order.items.all()
    ]
    shipping_address = {
        "address_line_1": order.shipping_line_1,
        "admin_area_2": order.shipping_city,
        "admin_area_1": order.shipping_region,
        "postal_code": order.shipping_postal_code,
        "country_code": order.shipping_country_code,
    }
    if order.shipping_line_2:
        shipping_address["address_line_2"] = order.shipping_line_2
    return {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": order.reference,
                "custom_id": order.reference,
                "invoice_id": order.reference,
                "amount": {
                    "currency_code": order.currency,
                    "value": _money(order.total),
                    "breakdown": {
                        "item_total": {
                            "currency_code": order.currency,
                            "value": _money(order.subtotal),
                        },
                        "shipping": {
                            "currency_code": order.currency,
                            "value": _money(order.shipping_total),
                        },
                    },
                },
                "items": items,
                "shipping": {
                    "name": {"full_name": order.customer_name},
                    "address": shipping_address,
                },
            }
        ],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    "shipping_preference": "SET_PROVIDED_ADDRESS",
                    "user_action": "PAY_NOW",
                    "return_url": return_url,
                    "cancel_url": cancel_url,
                }
            }
        },
    }


@transaction.atomic
def create_paypal_checkout(order_id, return_url, cancel_url, client: PayPalClient):
    order = Order.objects.select_for_update().prefetch_related("items").get(pk=order_id)
    if order.status != Order.Status.AWAITING_PAYMENT:
        raise OrderStateError("Only awaiting-payment orders can start PayPal checkout.")
    if order.paypal_order_id:
        response = client.get_order(order.paypal_order_id)
        if response["id"] != order.paypal_order_id:
            raise PaymentDataError("PayPal order identity does not match the order.")
        order.paypal_status = response["status"]
        order.save(update_fields=("paypal_status", "updated_at"))
        if response["status"] == "VOIDED":
            raise PayPalOrderInactive("The previous PayPal checkout has ended.")
        return order
    if order.expires_at <= timezone.now():
        raise OrderStateError("The inventory reservation has expired.")
    _validate_us_destination(order.shipping_country_code, order.shipping_region)

    response = client.create_order(
        _paypal_payload(order, return_url, cancel_url),
        request_id=f"create-{order.reference}",
    )
    if not response["id"] or response["status"] not in {
        "CREATED",
        "PAYER_ACTION_REQUIRED",
    }:
        raise PaymentDataError("PayPal did not create a payable order.")
    order.paypal_order_id = response["id"]
    order.paypal_status = response["status"]
    order.save(update_fields=("paypal_order_id", "paypal_status", "updated_at"))
    _event(
        order,
        "paypal.order_created",
        OrderEvent.Source.PAYPAL,
        {"paypal_order_id": order.paypal_order_id, "status": order.paypal_status},
        f"paypal-order:{order.paypal_order_id}:created",
    )
    return order


def _validate_amount(order, amount):
    if amount["currency_code"] != order.currency:
        raise PaymentDataError("PayPal currency does not match the order.")
    if Decimal(amount["value"]) != order.total:
        raise PaymentDataError("PayPal amount does not match the order.")


def _validate_paypal_order(order, response):
    if response["id"] != order.paypal_order_id or response["intent"] != "CAPTURE":
        raise PaymentDataError("PayPal order identity does not match the order.")
    if response["status"] not in {"APPROVED", "COMPLETED"}:
        raise PaymentDataError("PayPal has not approved this order.")
    purchase_units = response["purchase_units"]
    if len(purchase_units) != 1:
        raise PaymentDataError("PayPal order must contain one purchase unit.")
    purchase = purchase_units[0]
    for field in ("reference_id", "custom_id", "invoice_id"):
        if purchase[field] != order.reference:
            raise PaymentDataError("PayPal reference does not match the order.")
    _validate_amount(order, purchase["amount"])
    address = purchase["shipping"]["address"]
    expected = {
        "address_line_1": order.shipping_line_1,
        "admin_area_2": order.shipping_city,
        "admin_area_1": order.shipping_region,
        "postal_code": order.shipping_postal_code,
        "country_code": order.shipping_country_code,
    }
    if order.shipping_line_2:
        expected["address_line_2"] = order.shipping_line_2
    if any(address.get(key, "").strip() != value.strip() for key, value in expected.items()):
        raise PaymentDataError("PayPal shipping address does not match the order.")
    _validate_us_destination(address["country_code"], address["admin_area_1"])
    return purchase


def _completed_capture(order, response):
    if response["id"] != order.paypal_order_id or response["status"] != "COMPLETED":
        raise PaymentDataError("PayPal order capture is not complete.")
    captures = response["purchase_units"][0]["payments"]["captures"]
    if len(captures) != 1 or captures[0]["status"] != "COMPLETED":
        raise PaymentDataError("PayPal did not return one completed capture.")
    capture = captures[0]
    _validate_amount(order, capture["amount"])
    return capture


@transaction.atomic
def _begin_payment_processing(order_id, paypal_status):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status == Order.Status.AWAITING_PAYMENT:
        if order.expires_at <= timezone.now():
            raise OrderStateError("The inventory reservation has expired.")
        order.status = Order.Status.PAYMENT_PROCESSING
    elif order.status != Order.Status.PAYMENT_PROCESSING:
        raise OrderStateError("This order cannot be captured.")
    order.paypal_status = paypal_status
    order.save(update_fields=("status", "paypal_status", "updated_at"))


def _commit_reservations(order_id, inventory):
    reservation_ids = list(
        InventoryReservation.objects.filter(order_item__order_id=order_id)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    for reservation_id in reservation_ids:
        with transaction.atomic():
            reservation = InventoryReservation.objects.select_for_update().get(
                pk=reservation_id
            )
            if reservation.status == InventoryReservation.Status.COMMITTED:
                continue
            if reservation.status == InventoryReservation.Status.RESERVED:
                reservation.status = InventoryReservation.Status.COMMITTING
                reservation.save(update_fields=("status", "updated_at"))
            elif reservation.status != InventoryReservation.Status.COMMITTING:
                raise OrderStateError("Released inventory cannot be committed.")
        inventory.commit(reservation)
        with transaction.atomic():
            reservation = InventoryReservation.objects.select_for_update().get(
                pk=reservation_id
            )
            if reservation.status == InventoryReservation.Status.COMMITTED:
                continue
            if reservation.status != InventoryReservation.Status.COMMITTING:
                continue
            reservation.status = InventoryReservation.Status.COMMITTED
            reservation.committed_at = timezone.now()
            reservation.released_at = None
            reservation.save(
                update_fields=("status", "committed_at", "released_at", "updated_at")
            )


@transaction.atomic
def _reset_failed_commits(order_id):
    reservations = InventoryReservation.objects.select_for_update().filter(
        order_item__order_id=order_id,
        status=InventoryReservation.Status.COMMITTING,
    )
    for reservation in reservations:
        operation = InventoryOperation.objects.filter(
            idempotency_key=f"sale-{reservation.pk}"
        ).first()
        if operation is None or operation.status == InventoryOperation.Status.FAILED:
            reservation.status = InventoryReservation.Status.RESERVED
            reservation.save(update_fields=("status", "updated_at"))


@transaction.atomic
def _mark_capture_pending(order_id):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status == Order.Status.CAPTURE_PENDING:
        return
    if order.status != Order.Status.PAYMENT_PROCESSING:
        raise OrderStateError("This order cannot be captured.")
    if InventoryReservation.objects.filter(order_item__order=order).exclude(
        status=InventoryReservation.Status.COMMITTED
    ).exists():
        raise OrderStateError("Inventory is not ready for payment capture.")
    order.status = Order.Status.CAPTURE_PENDING
    order.save(update_fields=("status", "updated_at"))


@transaction.atomic
def _mark_paid(order_id, capture):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.paid_at:
        if order.paypal_capture_id != capture["id"]:
            raise PaymentDataError("Order already has a different PayPal capture.")
        return order
    if InventoryReservation.objects.filter(order_item__order=order).exclude(
        status=InventoryReservation.Status.COMMITTED
    ).exists():
        raise PaymentDataError("Captured payment requires committed inventory.")
    order.status = Order.Status.PAID
    order.paypal_capture_id = capture["id"]
    order.paypal_status = capture["status"]
    order.paid_at = timezone.now()
    order.save(
        update_fields=(
            "status",
            "paypal_capture_id",
            "paypal_status",
            "paid_at",
            "updated_at",
        )
    )
    _event(
        order,
        "payment.captured",
        OrderEvent.Source.PAYPAL,
        {"capture_id": capture["id"], "amount": capture["amount"]},
        f"paypal-capture:{capture['id']}:completed",
    )
    return order


def capture_paypal_order(order_id, client: PayPalClient, inventory: InventoryGateway):
    order = Order.objects.get(pk=order_id)
    if order.paid_at:
        return order
    if not order.paypal_order_id:
        raise OrderStateError("PayPal checkout has not been created.")
    if order.status not in {
        Order.Status.AWAITING_PAYMENT,
        Order.Status.PAYMENT_PROCESSING,
        Order.Status.CAPTURE_PENDING,
    }:
        raise OrderStateError("This order cannot be captured.")
    _validate_us_destination(order.shipping_country_code, order.shipping_region)

    paypal_order = client.get_order(order.paypal_order_id)
    if paypal_order["id"] != order.paypal_order_id:
        raise PaymentDataError("PayPal order identity does not match the order.")
    if paypal_order["status"] == "VOIDED":
        cancel_order(order.pk, inventory, capture_definitely_absent=True)
        raise PayPalOrderInactive("The PayPal checkout has ended.")
    _validate_paypal_order(order, paypal_order)
    if paypal_order["status"] == "COMPLETED":
        return _mark_paid(order.pk, _completed_capture(order, paypal_order))

    if order.status != Order.Status.CAPTURE_PENDING:
        _begin_payment_processing(order.pk, paypal_order["status"])
        try:
            _commit_reservations(order.pk, inventory)
        except (EbayInventoryConflict, InventoryUnavailable):
            _reset_failed_commits(order.pk)
            cancel_order(order.pk, inventory)
            raise
        _mark_capture_pending(order.pk)
    response = client.capture_order(
        order.paypal_order_id, request_id=f"capture-{order.reference}"
    )
    return _mark_paid(order.pk, _completed_capture(order, response))


def _release_reservations(order_id, inventory):
    reservation_ids = list(
        InventoryReservation.objects.filter(order_item__order_id=order_id)
        .exclude(status=InventoryReservation.Status.RELEASED)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    for reservation_id in reservation_ids:
        reservation = InventoryReservation.objects.get(pk=reservation_id)
        if reservation.status == InventoryReservation.Status.RELEASED:
            continue
        if reservation.status == InventoryReservation.Status.COMMITTING:
            operation = InventoryOperation.objects.filter(
                idempotency_key=f"sale-{reservation.pk}"
            ).first()
            if operation and operation.status == InventoryOperation.Status.FAILED:
                with transaction.atomic():
                    reservation = InventoryReservation.objects.select_for_update().get(
                        pk=reservation_id
                    )
                    if reservation.status == InventoryReservation.Status.COMMITTING:
                        reservation.status = InventoryReservation.Status.RESERVED
                        reservation.save(update_fields=("status", "updated_at"))
            else:
                inventory.commit(reservation)
                with transaction.atomic():
                    reservation = InventoryReservation.objects.select_for_update().get(
                        pk=reservation_id
                    )
                    if reservation.status == InventoryReservation.Status.COMMITTING:
                        reservation.status = InventoryReservation.Status.COMMITTED
                        reservation.committed_at = timezone.now()
                        reservation.save(
                            update_fields=("status", "committed_at", "updated_at")
                        )
        with transaction.atomic():
            reservation = InventoryReservation.objects.select_for_update().get(
                pk=reservation_id
            )
            if reservation.status == InventoryReservation.Status.RELEASED:
                continue
            reservation.status = InventoryReservation.Status.RELEASING
            reservation.save(update_fields=("status", "updated_at"))
        inventory.release(reservation)
        with transaction.atomic():
            reservation = InventoryReservation.objects.select_for_update().get(
                pk=reservation_id
            )
            if reservation.status == InventoryReservation.Status.RELEASED:
                continue
            reservation.status = InventoryReservation.Status.RELEASED
            reservation.released_at = timezone.now()
            reservation.save(update_fields=("status", "released_at", "updated_at"))


def cancel_order(order_id, inventory: InventoryGateway, capture_definitely_absent=False):
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        allowed = {
            Order.Status.AWAITING_PAYMENT,
            Order.Status.PAYMENT_PROCESSING,
            Order.Status.CANCELLED,
        }
        if capture_definitely_absent:
            allowed.add(Order.Status.CAPTURE_PENDING)
        if order.status not in allowed:
            raise OrderStateError("Only unpaid orders can be cancelled.")
        if order.status != Order.Status.CANCELLED:
            now = timezone.now()
            order.status = Order.Status.CANCELLED
            order.cancelled_at = now
            order.save(update_fields=("status", "cancelled_at", "updated_at"))
            _event(order, "order.cancelled", OrderEvent.Source.SYSTEM)
    _release_reservations(order.pk, inventory)
    return Order.objects.get(pk=order_id)


@transaction.atomic
def expire_due_orders(inventory: InventoryGateway, now=None):
    now = now or timezone.now()
    orders = list(
        Order.objects.select_for_update().filter(
            status=Order.Status.AWAITING_PAYMENT, expires_at__lte=now
        )
    )
    for order in orders:
        _release_reservations(order.pk, inventory)
        order.status = Order.Status.EXPIRED
        order.save(update_fields=("status", "updated_at"))
        _event(order, "order.expired", OrderEvent.Source.SYSTEM)
    return len(orders)


def _apply_refund(order, refund_id, amount, now):
    existing = Refund.objects.filter(paypal_refund_id=refund_id).first()
    if existing:
        if existing.order_id != order.pk or existing.amount != amount:
            raise PaymentDataError("PayPal refund data does not match its prior record.")
        return False
    if amount <= 0 or order.refunded_total + amount > order.total:
        raise PaymentDataError("PayPal refund amount is invalid.")
    Refund.objects.create(order=order, paypal_refund_id=refund_id, amount=amount)
    order.refunded_total += amount
    order.paypal_refund_id = refund_id
    if order.refunded_total == order.total:
        order.status = Order.Status.REFUNDED
        order.refunded_at = now
    else:
        order.status = Order.Status.PARTIALLY_REFUNDED
    order.save(
        update_fields=(
            "refunded_total",
            "paypal_refund_id",
            "status",
            "refunded_at",
            "updated_at",
        )
    )
    return True


@transaction.atomic
def refund_order(order_id, client: PayPalClient):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status == Order.Status.REFUNDED:
        return order
    if not order.paypal_capture_id or not order.paid_at:
        raise OrderStateError("Only captured orders can be refunded.")
    amount = order.total - order.refunded_total
    response = client.refund_capture(
        order.paypal_capture_id,
        _money(amount),
        order.currency,
        f"{order.reference}-REFUND",
        request_id=f"refund-{order.reference}",
    )
    if response["status"] != "COMPLETED":
        raise PaymentDataError("PayPal refund is not complete.")
    if response["amount"]["currency_code"] != order.currency or Decimal(
        response["amount"]["value"]
    ) != amount:
        raise PaymentDataError("PayPal refund amount does not match the request.")
    _apply_refund(order, response["id"], amount, timezone.now())
    _event(
        order,
        "payment.refunded",
        OrderEvent.Source.PAYPAL,
        {"refund_id": response["id"], "amount": response["amount"]},
        f"paypal-refund:{response['id']}:completed",
    )
    return order


def _shipment_status(paypal_status):
    return {
        "SHIPPED": Shipment.Status.SHIPPED,
        "ON_HOLD": Shipment.Status.ON_HOLD,
        "DELIVERED": Shipment.Status.DELIVERED,
        "CANCELLED": Shipment.Status.CANCELLED,
    }[paypal_status]


def _record_paypal_tracking(order, resource, now):
    status = _shipment_status(resource["status"])
    shipment, created = Shipment.objects.get_or_create(
        order=order,
        carrier=resource["carrier"],
        tracking_number=resource["tracking_number"],
        defaults={
            "status": status,
            "source": Shipment.Source.PAYPAL,
        },
    )
    if not created:
        shipment.status = status
    if status == Shipment.Status.SHIPPED and not shipment.shipped_at:
        shipment.shipped_at = now
    if status == Shipment.Status.DELIVERED and not shipment.delivered_at:
        shipment.delivered_at = now
    shipment.save()
    if status in {Shipment.Status.SHIPPED, Shipment.Status.DELIVERED}:
        order.status = Order.Status.SHIPPED
        order.shipped_at = order.shipped_at or now
        order.save(update_fields=("status", "shipped_at", "updated_at"))


@transaction.atomic
def _record_webhook_event(order_id, event_id, event_type, resource_id):
    order = Order.objects.select_for_update().get(pk=order_id)
    event_key = f"paypal-webhook:{event_id}"
    existing = OrderEvent.objects.filter(event_key=event_key).first()
    if existing:
        return existing.order
    _event(
        order,
        event_type,
        OrderEvent.Source.PAYPAL,
        {"paypal_event_id": event_id, "resource_id": resource_id},
        event_key,
    )
    return order


def process_paypal_webhook(
    headers,
    event,
    client: PayPalClient,
    inventory: InventoryGateway,
):
    if not client.verify_webhook_signature(headers, event):
        raise WebhookVerificationError("PayPal webhook signature is invalid.")

    event_id = event["id"]
    existing = OrderEvent.objects.select_related("order").filter(
        event_key=f"paypal-webhook:{event_id}"
    ).first()
    if existing:
        return existing.order

    event_type = event["event_type"]
    resource = event["resource"]
    now = timezone.now()
    if event_type == "CHECKOUT.ORDER.APPROVED":
        order = Order.objects.get(paypal_order_id=resource["id"])
        order = capture_paypal_order(order.pk, client, inventory)
        resource_id = resource["id"]
    elif event_type == "CHECKOUT.ORDER.VOIDED":
        order = Order.objects.get(paypal_order_id=resource["id"])
        resource_id = resource["id"]
        if order.status in {
            Order.Status.AWAITING_PAYMENT,
            Order.Status.PAYMENT_PROCESSING,
            Order.Status.CAPTURE_PENDING,
        }:
            cancel_order(order.pk, inventory, capture_definitely_absent=True)
            with transaction.atomic():
                order = Order.objects.select_for_update().get(pk=order.pk)
                order.status = Order.Status.EXPIRED
                order.paypal_status = resource["status"]
                order.save(update_fields=("status", "paypal_status", "updated_at"))
    elif event_type == "PAYMENT.CAPTURE.COMPLETED":
        paypal_order_id = resource["supplementary_data"]["related_ids"]["order_id"]
        order = Order.objects.get(paypal_order_id=paypal_order_id)
        if resource["status"] != "COMPLETED":
            raise PaymentDataError("PayPal capture is not complete.")
        _validate_amount(order, resource["amount"])
        order = _mark_paid(order.pk, resource)
        resource_id = resource["id"]
    elif event_type == "PAYMENT.CAPTURE.REFUNDED":
        capture_id = resource["supplementary_data"]["related_ids"]["capture_id"]
        with transaction.atomic():
            order = Order.objects.select_for_update().get(paypal_capture_id=capture_id)
            if resource["amount"]["currency_code"] != order.currency:
                raise PaymentDataError("PayPal refund currency does not match the order.")
            _apply_refund(
                order, resource["id"], Decimal(resource["amount"]["value"]), now
            )
        resource_id = resource["id"]
    elif event_type in {
        "SHIPPING.TRACKING.CREATED",
        "SHIPPING.TRACKING.UPDATED",
        "SHIPPING.TRACKING.CANCELLED",
    }:
        with transaction.atomic():
            order = Order.objects.select_for_update().get(
                paypal_capture_id=resource["transaction_id"]
            )
            _record_paypal_tracking(order, resource, now)
        resource_id = f"{resource['transaction_id']}:{resource['tracking_number']}"
    else:
        return None

    return _record_webhook_event(order.pk, event_id, event_type, resource_id)


@transaction.atomic
def record_manual_shipment(
    order_id, carrier, tracking_number, status=Shipment.Status.SHIPPED
):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status not in {
        Order.Status.PAID,
        Order.Status.FULFILLING,
        Order.Status.SHIPPED,
        Order.Status.PARTIALLY_REFUNDED,
    }:
        raise OrderStateError("Only paid orders can be shipped.")
    if status not in Shipment.Status.values:
        raise ValueError("Shipment status is invalid.")
    now = timezone.now()
    shipment, created = Shipment.objects.get_or_create(
        order=order,
        carrier=carrier,
        tracking_number=tracking_number,
        defaults={"status": status, "source": Shipment.Source.MANUAL},
    )
    previous_status = None if created else shipment.status
    shipment.status = status
    if status in {Shipment.Status.SHIPPED, Shipment.Status.DELIVERED}:
        shipment.shipped_at = shipment.shipped_at or now
    if status == Shipment.Status.DELIVERED:
        shipment.delivered_at = shipment.delivered_at or now
    shipment.save()
    if status == Shipment.Status.LABEL_CREATED and order.status == Order.Status.PAID:
        order.status = Order.Status.FULFILLING
        order.save(update_fields=("status", "updated_at"))
    if status in {Shipment.Status.SHIPPED, Shipment.Status.DELIVERED}:
        order.status = Order.Status.SHIPPED
        order.shipped_at = order.shipped_at or now
        order.save(update_fields=("status", "shipped_at", "updated_at"))
    if created or previous_status != status:
        _event(
            order,
            "shipment.recorded" if created else "shipment.updated",
            OrderEvent.Source.ADMIN,
            {
                "carrier": carrier,
                "tracking_number": tracking_number,
                "status": status,
            },
        )
    return shipment
