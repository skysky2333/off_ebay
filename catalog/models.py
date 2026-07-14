from django.db import models
from django.db.models import Exists, F, OuterRef, Q, Sum, Value
from django.db.models.functions import Coalesce

from .pricing import direct_price as calculate_direct_price


HELD_RESERVATION_STATUSES = ("reserved", "committing")
RECOVERABLE_RESERVATION_STATUSES = (*HELD_RESERVATION_STATUSES, "committed")


class EbayAccountClosure(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    notification_id = models.CharField(max_length=128, unique=True)
    closed_at = models.DateTimeField(auto_now_add=True)


class EbayAccountIdentity(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    username = models.CharField(max_length=100)
    eias_token = models.CharField(max_length=256)
    updated_at = models.DateTimeField(auto_now=True)


class EbayPublicKeyLookupBudget(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    window = models.PositiveBigIntegerField(default=0)
    count = models.PositiveIntegerField(default=0)


class ProductVariantQuerySet(models.QuerySet):
    def with_availability(self, exclude_order_id=None, restore_order_id=None):
        held = Q(
            order_items__reservation__status__in=HELD_RESERVATION_STATUSES
        )
        if exclude_order_id:
            held &= ~Q(order_items__order_id=exclude_order_id)
        annotations = {
            "_held_quantity": Coalesce(
                Sum("order_items__reservation__quantity", filter=held), Value(0)
            )
        }
        if restore_order_id:
            annotations["_owned_checkout_quantity"] = Coalesce(
                Sum(
                    "order_items__reservation__quantity",
                    filter=Q(
                        order_items__order_id=restore_order_id,
                        order_items__reservation__status__in=RECOVERABLE_RESERVATION_STATUSES,
                    ),
                ),
                Value(0),
            )
        return self.annotate(**annotations)


class ProductQuerySet(models.QuerySet):
    def with_availability(self, exclude_order_id=None, restore_order_id=None):
        held = Q(
            order_items__variant__isnull=True,
            order_items__reservation__status__in=HELD_RESERVATION_STATUSES,
        )
        if exclude_order_id:
            held &= ~Q(order_items__order_id=exclude_order_id)
        annotations = {
            "_held_quantity": Coalesce(
                Sum("order_items__reservation__quantity", filter=held), Value(0)
            )
        }
        if restore_order_id:
            annotations["_owned_checkout_quantity"] = Coalesce(
                Sum(
                    "order_items__reservation__quantity",
                    filter=Q(
                        order_items__order_id=restore_order_id,
                        order_items__variant__isnull=True,
                        order_items__reservation__status__in=RECOVERABLE_RESERVATION_STATUSES,
                    ),
                ),
                Value(0),
            )
        return self.annotate(**annotations)

    def purchasable(self):
        active_variants = ProductVariant.objects.with_availability().filter(
            product_id=OuterRef("pk"), active=True
        )
        return self.with_availability().alias(
            has_active_variants=Exists(active_variants),
            has_available_variant=Exists(
                active_variants.filter(
                    purchasable=True, quantity__gt=F("_held_quantity")
                )
            ),
        ).filter(active=True, checkout_excluded=False, currency="USD").filter(
            Q(has_available_variant=True)
            | Q(
                has_active_variants=False,
                quantity__gt=F("_held_quantity"),
            )
        )


class Product(models.Model):
    ebay_item_id = models.CharField(max_length=32, unique=True)
    slug = models.SlugField(max_length=240, unique=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    condition = models.CharField(max_length=120, blank=True)
    category_id = models.CharField(max_length=32, blank=True)
    category_name = models.CharField(max_length=255, blank=True)
    item_specifics = models.JSONField(default=dict)
    shipping = models.JSONField(default=dict)
    listing_url = models.URLField(max_length=500)
    listing_type = models.CharField(max_length=40)
    quantity = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    checkout_excluded = models.BooleanField(default=False)
    ebay_started_at = models.DateTimeField(null=True, blank=True)
    ebay_ends_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ProductQuerySet.as_manager()

    class Meta:
        ordering = ("title",)

    def __str__(self):
        return self.title

    @property
    def available_quantity(self):
        active_variants = [variant for variant in self.variants.all() if variant.active]
        if active_variants:
            if all(hasattr(variant, "_held_quantity") for variant in active_variants):
                return sum(
                    variant.available_quantity
                    for variant in active_variants
                    if variant.purchasable
                )
            held_quantities = {
                row["variant_id"]: row["total"]
                for row in self.order_items.filter(
                    variant_id__in=[variant.pk for variant in active_variants],
                    reservation__status__in=HELD_RESERVATION_STATUSES,
                )
                .values("variant_id")
                .annotate(total=Sum("reservation__quantity"))
            }
            return sum(
                max(variant.quantity - held_quantities.get(variant.pk, 0), 0)
                for variant in active_variants
                if variant.purchasable
            )
        held_quantity = getattr(self, "_held_quantity", None)
        if held_quantity is None:
            held_quantity = (
                self.order_items.filter(
                    variant__isnull=True,
                    reservation__status__in=HELD_RESERVATION_STATUSES,
                ).aggregate(total=Sum("reservation__quantity"))["total"]
                or 0
            )
        return max(self.quantity - held_quantity, 0)

    @property
    def display_price(self):
        active_variants = [variant for variant in self.variants.all() if variant.active]
        if not active_variants:
            return self.price
        prices = [
            variant.price
            for variant in active_variants
            if variant.purchasable
            and variant.available_quantity > 0
        ]
        return min(prices) if prices else self.price

    @property
    def direct_price(self):
        return calculate_direct_price(self.price)

    @property
    def display_direct_price(self):
        return calculate_direct_price(self.display_price)

    @property
    def has_active_variants(self):
        return any(variant.active for variant in self.variants.all())

    @property
    def is_purchasable(self):
        return (
            self.active
            and not self.checkout_excluded
            and self.currency == "USD"
            and self.available_quantity > 0
        )


class ProductVariant(models.Model):
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="variants"
    )
    source_key = models.CharField(max_length=255)
    sku = models.CharField(max_length=100, blank=True)
    title = models.CharField(max_length=255)
    specifics = models.JSONField(default=dict)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    quantity = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    purchasable = models.BooleanField(default=True)

    objects = ProductVariantQuerySet.as_manager()

    class Meta:
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(
                fields=("product", "source_key"), name="unique_product_variant"
            )
        ]

    def __str__(self):
        return self.title

    @property
    def available_quantity(self):
        held_quantity = getattr(self, "_held_quantity", None)
        if held_quantity is None:
            held_quantity = (
                self.order_items.filter(
                    reservation__status__in=HELD_RESERVATION_STATUSES
                ).aggregate(total=Sum("reservation__quantity"))["total"]
                or 0
            )
        return max(self.quantity - held_quantity, 0)

    @property
    def direct_price(self):
        return calculate_direct_price(self.price)


class ProductImage(models.Model):
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="images"
    )
    url = models.URLField(max_length=1000)
    position = models.PositiveSmallIntegerField()
    variation_name = models.CharField(max_length=120, blank=True)
    variation_value = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ("position",)
        constraints = [
            models.UniqueConstraint(
                fields=("product", "position"), name="unique_product_image_position"
            )
        ]

    def __str__(self):
        return f"{self.product} image {self.position}"


class SyncRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    status = models.CharField(max_length=10, choices=Status, default=Status.RUNNING)
    indexed_count = models.PositiveIntegerField(default=0)
    imported_count = models.PositiveIntegerField(default=0)
    deactivated_count = models.PositiveIntegerField(default=0)
    seller_username = models.CharField(max_length=100)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-started_at",)

    def __str__(self):
        return f"{self.started_at:%Y-%m-%d %H:%M} {self.status}"


class InventoryOperation(models.Model):
    class Reason(models.TextChoices):
        RESERVE = "reserve", "Checkout reservation"
        RELEASE = "release", "Reservation release"
        SALE = "sale", "Completed sale"
        RECONCILE = "reconcile", "Reconciliation"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    idempotency_key = models.CharField(max_length=100, unique=True)
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="inventory_operations"
    )
    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.PROTECT,
        related_name="inventory_operations",
        null=True,
        blank=True,
    )
    reason = models.CharField(max_length=12, choices=Reason)
    expected_quantity = models.PositiveIntegerField()
    requested_quantity = models.PositiveIntegerField()
    verified_quantity = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status, default=Status.PENDING)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.product.ebay_item_id} -> {self.requested_quantity}"
