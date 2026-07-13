from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from catalog.account_state import account_closure_notification_id
from catalog.models import EbayAccountClosure, SyncRun
from orders.models import InventoryReservation, Order, Refund


class Command(BaseCommand):
    def handle(self, *args, **options):
        if account_closure_notification_id() or EbayAccountClosure.objects.exists():
            self.stdout.write("Worker is healthy; eBay seller account is closed.")
            return
        cutoff = timezone.now() - timedelta(seconds=settings.EBAY_SYNC_SECONDS * 2)
        failures = []
        if not SyncRun.objects.filter(
            status=SyncRun.Status.SUCCEEDED, completed_at__gte=cutoff
        ).exists():
            failures.append("No recent successful eBay synchronization.")
        pending_orders = list(
            Order.objects.filter(
                status__in={
                    Order.Status.PAYMENT_PROCESSING,
                    Order.Status.CAPTURE_PENDING,
                },
                updated_at__lt=cutoff,
            )
            .order_by("pk")
            .values_list("reference", flat=True)
        )
        if pending_orders:
            failures.append(
                f"Payment reconciliation is stale for: {', '.join(pending_orders)}."
            )
        expired_funding = list(
            Order.objects.filter(
                status=Order.Status.FUNDING_RETRY,
                expires_at__lt=timezone.now(),
            )
            .order_by("pk")
            .values_list("reference", flat=True)
        )
        if expired_funding:
            failures.append(
                f"Payment-method retries are overdue for: {', '.join(expired_funding)}."
            )
        pending_refunds = list(
            Refund.objects.filter(
                status=Refund.Status.PENDING,
                updated_at__lt=cutoff,
            )
            .order_by("order_id")
            .values_list("order__reference", flat=True)
        )
        if pending_refunds:
            failures.append(
                f"PayPal refunds are stale for: {', '.join(pending_refunds)}."
            )
        inventory_orders = list(
            InventoryReservation.objects.filter(
                status__in={
                    InventoryReservation.Status.COMMITTING,
                    InventoryReservation.Status.RELEASING,
                },
                updated_at__lt=cutoff,
            )
            .order_by("order_item__order_id")
            .values_list("order_item__order__reference", flat=True)
            .distinct()
        )
        if inventory_orders:
            failures.append(
                f"Inventory reconciliation is stale for: {', '.join(inventory_orders)}."
            )
        if failures:
            raise CommandError(" ".join(failures))
        self.stdout.write("Worker is healthy.")
