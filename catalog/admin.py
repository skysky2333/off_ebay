from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseNotAllowed
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.html import format_html

from .ebay import EbayTradingClient
from .models import (
    InventoryOperation,
    Product,
    ProductImage,
    ProductVariant,
    SyncRun,
)
from .services import sync_catalog


class ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class ReadOnlyAdminMixin:
    def has_change_permission(self, request, obj=None):
        return obj is None and super().has_change_permission(request, obj)


class ProductVariantInline(ReadOnlyInline):
    model = ProductVariant
    classes = ("collapse",)
    fields = ("sku", "title", "price", "quantity", "active", "purchasable")
    readonly_fields = fields


class ProductImageInline(ReadOnlyInline):
    model = ProductImage
    classes = ("collapse",)
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
    readonly_fields = (
        "customer_page",
        "ebay_listing",
        *(
            field.name
            for field in Product._meta.fields
            if field.name != "checkout_excluded"
        ),
    )
    fieldsets = (
        (
            "Store availability",
            {
                "fields": (
                    "checkout_excluded",
                    "customer_page",
                    "ebay_listing",
                    "active",
                    "quantity",
                    "price",
                    "currency",
                    "last_synced_at",
                )
            },
        ),
        (
            "Listing",
            {"fields": ("title", "condition", "category_name")},
        ),
        (
            "Customer content",
            {"classes": ("collapse",), "fields": ("description",)},
        ),
        (
            "eBay details",
            {
                "classes": ("collapse",),
                "fields": (
                    "ebay_item_id",
                    "slug",
                    "listing_type",
                    "category_id",
                    "item_specifics",
                    "shipping",
                    "volume_discounts",
                    "ebay_started_at",
                    "ebay_ends_at",
                    "created_at",
                    "updated_at",
                ),
            },
        ),
    )

    def get_form(self, request, obj=None, change=False, **kwargs):
        form = super().get_form(request, obj, change, **kwargs)
        if "checkout_excluded" in form.base_fields:
            form.base_fields["checkout_excluded"].help_text = (
                "Immediately remove this listing from store checkout."
            )
        return form

    def get_inline_instances(self, request, obj=None):
        instances = super().get_inline_instances(request, obj)
        if obj and not obj.variants.exists():
            return [
                inline
                for inline in instances
                if not isinstance(inline, ProductVariantInline)
            ]
        return instances

    @admin.display(description="Customer page")
    def customer_page(self, product):
        if not product.is_purchasable:
            return "Not available in store"
        url = reverse("storefront:product_detail", kwargs={"slug": product.slug})
        return format_html('<a href="{}">Open customer page</a>', url)

    @admin.display(description="eBay listing")
    def ebay_listing(self, product):
        return format_html(
            '<a href="{}" target="_blank" rel="noopener">Open on eBay</a>',
            product.listing_url,
        )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SyncRun)
class SyncRunAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        "started_at",
        "status",
        "indexed_count",
        "imported_count",
        "deactivated_count",
    )
    list_filter = ("status",)
    readonly_fields = tuple(field.name for field in SyncRun._meta.fields)

    def get_urls(self):
        return [
            path(
                "sync/",
                self.admin_site.admin_view(self.sync_catalog_view),
                name="catalog_syncrun_sync",
            )
        ] + super().get_urls()

    def sync_catalog_view(self, request):
        if not self.has_change_permission(request):
            raise PermissionDenied
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        with EbayTradingClient() as client:
            run = sync_catalog(client)
        self.message_user(
            request,
            f"Catalog sync completed: {run.imported_count} imported, "
            f"{run.deactivated_count} deactivated.",
        )
        return redirect("admin:index")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InventoryOperation)
class InventoryOperationAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
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
