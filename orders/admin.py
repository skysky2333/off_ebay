from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.core.exceptions import PermissionDenied
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.html import format_html

from .models import InventoryReservation, Order, OrderEvent, OrderItem, Refund, Shipment
from .paypal import PayPalClient
from .services import (
    SHIPPABLE_ORDER_STATUSES,
    orders_accepting_shipments,
    orders_needing_fulfillment,
    record_manual_shipment,
    refund_order,
    refunds_needing_review,
)


class FulfillmentFilter(admin.SimpleListFilter):
    title = "fulfillment"
    parameter_name = "fulfillment"

    def lookups(self, request, model_admin):
        return (("needed", "Needs fulfillment"),)

    def queryset(self, request, queryset):
        if self.value() == "needed":
            return orders_needing_fulfillment(queryset)
        return queryset


class PaymentReviewFilter(admin.SimpleListFilter):
    title = "payment review"
    parameter_name = "payment_review"

    def lookups(self, request, model_admin):
        return (("needed", "Needs review"),)

    def queryset(self, request, queryset):
        if self.value() == "needed":
            return queryset.filter(
                status__in={
                    Order.Status.PAYMENT_PROCESSING,
                    Order.Status.CAPTURE_PENDING,
                    Order.Status.FUNDING_RETRY,
                }
            )
        return queryset


class RefundReviewFilter(admin.SimpleListFilter):
    title = "refund review"
    parameter_name = "refund_review"

    def lookups(self, request, model_admin):
        return (("needed", "Needs review"),)

    def queryset(self, request, queryset):
        if self.value() == "needed":
            return refunds_needing_review(queryset)
        return queryset


class ShipmentAdminForm(forms.ModelForm):
    class Meta:
        model = Shipment
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "order" in self.fields:
            self.fields["order"].queryset = orders_accepting_shipments().order_by(
                "-created_at"
            )

    def clean(self):
        cleaned_data = super().clean()
        order = cleaned_data.get("order")
        if order is None and self.instance.order_id:
            order = self.instance.order
        if order and order.status not in SHIPPABLE_ORDER_STATUSES:
            raise forms.ValidationError("Only paid orders can be shipped.")
        if (
            order
            and not self.instance.pk
            and order.refunds.filter(status=Refund.Status.PENDING).exists()
        ):
            raise forms.ValidationError(
                "Orders with a pending refund cannot receive a new shipment."
            )
        return cleaned_data


class ReadOnlyAdminMixin:
    def has_change_permission(self, request, obj=None):
        return obj is None and super().has_change_permission(request, obj)


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
        "provider_updated_at",
        "completes_order",
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
    classes = ("collapse",)

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
    list_filter = (
        FulfillmentFilter,
        PaymentReviewFilter,
        "status",
        "currency",
        "created_at",
    )
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
        "status",
        "created_at",
        "updated_at",
        "expires_at",
        "paid_at",
        "cancelled_at",
        "shipped_at",
        "refunded_at",
        "customer_email",
        "customer_name",
        "customer_phone",
        "shipping_line_1",
        "shipping_line_2",
        "shipping_city",
        "shipping_region",
        "shipping_postal_code",
        "shipping_country_code",
        "currency",
        "subtotal",
        "shipping_total",
        "total",
        "refunded_total",
        "paypal_order_id",
        "paypal_capture_id",
        "paypal_refund_id",
        "paypal_status",
        "customer_status_link",
        "shipment_action",
    )
    fieldsets = (
        (
            "Order",
            {
                "fields": (
                    "reference",
                    "status",
                    "created_at",
                    "customer_status_link",
                    "shipment_action",
                )
            },
        ),
        (
            "Customer",
            {
                "fields": (
                    "customer_name",
                    "customer_email",
                    "customer_phone",
                    "shipping_line_1",
                    "shipping_line_2",
                    "shipping_city",
                    "shipping_region",
                    "shipping_postal_code",
                    "shipping_country_code",
                )
            },
        ),
        (
            "Payment",
            {
                "fields": (
                    "subtotal",
                    "shipping_total",
                    "total",
                    "refunded_total",
                    "currency",
                    "paypal_status",
                )
            },
        ),
        (
            "Timeline",
            {
                "fields": (
                    "paid_at",
                    "shipped_at",
                    "refunded_at",
                    "cancelled_at",
                    "expires_at",
                    "updated_at",
                )
            },
        ),
        (
            "Provider references",
            {
                "classes": ("collapse",),
                "fields": (
                    "paypal_order_id",
                    "paypal_capture_id",
                    "paypal_refund_id",
                ),
            },
        ),
    )
    inlines = (OrderItemInline, ShipmentInline, OrderEventInline)
    actions = ("refund_one_order",)

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if request.user.has_perm("orders.add_shipment"):
            return fieldsets
        order_fields = tuple(
            field
            for field in fieldsets[0][1]["fields"]
            if field != "shipment_action"
        )
        return (
            (fieldsets[0][0], {**fieldsets[0][1], "fields": order_fields}),
            *fieldsets[1:],
        )

    @admin.display(description="Customer view")
    def customer_status_link(self, order):
        url = reverse("storefront:order_status", kwargs={"token": order.status_token})
        return format_html('<a href="{}">Open order status</a>', url)

    @admin.display(description="Shipment")
    def shipment_action(self, order):
        if order.status not in SHIPPABLE_ORDER_STATUSES:
            return (
                "Unavailable for this order"
                if order.paid_at
                else "Available after payment"
            )
        if order.refunds.filter(status=Refund.Status.PENDING).exists():
            return "Unavailable while a refund is pending"
        url = reverse("admin:orders_shipment_add")
        return format_html(
            '<a class="button" href="{}?order={}">Add shipment</a>', url, order.pk
        )

    @admin.action(
        description="Refund one captured order through PayPal",
        permissions=("refund",),
    )
    def refund_one_order(self, request, queryset):
        if not self.has_refund_permission(request):
            raise PermissionDenied
        if queryset.count() != 1:
            self.message_user(
                request, "Select exactly one order to refund.", level=messages.ERROR
            )
            return None
        order = queryset.get()
        if not order.paypal_capture_id or not order.paid_at:
            self.message_user(
                request, "Only captured orders can be refunded.", level=messages.ERROR
            )
            return None
        amount = order.total - order.refunded_total
        if order.status == Order.Status.REFUNDED or amount <= 0:
            self.message_user(
                request, "This order has already been fully refunded.", level=messages.ERROR
            )
            return None
        if request.POST.get("confirm_refund") != "yes":
            return TemplateResponse(
                request,
                "admin/orders/order/refund_confirmation.html",
                {
                    **self.admin_site.each_context(request),
                    "title": "Confirm PayPal refund",
                    "opts": self.model._meta,
                    "order": order,
                    "refund_amount": amount,
                    "action_checkbox_name": ACTION_CHECKBOX_NAME,
                },
            )
        with PayPalClient() as client:
            order = refund_order(order.pk, client)
        refund = Refund.objects.get(paypal_refund_id=order.paypal_refund_id)
        if refund.status == Refund.Status.COMPLETED:
            self.message_user(
                request,
                f"Refunded {order.reference} for {amount:.2f} {order.currency}.",
            )
        else:
            self.message_user(
                request,
                f"PayPal refund for {order.reference} is {refund.get_status_display().lower()}.",
                level=(
                    messages.WARNING
                    if refund.status == Refund.Status.PENDING
                    else messages.ERROR
                ),
            )

    def has_refund_permission(self, request):
        return request.user.has_perm("orders.refund_order")

    def has_add_permission(self, request):
        return False

    def changelist_view(self, request, extra_context=None):
        return super().changelist_view(
            request, {"title": "Orders", **(extra_context or {})}
        )

    def has_change_permission(self, request, obj=None):
        return obj is None and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    form = ShipmentAdminForm
    list_display = (
        "order",
        "carrier",
        "tracking_number",
        "status",
        "source",
        "completes_order",
        "updated_at",
    )
    list_filter = ("status", "source", "carrier")
    search_fields = ("order__reference", "tracking_number", "carrier")
    readonly_fields = (
        "source",
        "shipped_at",
        "delivered_at",
        "provider_updated_at",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (
            "Shipment identity",
            {"fields": ("order", "carrier", "tracking_number", "source")},
        ),
        (
            "Fulfillment",
            {"fields": ("status", "completes_order")},
        ),
        (
            "Timeline",
            {
                "classes": ("collapse",),
                "fields": (
                    "shipped_at",
                    "delivered_at",
                    "provider_updated_at",
                    "created_at",
                    "updated_at",
                ),
            },
        ),
    )

    def get_readonly_fields(self, request, obj=None):
        fields = self.readonly_fields
        if obj:
            fields += ("order", "tracking_number")
        return fields

    def save_model(self, request, obj, form, change):
        shipment = record_manual_shipment(
            obj.order_id,
            obj.carrier,
            obj.tracking_number,
            obj.status,
            obj.completes_order,
        )
        obj.pk = shipment.pk
        obj._state.adding = False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InventoryReservation)
class InventoryReservationAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
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
class OrderEventAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("order", "kind", "source", "created_at")
    list_filter = ("source", "kind")
    search_fields = ("order__reference", "event_key")
    readonly_fields = ("order", "kind", "source", "event_key", "data", "created_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Refund)
class RefundAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("order", "paypal_refund_id", "amount", "status", "updated_at")
    list_filter = (RefundReviewFilter, "status")
    search_fields = ("order__reference", "paypal_refund_id")
    readonly_fields = (
        "order",
        "paypal_refund_id",
        "amount",
        "status",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
