from datetime import timedelta

from django import template
from django.conf import settings
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from catalog.models import InventoryOperation, SyncRun
from orders.models import Order, PayPalCase, Refund
from orders.services import (
    orders_needing_fulfillment,
    paypal_cases_needing_review,
    refunds_needing_review,
)

from ..configuration import checkout_enabled
from ..models import StoreSettings

register = template.Library()


def _can_view(user, model):
    label = model._meta.app_label
    name = model._meta.model_name
    return user.has_perm(f"{label}.view_{name}") or user.has_perm(
        f"{label}.change_{name}"
    )


@register.inclusion_tag("admin/operations_dashboard.html", takes_context=True)
def operations_dashboard(context):
    user = context["request"].user
    can_view_orders = _can_view(user, Order)
    can_view_inventory = _can_view(user, InventoryOperation)
    can_view_sync = _can_view(user, SyncRun)
    can_view_refunds = _can_view(user, Refund)
    can_view_paypal_cases = _can_view(user, PayPalCase)
    store = StoreSettings.objects.filter(pk=1).first()
    can_view_store = (
        _can_view(user, StoreSettings)
        if store
        else user.has_perm("storefront.add_storesettings")
    )
    ebay_configured = all(
        (
            settings.EBAY_CLIENT_ID,
            settings.EBAY_CLIENT_SECRET,
            settings.EBAY_REFRESH_TOKEN,
            settings.EBAY_COMPATIBILITY_LEVEL,
        )
    )
    last_sync = SyncRun.objects.first() if can_view_sync else None
    sync_cutoff = timezone.now() - timedelta(seconds=settings.EBAY_SYNC_SECONDS * 2)
    if not ebay_configured:
        sync_state, sync_label = "setup-required", "Setup required"
    elif last_sync is None:
        sync_state, sync_label = "not-run", "Not run"
    elif last_sync.status == SyncRun.Status.RUNNING and last_sync.started_at < sync_cutoff:
        sync_state, sync_label = "stalled", "Stalled"
    elif last_sync.status == SyncRun.Status.SUCCEEDED and (
        last_sync.completed_at is None or last_sync.completed_at < sync_cutoff
    ):
        sync_state, sync_label = "stale", "Stale"
    else:
        sync_state, sync_label = last_sync.status, last_sync.get_status_display()
    refund_counts = (
        refunds_needing_review().aggregate(
            review=Count(
                "pk",
                filter=Q(
                    status__in={
                        Refund.Status.PENDING,
                        Refund.Status.FAILED,
                        Refund.Status.CANCELLED,
                    }
                ),
            ),
            failed=Count(
                "pk",
                filter=Q(
                    status__in={Refund.Status.FAILED, Refund.Status.CANCELLED}
                ),
            ),
        )
        if can_view_refunds
        else {"review": None, "failed": None}
    )
    paypal_case_counts = (
        paypal_cases_needing_review().aggregate(
            review=Count("pk"),
            urgent=Count(
                "pk",
                filter=Q(
                    Q(kind=PayPalCase.Kind.REVERSAL)
                    | Q(status=PayPalCase.Status.WAITING_FOR_SELLER_RESPONSE)
                ),
            ),
        )
        if can_view_paypal_cases
        else {"review": None, "urgent": None}
    )
    sync_in_progress = bool(
        last_sync
        and last_sync.status == SyncRun.Status.RUNNING
        and sync_state != "stalled"
    )
    can_request_sync = user.has_perm("catalog.change_syncrun") and ebay_configured
    return {
        "can_view_orders": can_view_orders,
        "can_view_inventory": can_view_inventory,
        "can_view_sync": can_view_sync,
        "can_view_refunds": can_view_refunds,
        "can_view_store": can_view_store,
        "can_request_sync": can_request_sync,
        "can_sync": can_request_sync and not sync_in_progress,
        "sync_in_progress": sync_in_progress,
        "ebay_configured": ebay_configured,
        "can_view_paypal_cases": can_view_paypal_cases,
        "checkout_is_enabled": checkout_enabled(store),
        "fulfillment_count": (
            orders_needing_fulfillment().count()
            if can_view_orders
            else None
        ),
        "payment_review_count": (
            Order.objects.filter(
                status__in={
                    Order.Status.PAYMENT_PROCESSING,
                    Order.Status.CAPTURE_PENDING,
                    Order.Status.FUNDING_RETRY,
                }
            ).count()
            if can_view_orders
            else None
        ),
        "failed_inventory_count": (
            InventoryOperation.objects.filter(
                status=InventoryOperation.Status.FAILED
            ).count()
            if can_view_inventory
            else None
        ),
        "refund_review_count": refund_counts["review"],
        "failed_refund_count": refund_counts["failed"],
        "paypal_case_review_count": paypal_case_counts["review"],
        "urgent_paypal_case_count": paypal_case_counts["urgent"],
        "store_settings_url": reverse(
            (
                "admin:storefront_storesettings_change"
                if store
                else "admin:storefront_storesettings_add"
            ),
            args=(store.pk,) if store else (),
        ),
        "last_sync": last_sync,
        "sync_state": sync_state,
        "sync_label": sync_label,
        "recent_orders": Order.objects.all()[:8] if can_view_orders else (),
        "sync_url": reverse("admin:catalog_syncrun_sync"),
    }
