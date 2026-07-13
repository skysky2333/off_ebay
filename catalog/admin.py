from django.contrib import admin

from .models import (
    InventoryOperation,
    Product,
    ProductImage,
    ProductVariant,
    SyncRun,
)


class ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class ProductVariantInline(ReadOnlyInline):
    model = ProductVariant
    fields = ("sku", "title", "price", "quantity", "active", "purchasable")
    readonly_fields = fields


class ProductImageInline(ReadOnlyInline):
    model = ProductImage
    fields = ("position", "url", "variation_name", "variation_value")
    readonly_fields = fields


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "price",
        "quantity",
        "active",
        "checkout_excluded",
        "last_synced_at",
    )
    list_filter = ("active", "checkout_excluded", "condition", "category_name")
    search_fields = ("title", "ebay_item_id")
    list_editable = ("checkout_excluded",)
    inlines = (ProductVariantInline, ProductImageInline)
    readonly_fields = tuple(
        field.name
        for field in Product._meta.fields
        if field.name != "checkout_excluded"
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    list_display = (
        "started_at",
        "status",
        "indexed_count",
        "imported_count",
        "deactivated_count",
    )
    list_filter = ("status",)
    readonly_fields = tuple(field.name for field in SyncRun._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InventoryOperation)
class InventoryOperationAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "product",
        "variant",
        "reason",
        "requested_quantity",
        "verified_quantity",
        "status",
    )
    list_filter = ("status", "reason")
    search_fields = ("idempotency_key", "product__ebay_item_id", "variant__sku")
    readonly_fields = tuple(field.name for field in InventoryOperation._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
