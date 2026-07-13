import base64
import uuid
from urllib.parse import quote_plus

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q


def generate_order_reference():
    token = base64.b32encode(uuid.uuid4().bytes[:6]).decode().rstrip("=")
    return f"FM-{token}"


class Order(models.Model):
    class Status(models.TextChoices):
        AWAITING_PAYMENT = "awaiting_payment", "Awaiting payment"
        PAYMENT_PROCESSING = "payment_processing", "Processing payment"
        CAPTURE_PENDING = "capture_pending", "Confirming payment"
        PAID = "paid", "Paid"
        FULFILLING = "fulfilling", "Fulfilling"
        SHIPPED = "shipped", "Shipped"
        PARTIALLY_REFUNDED = "partially_refunded", "Partially refunded"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"
        REFUNDED = "refunded", "Refunded"

    reference = models.CharField(
        max_length=13, unique=True, default=generate_order_reference, editable=False
    )
    status_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    checkout_key = models.UUIDField(unique=True, editable=False)
    checkout_fingerprint = models.CharField(max_length=64, editable=False)
    status = models.CharField(
        max_length=24, choices=Status, default=Status.AWAITING_PAYMENT
    )
    customer_email = models.EmailField()
    customer_name = models.CharField(max_length=300)
    customer_phone = models.CharField(max_length=40, blank=True)
    shipping_line_1 = models.CharField(max_length=300)
    shipping_line_2 = models.CharField(max_length=300, blank=True)
    shipping_city = models.CharField(max_length=120)
    shipping_region = models.CharField(max_length=120)
    shipping_postal_code = models.CharField(max_length=32)
    shipping_country_code = models.CharField(max_length=2)
    currency = models.CharField(max_length=3, default="USD")
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    shipping_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    refunded_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    paypal_order_id = models.CharField(
        max_length=64, unique=True, null=True, blank=True
    )
    paypal_capture_id = models.CharField(
        max_length=64, unique=True, null=True, blank=True
    )
    paypal_refund_id = models.CharField(
        max_length=64, unique=True, null=True, blank=True
    )
    paypal_status = models.CharField(max_length=40, blank=True)
    expires_at = models.DateTimeField()
    paid_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    shipped_at = models.DateTimeField(null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(
                condition=Q(subtotal__gte=0), name="order_subtotal_nonnegative"
            ),
            models.CheckConstraint(
                condition=Q(shipping_total__gte=0),
                name="order_shipping_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(total=F("subtotal") + F("shipping_total")),
                name="order_total_matches_parts",
            ),
            models.CheckConstraint(
                condition=Q(refunded_total__gte=0)
                & Q(refunded_total__lte=F("total")),
                name="order_refund_within_total",
            ),
        ]

    def __str__(self):
        return self.reference


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="items")
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.SET_NULL,
        related_name="order_items",
        null=True,
        blank=True,
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.SET_NULL,
        related_name="order_items",
        null=True,
        blank=True,
    )
    ebay_item_id = models.CharField(max_length=32)
    variation_sku = models.CharField(max_length=100, blank=True)
    variation_title = models.CharField(max_length=255, blank=True)
    title = models.CharField(max_length=255)
    condition = models.CharField(max_length=120, blank=True)
    image_url = models.URLField(max_length=1000, blank=True)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("id",)
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="order_item_quantity_positive"
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gte=0),
                name="order_item_price_nonnegative",
            ),
        ]

    @property
    def line_total(self):
        return self.unit_price * self.quantity

    def clean(self):
        if (
            self.variant_id
            and self.product_id
            and self.variant.product_id != self.product_id
        ):
            raise ValidationError("Order item variant does not belong to its product.")

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Order item snapshots cannot be changed.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Order item snapshots cannot be deleted.")

    def __str__(self):
        return f"{self.order.reference}: {self.title}"


class InventoryReservation(models.Model):
    class Status(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        COMMITTING = "committing", "Committing"
        COMMITTED = "committed", "Committed"
        RELEASING = "releasing", "Releasing"
        RELEASED = "released", "Released"

    order_item = models.OneToOneField(
        OrderItem, on_delete=models.PROTECT, related_name="reservation"
    )
    quantity = models.PositiveIntegerField()
    status = models.CharField(
        max_length=10, choices=Status, default=Status.RESERVED
    )
    expires_at = models.DateTimeField()
    committed_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("expires_at",)
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="reservation_quantity_positive"
            )
        ]

    def __str__(self):
        return f"{self.order_item.order.reference}: {self.status}"


class Shipment(models.Model):
    class Status(models.TextChoices):
        LABEL_CREATED = "label_created", "Label created"
        SHIPPED = "shipped", "Shipped"
        ON_HOLD = "on_hold", "On hold"
        DELIVERED = "delivered", "Delivered"
        CANCELLED = "cancelled", "Cancelled"

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual"
        PAYPAL = "paypal", "PayPal"

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="shipments")
    carrier = models.CharField(max_length=80)
    tracking_number = models.CharField(max_length=120)
    status = models.CharField(max_length=16, choices=Status, default=Status.SHIPPED)
    source = models.CharField(max_length=10, choices=Source, default=Source.MANUAL)
    shipped_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("order", "carrier", "tracking_number"),
                name="unique_order_tracking_number",
            )
        ]

    def __str__(self):
        return f"{self.order.reference}: {self.tracking_number}"

    @property
    def tracking_url(self):
        carrier = self.carrier.casefold()
        number = quote_plus(self.tracking_number)
        if "usps" in carrier:
            return f"https://tools.usps.com/go/TrackConfirmAction?tLabels={number}"
        if "ups" in carrier:
            return f"https://www.ups.com/track?tracknum={number}"
        if "fedex" in carrier or "federal express" in carrier:
            return f"https://www.fedex.com/fedextrack/?trknbr={number}"
        return ""


class Refund(models.Model):
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="refunds")
    paypal_refund_id = models.CharField(max_length=64, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="refund_amount_positive"
            )
        ]

    def __str__(self):
        return f"{self.order.reference}: {self.amount}"


class OrderEvent(models.Model):
    class Source(models.TextChoices):
        SYSTEM = "system", "System"
        PAYPAL = "paypal", "PayPal"
        ADMIN = "admin", "Administrator"

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=80)
    source = models.CharField(max_length=10, choices=Source)
    event_key = models.CharField(max_length=160, unique=True, null=True, blank=True)
    data = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Order events cannot be changed.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Order events cannot be deleted.")

    def __str__(self):
        return f"{self.order.reference}: {self.kind}"
