import time

import httpx
from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand
from django.utils import timezone

from catalog.account_state import account_closure_notification_id
from catalog.ebay import EbayError, EbayTradingClient
from catalog.inventory import EbayInventoryGateway
from catalog.models import EbayAccountClosure
from catalog.services import sync_catalog
from orders.inventory import InventoryUnavailable
from orders.models import InventoryReservation, Order, OrderEvent, Refund
from orders.paypal import PayPalClient, PayPalInstrumentDeclined
from orders.services import (
    OrderStateError,
    PaymentDataError,
    cancel_order,
    capture_paypal_order,
    expire_due_orders,
    orders_needing_paypal_tracking,
    reconcile_due_funding_retry,
    reconcile_pending_refund,
    reconcile_paypal_tracking,
)


EXPECTED_RECONCILIATION_ERRORS = (
    EbayError,
    InventoryUnavailable,
    OrderStateError,
    PaymentDataError,
    PayPalInstrumentDeclined,
    httpx.HTTPError,
)


class Command(BaseCommand):
    def _record_failure(self, order_id, operation, error):
        order = Order.objects.get(pk=order_id)
        OrderEvent.objects.create(
            order=order,
            kind="worker.reconciliation_failed",
            source=OrderEvent.Source.SYSTEM,
            data={
                "operation": operation,
                "status": order.status,
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )
        self.stderr.write(
            f"order={order.reference} operation={operation} "
            f"error={type(error).__name__}: {error}"
        )

    def _run_cycle(self):
        now = timezone.now()
        Session.objects.filter(expire_date__lt=now).delete()
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
            try:
                cancel_order(
                    order_id,
                    EbayInventoryGateway(),
                    capture_definitely_absent=True,
                )
            except EXPECTED_RECONCILIATION_ERRORS as error:
                self._record_failure(order_id, "inventory_release", error)
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
        funding_retry_ids = list(
            Order.objects.filter(
                status=Order.Status.FUNDING_RETRY,
                expires_at__lte=now,
            )
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        pending_refunds = list(
            Refund.objects.filter(status=Refund.Status.PENDING)
            .order_by("pk")
            .values_list("pk", "order_id")
        )
        tracking_order_ids = list(
            orders_needing_paypal_tracking()
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        funding_expired = 0
        if pending_order_ids or funding_retry_ids or pending_refunds or tracking_order_ids:
            with PayPalClient() as paypal:
                for order_id in pending_order_ids:
                    try:
                        capture_paypal_order(
                            order_id, paypal, EbayInventoryGateway()
                        )
                    except EXPECTED_RECONCILIATION_ERRORS as error:
                        self._record_failure(order_id, "payment_capture", error)
                for order_id in funding_retry_ids:
                    try:
                        order = reconcile_due_funding_retry(
                            order_id,
                            paypal,
                            EbayInventoryGateway(),
                            now,
                        )
                        funding_expired += order.status == Order.Status.EXPIRED
                    except EXPECTED_RECONCILIATION_ERRORS as error:
                        self._record_failure(order_id, "funding_retry", error)
                for refund_id, order_id in pending_refunds:
                    try:
                        reconcile_pending_refund(refund_id, paypal)
                    except EXPECTED_RECONCILIATION_ERRORS as error:
                        self._record_failure(order_id, "refund", error)
                for order_id in tracking_order_ids:
                    try:
                        reconcile_paypal_tracking(order_id, paypal)
                    except EXPECTED_RECONCILIATION_ERRORS as error:
                        self._record_failure(order_id, "tracking", error)
        expired = expire_due_orders(EbayInventoryGateway(), now) + funding_expired
        with EbayTradingClient() as client:
            run = sync_catalog(client)
        self.stdout.write(
            f"sync={run.pk} imported={run.imported_count} expired={expired}"
        )

    def handle(self, *args, **options):
        while True:
            if account_closure_notification_id() or EbayAccountClosure.objects.exists():
                self.stdout.write("eBay seller account is closed; worker is idle.")
            else:
                self._run_cycle()
            time.sleep(settings.EBAY_SYNC_SECONDS)
