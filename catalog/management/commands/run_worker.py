import time

from django.conf import settings
from django.core.management.base import BaseCommand

from catalog.ebay import EbayTradingClient
from catalog.inventory import EbayInventoryGateway
from catalog.services import sync_catalog
from orders.models import InventoryReservation, Order
from orders.paypal import PayPalClient
from orders.services import cancel_order, capture_paypal_order, expire_due_orders


class Command(BaseCommand):
    def handle(self, *args, **options):
        while True:
            cancelled_order_ids = list(
                Order.objects.filter(
                    status=Order.Status.CANCELLED,
                    items__reservation__status__in={
                        InventoryReservation.Status.RESERVED,
                        InventoryReservation.Status.COMMITTING,
                        InventoryReservation.Status.COMMITTED,
                        InventoryReservation.Status.RELEASING,
                    },
                )
                .order_by("pk")
                .values_list("pk", flat=True)
                .distinct()
            )
            for order_id in cancelled_order_ids:
                cancel_order(
                    order_id,
                    EbayInventoryGateway(),
                    capture_definitely_absent=True,
                )
            pending_order_ids = list(
                Order.objects.filter(
                    status__in={
                        Order.Status.PAYMENT_PROCESSING,
                        Order.Status.CAPTURE_PENDING,
                    }
                )
                .order_by("pk")
                .values_list("pk", flat=True)
            )
            if pending_order_ids:
                with PayPalClient() as paypal:
                    for order_id in pending_order_ids:
                        capture_paypal_order(
                            order_id, paypal, EbayInventoryGateway()
                        )
            with EbayTradingClient() as client:
                run = sync_catalog(client)
            expired = expire_due_orders(EbayInventoryGateway())
            self.stdout.write(
                f"sync={run.pk} imported={run.imported_count} expired={expired}"
            )
            time.sleep(settings.EBAY_SYNC_SECONDS)
