from django.contrib import admin

from .models import InventoryReservation, Order, OrderEvent, OrderItem, Refund, Shipment
from .paypal import PayPalClient
from .services import record_manual_shipment, refund_order


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    can_delete = False
    fields = (
        "title",
        "variation_title",
        "variation_sku",
        "quantity",
        "unit_price",
        "ebay_item_id",
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class ShipmentInline(admin.TabularInline):
    model = Shipment
    extra = 0
    can_delete = False
    show_change_link = True
    fields = (
        "carrier",
        "tracking_number",
        "status",
        "source",
        "shipped_at",
        "delivered_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class OrderEventInline(admin.TabularInline):
    model = OrderEvent
    extra = 0
    can_delete = False
    fields = ("created_at", "kind", "source", "data")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "status",
        "customer_name",
        "total",
        "currency",
        "created_at",
    )
    list_filter = ("status", "currency", "created_at")
    search_fields = (
        "reference",
        "customer_name",
        "customer_email",
        "paypal_order_id",
        "paypal_capture_id",
        "shipments__tracking_number",
    )
    readonly_fields = (
        "reference",
        "status_token",
        "checkout_key",
        "checkout_fingerprint",
        "subtotal",
        "shipping_total",
        "total",
        "refunded_total",
        "paypal_order_id",
        "paypal_capture_id",
        "paypal_refund_id",
        "paypal_status",
        "expires_at",
        "paid_at",
        "cancelled_at",
        "shipped_at",
        "refunded_at",
        "created_at",
        "updated_at",
    )
    inlines = (OrderItemInline, ShipmentInline, OrderEventInline)
    actions = ("refund_orders",)

    @admin.action(description="Refund selected paid orders through PayPal")
    def refund_orders(self, request, queryset):
        with PayPalClient() as client:
            for order in queryset.order_by("pk"):
                refund_order(order.pk, client)
        self.message_user(request, f"Refunded {queryset.count()} order(s).")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = (
        "order",
        "carrier",
        "tracking_number",
        "status",
        "source",
        "updated_at",
    )
    list_filter = ("status", "source", "carrier")
    search_fields = ("order__reference", "tracking_number", "carrier")
    readonly_fields = (
        "source",
        "shipped_at",
        "delivered_at",
        "created_at",
        "updated_at",
    )

    def get_readonly_fields(self, request, obj=None):
        fields = self.readonly_fields
        if obj:
            fields += ("order", "carrier", "tracking_number")
        return fields

    def save_model(self, request, obj, form, change):
        shipment = record_manual_shipment(
            obj.order_id, obj.carrier, obj.tracking_number, obj.status
        )
        obj.pk = shipment.pk
        obj._state.adding = False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InventoryReservation)
class InventoryReservationAdmin(admin.ModelAdmin):
    list_display = ("order_item", "quantity", "status", "expires_at")
    list_filter = ("status",)
    readonly_fields = (
        "order_item",
        "quantity",
        "status",
        "expires_at",
        "committed_at",
        "released_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OrderEvent)
class OrderEventAdmin(admin.ModelAdmin):
    list_display = ("order", "kind", "source", "created_at")
    list_filter = ("source", "kind")
    search_fields = ("order__reference", "event_key")
    readonly_fields = ("order", "kind", "source", "event_key", "data", "created_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    list_display = ("order", "paypal_refund_id", "amount", "created_at")
    search_fields = ("order__reference", "paypal_refund_id")
    readonly_fields = ("order", "paypal_refund_id", "amount", "created_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
