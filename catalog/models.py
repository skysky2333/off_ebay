from django.db import models
from django.db.models import Q, Sum


class ProductQuerySet(models.QuerySet):
    def purchasable(self):
        return self.filter(active=True, checkout_excluded=False).filter(
            Q(variants__isnull=True, quantity__gt=0)
            | Q(
                variants__active=True,
                variants__purchasable=True,
                variants__quantity__gt=0,
            )
        ).distinct()


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
        if self.variants.filter(active=True).exists():
            return self.variants.filter(active=True, purchasable=True).aggregate(
                total=Sum("quantity")
            )["total"] or 0
        return self.quantity

    @property
    def is_purchasable(self):
        return (
            self.active
            and not self.checkout_excluded
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

    class Meta:
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(
                fields=("product", "source_key"), name="unique_product_variant"
            )
        ]

    def __str__(self):
        return self.title


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
