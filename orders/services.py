import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from catalog.ebay import EbayInventoryConflict
from catalog.models import InventoryOperation, Product, ProductVariant

from .inventory import InventoryGateway, InventoryUnavailable
from .models import (
    InventoryReservation,
    Order,
    OrderEvent,
    OrderItem,
    PayPalCase,
    Refund,
    Shipment,
)
from .paypal import PayPalClient, PayPalInstrumentDeclined


class OrderStateError(ValueError):
    pass


class PayPalOrderInactive(OrderStateError):
    pass


class OrderReservationExpired(OrderStateError):
    pass


class IdempotencyConflict(ValueError):
    pass


class PaymentDataError(ValueError):
    pass


class WebhookVerificationError(ValueError):
    pass


SHIPPABLE_ORDER_STATUSES = frozenset(
    {
        Order.Status.PAID,
        Order.Status.FULFILLING,
        Order.Status.SHIPPED,
        Order.Status.PARTIALLY_REFUNDED,
    }
)
PENDING_SHIPMENT_STATUSES = frozenset(
    {Shipment.Status.LABEL_CREATED, Shipment.Status.ON_HOLD}
)
FINAL_SHIPMENT_STATUSES = frozenset(
    {Shipment.Status.SHIPPED, Shipment.Status.DELIVERED}
)
TERMINAL_PAYPAL_SHIPMENT_STATUSES = frozenset(
    {Shipment.Status.CANCELLED, Shipment.Status.DELIVERED}
)
PAYPAL_REFUND_STATUSES = {
    "PENDING": Refund.Status.PENDING,
    "COMPLETED": Refund.Status.COMPLETED,
    "FAILED": Refund.Status.FAILED,
    "CANCELLED": Refund.Status.CANCELLED,
}
REFUND_REVIEW_STATUSES = frozenset(
    {Refund.Status.PENDING, Refund.Status.FAILED, Refund.Status.CANCELLED}
)
PAYPAL_DISPUTE_EVENT_TYPES = frozenset(
    {
        "CUSTOMER.DISPUTE.CREATED",
        "CUSTOMER.DISPUTE.UPDATED",
        "CUSTOMER.DISPUTE.RESOLVED",
    }
)


def refunds_needing_review(queryset=None):
    queryset = Refund.objects.all() if queryset is None else queryset
    return queryset.filter(
        status__in=REFUND_REVIEW_STATUSES,
        order__refunded_total__lt=F("order__total"),
    )


def paypal_cases_needing_review(queryset=None):
    queryset = PayPalCase.objects.all() if queryset is None else queryset
    return queryset.filter(needs_review=True)


def orders_accepting_shipments(queryset=None):
    queryset = Order.objects.all() if queryset is None else queryset
    return (
        queryset.filter(status__in=SHIPPABLE_ORDER_STATUSES)
        .exclude(refunds__status=Refund.Status.PENDING)
        .exclude(paypal_cases__needs_review=True)
    )


def orders_needing_fulfillment(queryset=None):
    queryset = orders_accepting_shipments(queryset)
    completed = Shipment.objects.filter(
        order_id=OuterRef("pk"),
        completes_order=True,
        status__in=FINAL_SHIPMENT_STATUSES,
    )
    return (
        queryset.annotate(fulfillment_complete=Exists(completed))
        .filter(
            Q(status__in={Order.Status.PAID, Order.Status.FULFILLING})
            | Q(
                status=Order.Status.PARTIALLY_REFUNDED,
                fulfillment_complete=False,
            )
            | Q(
                status__in={
                    Order.Status.SHIPPED,
                    Order.Status.PARTIALLY_REFUNDED,
                },
                shipments__status__in=PENDING_SHIPMENT_STATUSES,
            )
        )
        .distinct()
    )


def orders_needing_paypal_tracking(queryset=None):
    queryset = Order.objects.all() if queryset is None else queryset
    paypal_shipments = Shipment.objects.filter(
        order_id=OuterRef("pk"), source=Shipment.Source.PAYPAL
    )
    active_paypal_shipments = paypal_shipments.exclude(
        status__in=TERMINAL_PAYPAL_SHIPMENT_STATUSES
    )
    return (
        queryset.filter(
            paid_at__isnull=False,
            paypal_order_id__isnull=False,
            paypal_capture_id__isnull=False,
        )
        .annotate(
            has_paypal_shipment=Exists(paypal_shipments),
            has_active_paypal_shipment=Exists(active_paypal_shipments),
        )
        .filter(
            Q(has_active_paypal_shipment=True)
            | Q(
                has_paypal_shipment=False,
                status__in={
                    Order.Status.PAID,
                    Order.Status.FULFILLING,
                    Order.Status.PARTIALLY_REFUNDED,
                },
            )
        )
    )


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


def _paypal_object(value, name):
    if not isinstance(value, dict):
        raise PaymentDataError(f"PayPal {name} is invalid.")
    return value


def _paypal_list(value, name):
    if not isinstance(value, list):
        raise PaymentDataError(f"PayPal {name} is invalid.")
    return value


def _paypal_text(value, name):
    if not isinstance(value, str) or not value:
        raise PaymentDataError(f"PayPal {name} is invalid.")
    return value


def _checkout_fingerprint(
    email, address, currency, shipping_total, expected_total, lines
):
    payload = {
        "email": email,
        "address": asdict(address),
        "currency": currency,
        "shipping_total": _money(shipping_total),
        "expected_total": _money(expected_total),
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
    expected_total: Decimal,
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
    fingerprint = _checkout_fingerprint(
        email, address, currency, shipping_total, expected_total, lines
    )
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
    pricing_quantities = {
        product_id: sum(
            line.quantity for line in lines if line.product_id == product_id
        )
        for product_id in products
    }
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
            ):
                raise InventoryUnavailable("A selected product option is unavailable.")
            price = product.direct_price_for(
                pricing_quantities[product.pk], variant.price
            )
        else:
            if active_variants:
                raise InventoryUnavailable("A product option is required.")
            price = product.direct_price_for(pricing_quantities[product.pk])
        currencies.add(product.currency)
        image = product.images.first()
        snapshots.append((line, product, variant, price, image.url if image else ""))
    if currencies != {"USD"}:
        raise ValueError("Checkout requires USD catalog prices.")

    subtotal = sum(
        (price * line.quantity for line, _, _, price, _ in snapshots),
        start=Decimal("0.00"),
    )
    total = subtotal + shipping_total
    if total != expected_total:
        raise InventoryUnavailable(
            "The order total changed. Refresh checkout and review the updated total."
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
        total=total,
        expires_at=expires_at,
    )
    order.full_clean(validate_unique=False)
    try:
        with transaction.atomic():
            order.save()
    except IntegrityError:
        existing = Order.objects.filter(checkout_key=checkout_key).first()
        if existing is None:
            raise
        if existing.checkout_fingerprint != fingerprint:
            raise IdempotencyConflict("Checkout key was already used for another order.")
        return existing

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
        response = _paypal_object(client.get_order(order.paypal_order_id), "order")
        paypal_order_id = _paypal_text(response.get("id"), "order ID")
        paypal_status = _paypal_text(response.get("status"), "order status")
        if paypal_order_id != order.paypal_order_id:
            raise PaymentDataError("PayPal order identity does not match the order.")
        order.paypal_status = paypal_status
        order.save(update_fields=("paypal_status", "updated_at"))
        if paypal_status == "VOIDED":
            raise PayPalOrderInactive("The previous PayPal checkout has ended.")
        return order
    if order.expires_at <= timezone.now():
        raise OrderReservationExpired("The inventory reservation has expired.")
    _validate_us_destination(order.shipping_country_code, order.shipping_region)

    response = _paypal_object(
        client.create_order(
            _paypal_payload(order, return_url, cancel_url),
            request_id=f"create-{order.reference}",
        ),
        "order",
    )
    paypal_order_id = _paypal_text(response.get("id"), "order ID")
    paypal_status = _paypal_text(response.get("status"), "order status")
    if paypal_status not in {
        "CREATED",
        "PAYER_ACTION_REQUIRED",
    }:
        raise PaymentDataError("PayPal did not create a payable order.")
    order.paypal_order_id = paypal_order_id
    order.paypal_status = paypal_status
    order.save(update_fields=("paypal_order_id", "paypal_status", "updated_at"))
    _event(
        order,
        "paypal.order_created",
        OrderEvent.Source.PAYPAL,
        {"paypal_order_id": order.paypal_order_id, "status": order.paypal_status},
        f"paypal-order:{order.paypal_order_id}:created",
    )
    return order


def _paypal_amount(amount):
    if not isinstance(amount, dict):
        raise PaymentDataError("PayPal amount is invalid.")
    currency = amount.get("currency_code")
    value = amount.get("value")
    if not isinstance(currency, str) or not isinstance(value, str):
        raise PaymentDataError("PayPal amount is invalid.")
    try:
        parsed_value = Decimal(value)
    except InvalidOperation as error:
        raise PaymentDataError("PayPal amount is invalid.") from error
    if not parsed_value.is_finite():
        raise PaymentDataError("PayPal amount is invalid.")
    return currency, parsed_value


def _validate_amount(order, amount):
    currency, parsed_value = _paypal_amount(amount)
    if currency != order.currency:
        raise PaymentDataError("PayPal currency does not match the order.")
    if parsed_value != order.total:
        raise PaymentDataError("PayPal amount does not match the order.")


def _validate_paypal_order(order, response):
    response = _paypal_object(response, "order")
    paypal_order_id = _paypal_text(response.get("id"), "order ID")
    intent = _paypal_text(response.get("intent"), "order intent")
    paypal_status = _paypal_text(response.get("status"), "order status")
    if paypal_order_id != order.paypal_order_id or intent != "CAPTURE":
        raise PaymentDataError("PayPal order identity does not match the order.")
    if paypal_status not in {"APPROVED", "COMPLETED"}:
        raise PaymentDataError("PayPal has not approved this order.")
    purchase_units = _paypal_list(response.get("purchase_units"), "purchase units")
    if len(purchase_units) != 1:
        raise PaymentDataError("PayPal order must contain one purchase unit.")
    purchase = _paypal_object(purchase_units[0], "purchase unit")
    for field in ("reference_id", "custom_id", "invoice_id"):
        if _paypal_text(purchase.get(field), "order reference") != order.reference:
            raise PaymentDataError("PayPal reference does not match the order.")
    _validate_amount(order, purchase.get("amount"))
    shipping = _paypal_object(purchase.get("shipping"), "shipping data")
    name = _paypal_object(shipping.get("name"), "shipping name")
    full_name = _paypal_text(name.get("full_name"), "shipping name")
    if full_name.strip() != order.customer_name.strip():
        raise PaymentDataError("PayPal shipping name does not match the order.")
    address = _paypal_object(shipping.get("address"), "shipping address")
    expected = {
        "address_line_1": order.shipping_line_1,
        "address_line_2": order.shipping_line_2,
        "admin_area_2": order.shipping_city,
        "admin_area_1": order.shipping_region,
        "postal_code": order.shipping_postal_code,
        "country_code": order.shipping_country_code,
    }
    for key, value in expected.items():
        actual = address.get(key, "")
        if not isinstance(actual, str) or actual.strip() != value.strip():
            raise PaymentDataError("PayPal shipping address does not match the order.")
    _validate_us_destination(address["country_code"], address["admin_area_1"])
    return purchase


def _capture_from_order(order, response):
    response = _paypal_object(response, "captured order")
    paypal_order_id = _paypal_text(response.get("id"), "order ID")
    paypal_status = _paypal_text(response.get("status"), "order status")
    if paypal_order_id != order.paypal_order_id or paypal_status != "COMPLETED":
        raise PaymentDataError("PayPal order capture is not complete.")
    purchase_units = _paypal_list(response.get("purchase_units"), "purchase units")
    if len(purchase_units) != 1:
        raise PaymentDataError("PayPal order must contain one purchase unit.")
    purchase = _paypal_object(purchase_units[0], "purchase unit")
    payments = _paypal_object(purchase.get("payments"), "payment data")
    captures = _paypal_list(payments.get("captures"), "captures")
    if len(captures) != 1:
        raise PaymentDataError("PayPal did not return one capture.")
    capture = _paypal_object(captures[0], "capture")
    _paypal_text(capture.get("id"), "capture ID")
    capture_status = _paypal_text(capture.get("status"), "capture status")
    if capture_status not in {
        "COMPLETED",
        "DECLINED",
        "PARTIALLY_REFUNDED",
        "PENDING",
        "REFUNDED",
        "FAILED",
    }:
        raise PaymentDataError("PayPal returned an unsupported capture status.")
    _validate_amount(order, capture.get("amount"))
    return capture


@transaction.atomic
def _begin_payment_processing(order_id, paypal_status):
    order = Order.objects.select_for_update().get(pk=order_id)
    update_fields = ["paypal_status"]
    if order.status in {
        Order.Status.AWAITING_PAYMENT,
        Order.Status.FUNDING_RETRY,
    }:
        if order.expires_at <= timezone.now():
            raise OrderReservationExpired("The inventory reservation has expired.")
        order.status = Order.Status.PAYMENT_PROCESSING
        update_fields.extend(("status", "updated_at"))
    elif order.status != Order.Status.PAYMENT_PROCESSING:
        raise OrderStateError("This order cannot be captured.")
    order.paypal_status = paypal_status
    order.save(update_fields=update_fields)


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
def _mark_funding_retry(order_id):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status == Order.Status.FUNDING_RETRY:
        return order
    if order.status != Order.Status.CAPTURE_PENDING:
        raise OrderStateError("This order cannot request another payment method.")
    order.status = Order.Status.FUNDING_RETRY
    order.paypal_status = "INSTRUMENT_DECLINED"
    order.expires_at = timezone.now() + timedelta(
        minutes=settings.ORDER_RESERVATION_MINUTES
    )
    order.save(
        update_fields=("status", "paypal_status", "expires_at", "updated_at")
    )
    _event(
        order,
        "payment.funding_source_declined",
        OrderEvent.Source.PAYPAL,
        {"paypal_order_id": order.paypal_order_id},
    )
    return order


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
    if capture["status"] == "PARTIALLY_REFUNDED" and order.refunded_total <= 0:
        raise PaymentDataError("PayPal refund details have not been received.")
    if capture["status"] == "REFUNDED" and order.refunded_total != order.total:
        raise PaymentDataError("PayPal refund details have not been received.")
    order.paypal_capture_id = capture["id"]
    order.paypal_status = capture["status"]
    now = timezone.now()
    order.paid_at = now
    order.save(
        update_fields=(
            "paypal_capture_id",
            "paypal_status",
            "paid_at",
            "updated_at",
        )
    )
    _refresh_fulfillment_status(order, now)
    _event(
        order,
        "payment.captured",
        OrderEvent.Source.PAYPAL,
        {"capture_id": capture["id"], "amount": capture["amount"]},
        f"paypal-capture:{capture['id']}:completed",
    )
    return order


@transaction.atomic
def _record_incomplete_capture(order_id, capture):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.paid_at:
        if order.paypal_capture_id != capture["id"]:
            raise PaymentDataError("Order already has a different PayPal capture.")
        return order
    if capture["status"] == "PENDING" and InventoryReservation.objects.filter(
        order_item__order=order
    ).exclude(status=InventoryReservation.Status.COMMITTED).exists():
        raise PaymentDataError("Pending payment requires committed inventory.")
    if order.paypal_capture_id and order.paypal_capture_id != capture["id"]:
        raise PaymentDataError("Order already has a different PayPal capture.")
    order.paypal_capture_id = capture["id"]
    order.paypal_status = capture["status"]
    order.save(update_fields=("paypal_capture_id", "paypal_status"))
    event_key = f"paypal-capture:{capture['id']}:{capture['status'].lower()}"
    if not OrderEvent.objects.filter(event_key=event_key).exists():
        _event(
            order,
            f"payment.capture_{capture['status'].lower()}",
            OrderEvent.Source.PAYPAL,
            {"capture_id": capture["id"], "amount": capture["amount"]},
            event_key,
        )
    return order


def _apply_capture_result(order_id, capture, inventory):
    if capture["status"] in {"COMPLETED", "PARTIALLY_REFUNDED", "REFUNDED"}:
        return _mark_paid(order_id, capture)
    order = _record_incomplete_capture(order_id, capture)
    if capture["status"] in {"DECLINED", "FAILED"}:
        return cancel_order(order.pk, inventory, capture_definitely_absent=True)
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
        Order.Status.FUNDING_RETRY,
    }:
        raise OrderStateError("This order cannot be captured.")
    _validate_us_destination(order.shipping_country_code, order.shipping_region)

    paypal_order = _paypal_object(client.get_order(order.paypal_order_id), "order")
    paypal_order_id = _paypal_text(paypal_order.get("id"), "order ID")
    paypal_status = _paypal_text(paypal_order.get("status"), "order status")
    if paypal_order_id != order.paypal_order_id:
        raise PaymentDataError("PayPal order identity does not match the order.")
    if paypal_status == "VOIDED":
        cancel_order(order.pk, inventory, capture_definitely_absent=True)
        raise PayPalOrderInactive("The PayPal checkout has ended.")
    _validate_paypal_order(order, paypal_order)
    if paypal_status == "COMPLETED":
        return _apply_capture_result(
            order.pk, _capture_from_order(order, paypal_order), inventory
        )

    if order.status != Order.Status.CAPTURE_PENDING:
        _begin_payment_processing(order.pk, paypal_status)
        try:
            _commit_reservations(order.pk, inventory)
        except (EbayInventoryConflict, InventoryUnavailable):
            _reset_failed_commits(order.pk)
            cancel_order(order.pk, inventory)
            raise
        _mark_capture_pending(order.pk)
    try:
        response = client.capture_order(
            order.paypal_order_id, request_id=f"capture-{order.reference}"
        )
    except PayPalInstrumentDeclined:
        _mark_funding_retry(order.pk)
        raise
    return _apply_capture_result(
        order.pk, _capture_from_order(order, response), inventory
    )


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
            if reservation.status != InventoryReservation.Status.RELEASING:
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
            allowed.update(
                {Order.Status.CAPTURE_PENDING, Order.Status.FUNDING_RETRY}
            )
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


@transaction.atomic
def _mark_expired(order_id, paypal_status):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status == Order.Status.EXPIRED:
        return order
    if order.status != Order.Status.CANCELLED:
        raise OrderStateError("Only a cancelled order can expire.")
    order.status = Order.Status.EXPIRED
    order.paypal_status = paypal_status
    order.save(update_fields=("status", "paypal_status", "updated_at"))
    _event(order, "order.expired", OrderEvent.Source.SYSTEM)
    return order


def reconcile_due_funding_retry(
    order_id, client: PayPalClient, inventory: InventoryGateway, now=None
):
    now = now or timezone.now()
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        if order.status != Order.Status.FUNDING_RETRY or order.expires_at > now:
            raise OrderStateError("This payment-method retry is not due for expiration.")
    response = _paypal_object(client.get_order(order.paypal_order_id), "order")
    paypal_order_id = _paypal_text(response.get("id"), "order ID")
    paypal_status = _paypal_text(response.get("status"), "order status")
    if paypal_order_id != order.paypal_order_id:
        raise PaymentDataError("PayPal order identity does not match the order.")
    if paypal_status == "COMPLETED":
        _validate_paypal_order(order, response)
        return _apply_capture_result(
            order.pk, _capture_from_order(order, response), inventory
        )
    if paypal_status not in {"CREATED", "PAYER_ACTION_REQUIRED", "APPROVED", "VOIDED"}:
        raise PaymentDataError("PayPal returned an unsupported order status.")
    cancel_order(order.pk, inventory, capture_definitely_absent=True)
    return _mark_expired(order.pk, paypal_status)


def _apply_refund(order, refund_id, amount, now):
    existing = Refund.objects.filter(paypal_refund_id=refund_id).first()
    if existing:
        if existing.order_id != order.pk or existing.amount != amount:
            raise PaymentDataError("PayPal refund data does not match its prior record.")
        if existing.status == Refund.Status.COMPLETED:
            return False
        existing.status = Refund.Status.COMPLETED
        existing.save(update_fields=("status", "updated_at"))
    if amount <= 0 or order.refunded_total + amount > order.total:
        raise PaymentDataError("PayPal refund amount is invalid.")
    if existing is None:
        Refund.objects.create(
            order=order,
            paypal_refund_id=refund_id,
            amount=amount,
            status=Refund.Status.COMPLETED,
        )
    order.refunded_total += amount
    order.paypal_refund_id = refund_id
    if order.refunded_total == order.total:
        order.refunded_at = now
    if order.paid_at:
        order.status = (
            Order.Status.REFUNDED
            if order.refunded_total == order.total
            else Order.Status.PARTIALLY_REFUNDED
        )
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


def _validated_refund_response(order, response, amount):
    response = _paypal_object(response, "refund")
    refund_id = _paypal_text(response.get("id"), "refund ID")
    provider_status = _paypal_text(response.get("status"), "refund status")
    if provider_status not in PAYPAL_REFUND_STATUSES:
        raise PaymentDataError("PayPal refund status is invalid.")
    currency, refund_amount = _paypal_amount(response.get("amount"))
    if currency != order.currency or refund_amount != amount:
        raise PaymentDataError("PayPal refund amount does not match the request.")
    return response, refund_id, PAYPAL_REFUND_STATUSES[provider_status]


@transaction.atomic
def refund_order(order_id, client: PayPalClient):
    order = Order.objects.select_for_update().get(pk=order_id)
    if not order.paypal_capture_id or not order.paid_at:
        raise OrderStateError("Only captured orders can be refunded.")
    if order.refunded_total == order.total:
        return order
    unresolved = order.refunds.filter(status=Refund.Status.PENDING).first()
    if unresolved:
        order.paypal_refund_id = unresolved.paypal_refund_id
        order.save(update_fields=("paypal_refund_id", "updated_at"))
        return order
    if order.paypal_cases.filter(needs_review=True).exists():
        raise OrderStateError("Review the open PayPal case before refunding this order.")
    amount = order.total - order.refunded_total
    attempt = order.refunds.count() + 1
    response = client.refund_capture(
        order.paypal_capture_id,
        _money(amount),
        order.currency,
        f"{order.reference}-REFUND-{attempt}",
        request_id=f"refund-{order.reference}-{attempt}",
    )
    response, refund_id, refund_status = _validated_refund_response(
        order, response, amount
    )
    if refund_status == Refund.Status.COMPLETED:
        _apply_refund(order, refund_id, amount, timezone.now())
    else:
        Refund.objects.create(
            order=order,
            paypal_refund_id=refund_id,
            amount=amount,
            status=refund_status,
        )
        order.paypal_refund_id = refund_id
        order.save(update_fields=("paypal_refund_id", "updated_at"))
    _event(
        order,
        (
            "payment.refunded"
            if refund_status == Refund.Status.COMPLETED
            else f"payment.refund_{refund_status}"
        ),
        OrderEvent.Source.PAYPAL,
        {"refund_id": refund_id, "amount": response["amount"]},
        f"paypal-refund:{refund_id}:{refund_status}",
    )
    return order


def reconcile_pending_refund(refund_id, client: PayPalClient):
    refund = Refund.objects.select_related("order").get(pk=refund_id)
    if refund.status != Refund.Status.PENDING:
        return refund
    response, paypal_refund_id, status = _validated_refund_response(
        refund.order,
        client.get_refund(refund.paypal_refund_id),
        refund.amount,
    )
    if paypal_refund_id != refund.paypal_refund_id:
        raise PaymentDataError("PayPal refund identity does not match the order.")
    if _refund_capture_id(response) != refund.order.paypal_capture_id:
        raise PaymentDataError("PayPal refund capture does not match the order.")
    if status == Refund.Status.PENDING:
        Refund.objects.filter(pk=refund_id, status=Refund.Status.PENDING).update(
            updated_at=timezone.now()
        )
        return Refund.objects.get(pk=refund_id)
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=refund.order_id)
        refund = Refund.objects.select_for_update().get(pk=refund_id)
        if refund.status != Refund.Status.PENDING:
            return refund
        if status == Refund.Status.COMPLETED:
            _apply_refund(order, refund.paypal_refund_id, refund.amount, timezone.now())
        else:
            refund.status = status
            refund.save(update_fields=("status", "updated_at"))
        _event(
            order,
            (
                "payment.refunded"
                if status == Refund.Status.COMPLETED
                else f"payment.refund_{status}"
            ),
            OrderEvent.Source.PAYPAL,
            {"refund_id": refund.paypal_refund_id, "amount": response["amount"]},
            f"paypal-refund:{refund.paypal_refund_id}:{status}",
        )
    return Refund.objects.get(pk=refund_id)


def _shipment_status(paypal_status):
    statuses = {
        "CANCELLED": Shipment.Status.CANCELLED,
        "DELIVERED": Shipment.Status.DELIVERED,
        "LOCAL_PICKUP": Shipment.Status.DELIVERED,
        "ON_HOLD": Shipment.Status.ON_HOLD,
        "SHIPPED": Shipment.Status.SHIPPED,
        "SHIPMENT_CREATED": Shipment.Status.LABEL_CREATED,
        "DROPPED_OFF": Shipment.Status.SHIPPED,
        "IN_TRANSIT": Shipment.Status.SHIPPED,
        "RETURNED": Shipment.Status.ON_HOLD,
        "LABEL_PRINTED": Shipment.Status.LABEL_CREATED,
        "ERROR": Shipment.Status.ON_HOLD,
        "UNCONFIRMED": Shipment.Status.ON_HOLD,
        "PICKUP_FAILED": Shipment.Status.ON_HOLD,
        "DELIVERY_DELAYED": Shipment.Status.ON_HOLD,
        "DELIVERY_SCHEDULED": Shipment.Status.SHIPPED,
        "DELIVERY_FAILED": Shipment.Status.ON_HOLD,
        "INRETURN": Shipment.Status.ON_HOLD,
        "IN_PROCESS": Shipment.Status.LABEL_CREATED,
        "NEW": Shipment.Status.LABEL_CREATED,
        "VOID": Shipment.Status.CANCELLED,
        "PROCESSED": Shipment.Status.LABEL_CREATED,
        "NOT_SHIPPED": Shipment.Status.LABEL_CREATED,
        "COMPLETED": Shipment.Status.DELIVERED,
    }
    if paypal_status not in statuses:
        raise PaymentDataError("PayPal shipment status is invalid.")
    return statuses[paypal_status]


def _refund_capture_id(resource):
    resource_links = resource.get("links")
    if not isinstance(resource_links, list) or any(
        not isinstance(link, dict) for link in resource_links
    ):
        raise PaymentDataError("PayPal refund links are invalid.")
    links = [link for link in resource_links if link.get("rel") == "up"]
    if len(links) != 1:
        raise PaymentDataError("PayPal refund must link to one capture.")
    href = links[0].get("href")
    if not isinstance(href, str):
        raise PaymentDataError("PayPal refund capture link is invalid.")
    url = urlsplit(href)
    path = url.path.split("/")
    if (
        url.scheme != "https"
        or not url.netloc
        or path[1:4] != ["v2", "payments", "captures"]
        or len(path) != 5
        or not path[4]
    ):
        raise PaymentDataError("PayPal refund capture link is invalid.")
    return path[4]


def _refresh_fulfillment_status(order, now):
    active_shipments = list(
        order.shipments.exclude(status=Shipment.Status.CANCELLED).values_list(
            "status", "completes_order"
        )
    )
    has_shipped = any(
        status in {Shipment.Status.SHIPPED, Shipment.Status.DELIVERED}
        for status, _ in active_shipments
    )
    fulfillment_complete = any(
        completes_order and status in FINAL_SHIPMENT_STATUSES
        for status, completes_order in active_shipments
    )
    if order.paid_at:
        if order.refunded_total > 0:
            order.status = (
                Order.Status.REFUNDED
                if order.refunded_total == order.total
                else Order.Status.PARTIALLY_REFUNDED
            )
        elif fulfillment_complete:
            order.status = Order.Status.SHIPPED
        elif active_shipments:
            order.status = Order.Status.FULFILLING
        else:
            order.status = Order.Status.PAID
    order.shipped_at = (order.shipped_at or now) if has_shipped else None
    order.save(update_fields=("status", "shipped_at", "updated_at"))


def _record_paypal_tracking(order, resource, now, provider_updated_at):
    paypal_status = resource.get("status")
    if not isinstance(paypal_status, str):
        raise PaymentDataError("PayPal tracking data is missing its status.")
    status = _shipment_status(paypal_status)
    tracking_number = resource.get("tracking_number", "")
    carrier = resource.get("carrier", "")
    if not isinstance(tracking_number, str) or not isinstance(carrier, str):
        raise PaymentDataError("PayPal tracking data is invalid.")
    tracking_number = tracking_number.strip()
    carrier = carrier.strip()
    if tracking_number and not carrier:
        raise PaymentDataError("PayPal tracking data is missing its carrier.")
    if carrier == "OTHER":
        carrier = resource.get("carrier_name_other", "")
        if not isinstance(carrier, str) or not carrier.strip():
            raise PaymentDataError("PayPal tracking data is missing the carrier name.")
        carrier = carrier.strip()
    shipment, created = Shipment.objects.get_or_create(
        order=order,
        tracking_number=tracking_number,
        defaults={
            "carrier": carrier,
            "status": status,
            "source": Shipment.Source.PAYPAL,
            "provider_updated_at": provider_updated_at,
            "completes_order": False,
        },
    )
    if not created:
        if provider_updated_at and (
            shipment.provider_updated_at
            and provider_updated_at <= shipment.provider_updated_at
        ):
            return False
        if provider_updated_at is None and (
            shipment.carrier == carrier
            and shipment.status == status
            and shipment.source == Shipment.Source.PAYPAL
        ):
            return False
        shipment.carrier = carrier
        shipment.status = status
        shipment.source = Shipment.Source.PAYPAL
        if provider_updated_at:
            shipment.provider_updated_at = provider_updated_at
    status_time = provider_updated_at or now
    if (
        status in {Shipment.Status.SHIPPED, Shipment.Status.DELIVERED}
        and not shipment.shipped_at
    ):
        shipment.shipped_at = status_time
    if status not in {Shipment.Status.SHIPPED, Shipment.Status.DELIVERED}:
        shipment.shipped_at = None
    shipment.delivered_at = (
        shipment.delivered_at or status_time
        if status == Shipment.Status.DELIVERED
        else None
    )
    shipment.save()
    _refresh_fulfillment_status(order, now)
    return True


def reconcile_paypal_tracking(order_id, client: PayPalClient):
    order = Order.objects.get(pk=order_id)
    if not order.paid_at:
        raise OrderStateError("Only paid orders can reconcile PayPal tracking.")
    if not order.paypal_order_id or not order.paypal_capture_id:
        raise PaymentDataError("PayPal payment identity is incomplete.")

    paypal_order = _paypal_object(client.get_order(order.paypal_order_id), "order")
    purchase = _validate_paypal_order(order, paypal_order)
    capture = _capture_from_order(order, paypal_order)
    if capture["id"] != order.paypal_capture_id:
        raise PaymentDataError("PayPal capture identity does not match the order.")

    shipping = _paypal_object(purchase.get("shipping"), "shipping data")
    summaries = _paypal_list(shipping.get("trackers", []), "trackers")
    trackers = []
    tracker_ids = set()
    for summary in summaries:
        summary = _paypal_object(summary, "tracker summary")
        tracker_id = _paypal_text(summary.get("id"), "tracker ID")
        if tracker_id in tracker_ids:
            raise PaymentDataError("PayPal returned a duplicate tracker ID.")
        if not tracker_id.startswith(f"{order.paypal_capture_id}-"):
            raise PaymentDataError("PayPal tracker identity does not match the order.")
        tracker_ids.add(tracker_id)

        tracker = _paypal_object(client.get_tracker(tracker_id), "tracker")
        transaction_id = _paypal_text(
            tracker.get("transaction_id"), "tracking transaction ID"
        )
        if transaction_id != order.paypal_capture_id:
            raise PaymentDataError("PayPal tracker transaction does not match the order.")
        tracking_number = tracker.get("tracking_number", "")
        if isinstance(tracking_number, str) and tracking_number:
            if tracker_id != f"{transaction_id}-{tracking_number}":
                raise PaymentDataError("PayPal tracker identity does not match its data.")
        _shipment_status(_paypal_text(tracker.get("status"), "tracking status"))
        last_updated_time = tracker.get("last_updated_time")
        provider_updated_at = None
        if last_updated_time is not None:
            provider_updated_at = parse_datetime(
                _paypal_text(last_updated_time, "tracking update time")
            )
            if provider_updated_at is None or timezone.is_naive(provider_updated_at):
                raise PaymentDataError("PayPal tracking update time is invalid.")
        trackers.append((tracker_id, tracker, provider_updated_at))

    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        if not order.paid_at:
            raise OrderStateError("Only paid orders can reconcile PayPal tracking.")
        if (
            order.paypal_order_id != paypal_order["id"]
            or order.paypal_capture_id != capture["id"]
        ):
            raise PaymentDataError("PayPal payment identity changed during reconciliation.")
        now = timezone.now()
        for tracker_id, tracker, provider_updated_at in trackers:
            if not _record_paypal_tracking(order, tracker, now, provider_updated_at):
                continue
            provider_timestamp = (
                provider_updated_at.isoformat() if provider_updated_at else None
            )
            event_key = None
            if provider_timestamp:
                event_identity = "\0".join(
                    (tracker_id, provider_timestamp, tracker["status"])
                )
                event_key = (
                    "paypal-tracker:"
                    f"{hashlib.sha256(event_identity.encode()).hexdigest()}"
                )
            _event(
                order,
                "shipment.reconciled",
                OrderEvent.Source.PAYPAL,
                {
                    "tracker_id": tracker_id,
                    "tracking_number": tracker.get("tracking_number", ""),
                    "status": tracker["status"],
                    "provider_updated_at": provider_timestamp,
                },
                event_key,
            )
    return order


def _paypal_optional_text(value, name, max_length):
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or len(value) > max_length:
        raise PaymentDataError(f"PayPal {name} is invalid.")
    return value


def _paypal_datetime(value, name):
    if value is None or value == "":
        return None
    parsed = parse_datetime(_paypal_text(value, name))
    if parsed is None or timezone.is_naive(parsed):
        raise PaymentDataError(f"PayPal {name} is invalid.")
    return parsed


def _paypal_case_amount(resource, field_name, order, existing):
    payload = resource.get(field_name)
    if payload is None and existing is not None:
        return existing.currency, existing.amount
    currency, amount = _paypal_amount(payload)
    if currency != order.currency:
        raise PaymentDataError("PayPal case currency does not match the order.")
    if amount <= 0 or amount > order.total:
        raise PaymentDataError("PayPal case amount is invalid.")
    return currency, amount


def _local_order_for_dispute(resource, existing):
    transactions = resource.get("disputed_transactions")
    if transactions is None:
        if existing is not None:
            return existing.order
        raise PaymentDataError("PayPal dispute transactions are invalid.")
    capture_ids = {
        _paypal_text(
            _paypal_object(transaction, "dispute transaction").get(
                "seller_transaction_id"
            ),
            "dispute transaction ID",
        )
        for transaction in _paypal_list(transactions, "dispute transactions")
    }
    if not capture_ids:
        raise PaymentDataError("PayPal dispute transactions are invalid.")
    if existing is not None:
        if existing.order.paypal_capture_id not in capture_ids:
            raise PaymentDataError("PayPal dispute transaction does not match the order.")
        return existing.order
    order_ids = list(
        Order.objects.filter(paypal_capture_id__in=capture_ids)
        .order_by("pk")
        .values_list("pk", flat=True)[:2]
    )
    if not order_ids:
        return None
    if len(order_ids) != 1:
        raise PaymentDataError("PayPal dispute spans multiple store orders.")
    return Order.objects.get(pk=order_ids[0])


@transaction.atomic
def _record_paypal_dispute(resource, event_type, now):
    case_id = _paypal_text(resource.get("dispute_id"), "dispute ID")
    if len(case_id) > 255:
        raise PaymentDataError("PayPal dispute ID is invalid.")
    existing = (
        PayPalCase.objects.select_related("order")
        .filter(kind=PayPalCase.Kind.DISPUTE, paypal_case_id=case_id)
        .first()
    )
    order = _local_order_for_dispute(resource, existing)
    if order is None:
        return None
    order = Order.objects.select_for_update().get(pk=order.pk)
    case = (
        PayPalCase.objects.select_for_update()
        .filter(kind=PayPalCase.Kind.DISPUTE, paypal_case_id=case_id)
        .first()
    )
    if case is not None and case.order_id != order.pk:
        raise PaymentDataError("PayPal dispute identity does not match the order.")
    status = _paypal_text(resource.get("status"), "dispute status")
    if status not in PayPalCase.Status.values or status == PayPalCase.Status.REVERSED:
        raise PaymentDataError("PayPal dispute status is invalid.")
    if (
        event_type == "CUSTOMER.DISPUTE.RESOLVED"
        and status != PayPalCase.Status.RESOLVED
    ):
        raise PaymentDataError("PayPal resolved dispute has an invalid status.")
    provider_created_at = _paypal_datetime(
        resource.get("create_time"), "dispute creation time"
    )
    provider_updated_at = _paypal_datetime(
        resource.get("update_time"), "dispute update time"
    )
    watermark = provider_updated_at or provider_created_at
    if (
        case is not None
        and watermark is not None
        and case.provider_updated_at is not None
        and watermark < case.provider_updated_at
    ):
        return order, case_id
    currency, amount = _paypal_case_amount(
        resource, "dispute_amount", order, case
    )
    reason = _paypal_optional_text(resource.get("reason"), "dispute reason", 64)
    stage = _paypal_optional_text(
        resource.get("dispute_life_cycle_stage"),
        "dispute lifecycle stage",
        32,
    )
    channel = _paypal_optional_text(
        resource.get("dispute_channel"), "dispute channel", 32
    )
    outcome_payload = resource.get("dispute_outcome")
    if outcome_payload is None:
        outcome = case.outcome if case is not None else ""
    else:
        outcome = _paypal_optional_text(
            _paypal_object(outcome_payload, "dispute outcome").get("outcome_code"),
            "dispute outcome",
            64,
        )
    if case is not None:
        reason = reason or case.reason
        stage = stage or case.stage
        channel = channel or case.channel
    if "seller_response_due_date" in resource:
        seller_response_due_at = _paypal_datetime(
            resource.get("seller_response_due_date"),
            "seller response due date",
        )
    else:
        seller_response_due_at = (
            case.seller_response_due_at if case is not None else None
        )
    if case is None:
        case = PayPalCase(
            order=order,
            kind=PayPalCase.Kind.DISPUTE,
            paypal_case_id=case_id,
        )
    case.status = status
    case.reason = reason
    case.outcome = outcome
    case.stage = stage
    case.channel = channel
    case.amount = amount
    case.currency = currency
    case.seller_response_due_at = seller_response_due_at
    case.provider_created_at = (
        provider_created_at or case.provider_created_at
    )
    case.provider_updated_at = watermark or case.provider_updated_at
    case.last_event_type = event_type
    case.needs_review = True
    case.reviewed_at = None
    case.save()
    return order, case_id


@transaction.atomic
def _record_paypal_reversal(resource, event_type, now):
    capture_id = _paypal_text(resource.get("id"), "capture ID")
    order = Order.objects.filter(paypal_capture_id=capture_id).first()
    if order is None:
        return None
    order = Order.objects.select_for_update().get(pk=order.pk)
    case = (
        PayPalCase.objects.select_for_update()
        .filter(kind=PayPalCase.Kind.REVERSAL, paypal_case_id=capture_id)
        .first()
    )
    currency, amount = _paypal_case_amount(resource, "amount", order, case)
    provider_created_at = _paypal_datetime(
        resource.get("create_time"), "capture creation time"
    )
    provider_updated_at = _paypal_datetime(
        resource.get("update_time"), "capture update time"
    )
    watermark = provider_updated_at or provider_created_at
    if (
        case is not None
        and watermark is not None
        and case.provider_updated_at is not None
        and watermark < case.provider_updated_at
    ):
        return order, capture_id
    status_details = resource.get("status_details")
    reason = (
        _paypal_optional_text(
            _paypal_object(status_details, "capture status details").get("reason"),
            "capture reversal reason",
            64,
        )
        if status_details is not None
        else (case.reason if case is not None else "")
    )
    if case is None:
        case = PayPalCase(
            order=order,
            kind=PayPalCase.Kind.REVERSAL,
            paypal_case_id=capture_id,
        )
    case.status = PayPalCase.Status.REVERSED
    case.reason = reason
    case.outcome = ""
    case.stage = ""
    case.channel = ""
    case.amount = amount
    case.currency = currency
    case.seller_response_due_at = None
    case.provider_created_at = (
        provider_created_at or case.provider_created_at
    )
    case.provider_updated_at = watermark or case.provider_updated_at
    case.last_event_type = event_type
    case.needs_review = True
    case.reviewed_at = None
    case.save()
    if order.paypal_status != PayPalCase.Status.REVERSED:
        order.paypal_status = PayPalCase.Status.REVERSED
        order.save(update_fields=("paypal_status", "updated_at"))
    return order, capture_id


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

    event_id = event.get("id")
    event_type = event.get("event_type")
    if not isinstance(event_id, str) or not event_id:
        raise PaymentDataError("PayPal webhook event ID is invalid.")
    if not isinstance(event_type, str) or not event_type:
        raise PaymentDataError("PayPal webhook event type is invalid.")
    existing = OrderEvent.objects.select_related("order").filter(
        event_key=f"paypal-webhook:{event_id}"
    ).first()
    if existing:
        return existing.order

    supported_event_types = {
        "CHECKOUT.ORDER.APPROVED",
        "PAYMENT.CAPTURE.COMPLETED",
        "PAYMENT.CAPTURE.PENDING",
        "PAYMENT.CAPTURE.DECLINED",
        "PAYMENT.CAPTURE.REFUNDED",
        "PAYMENT.CAPTURE.REVERSED",
        *PAYPAL_DISPUTE_EVENT_TYPES,
    }
    if event_type not in supported_event_types:
        return None
    resource = event.get("resource")
    if not isinstance(resource, dict):
        raise PaymentDataError("PayPal webhook resource is invalid.")
    now = timezone.now()
    if event_type == "CHECKOUT.ORDER.APPROVED":
        resource_id = resource.get("id")
        if not isinstance(resource_id, str) or not resource_id:
            raise PaymentDataError("PayPal order ID is invalid.")
        order = Order.objects.get(paypal_order_id=resource_id)
        if order.status not in {Order.Status.CANCELLED, Order.Status.EXPIRED}:
            try:
                order = capture_paypal_order(order.pk, client, inventory)
            except PayPalInstrumentDeclined:
                order.refresh_from_db()
    elif event_type in {
        "PAYMENT.CAPTURE.COMPLETED",
        "PAYMENT.CAPTURE.PENDING",
        "PAYMENT.CAPTURE.DECLINED",
    }:
        supplementary_data = resource.get("supplementary_data")
        related_ids = (
            supplementary_data.get("related_ids")
            if isinstance(supplementary_data, dict)
            else None
        )
        paypal_order_id = (
            related_ids.get("order_id") if isinstance(related_ids, dict) else None
        )
        if not isinstance(paypal_order_id, str) or not paypal_order_id:
            raise PaymentDataError("PayPal capture order ID is invalid.")
        order = Order.objects.get(paypal_order_id=paypal_order_id)
        expected_status = event_type.rsplit(".", 1)[1]
        if resource.get("status") != expected_status:
            raise PaymentDataError("PayPal capture status does not match the event.")
        _validate_amount(order, resource.get("amount"))
        resource_id = resource.get("id")
        if not isinstance(resource_id, str) or not resource_id:
            raise PaymentDataError("PayPal capture ID is invalid.")
        order = _apply_capture_result(order.pk, resource, inventory)
    elif event_type == "PAYMENT.CAPTURE.REFUNDED":
        capture_id = _refund_capture_id(resource)
        order = Order.objects.filter(paypal_capture_id=capture_id).first()
        if order is None:
            return None
        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=order.pk)
            amount = resource.get("amount")
            currency, refund_amount = _paypal_amount(amount)
            resource_id = resource.get("id")
            if not isinstance(resource_id, str) or not resource_id:
                raise PaymentDataError("PayPal refund ID is invalid.")
            if currency != order.currency:
                raise PaymentDataError("PayPal refund currency does not match the order.")
            _apply_refund(order, resource_id, refund_amount, now)
    elif event_type == "PAYMENT.CAPTURE.REVERSED":
        result = _record_paypal_reversal(resource, event_type, now)
        if result is None:
            return None
        order, resource_id = result
    elif event_type in PAYPAL_DISPUTE_EVENT_TYPES:
        result = _record_paypal_dispute(resource, event_type, now)
        if result is None:
            return None
        order, resource_id = result
    return _record_webhook_event(order.pk, event_id, event_type, resource_id)


@transaction.atomic
def record_manual_shipment(
    order_id,
    carrier,
    tracking_number,
    status=Shipment.Status.SHIPPED,
    completes_order=True,
):
    carrier = carrier.strip()
    tracking_number = tracking_number.strip()
    if tracking_number and not carrier:
        raise ValueError("Enter a carrier when a tracking number is provided.")
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status not in SHIPPABLE_ORDER_STATUSES:
        raise OrderStateError("Only paid orders can be shipped.")
    if status not in Shipment.Status.values:
        raise ValueError("Shipment status is invalid.")
    now = timezone.now()
    shipment = Shipment.objects.filter(
        order=order, tracking_number=tracking_number
    ).first()
    if shipment is None:
        if order.refunds.filter(status=Refund.Status.PENDING).exists():
            raise OrderStateError(
                "Orders with a pending refund cannot receive a new shipment."
            )
        if order.paypal_cases.filter(needs_review=True).exists():
            raise OrderStateError(
                "Orders with an unreviewed PayPal case cannot receive "
                "a new shipment."
            )
        shipment = Shipment(
            order=order,
            tracking_number=tracking_number,
            source=Shipment.Source.MANUAL,
        )
        created = True
    else:
        created = False
    previous = (
        None
        if created
        else (shipment.carrier, shipment.status, shipment.completes_order)
    )
    shipment.carrier = carrier
    shipment.status = status
    shipment.completes_order = completes_order
    if status in {Shipment.Status.SHIPPED, Shipment.Status.DELIVERED}:
        shipment.shipped_at = shipment.shipped_at or now
    else:
        shipment.shipped_at = None
    shipment.delivered_at = (
        shipment.delivered_at or now
        if status == Shipment.Status.DELIVERED
        else None
    )
    shipment.save()
    _refresh_fulfillment_status(order, now)
    if created or previous != (carrier, status, completes_order):
        _event(
            order,
            "shipment.recorded" if created else "shipment.updated",
            OrderEvent.Source.ADMIN,
            {
                "carrier": carrier,
                "tracking_number": tracking_number,
                "status": status,
                "completes_order": completes_order,
            },
        )
    return shipment
