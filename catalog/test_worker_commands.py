import uuid
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from orders.inventory import InventoryUnavailable
from orders.models import (
    InventoryReservation,
    Order,
    OrderEvent,
    OrderItem,
    Refund,
    Shipment,
)
from orders.services import PaymentDataError, _begin_payment_processing, cancel_order

from .management.commands.run_worker import Command
from .models import EbayAccountClosure, Product, SyncRun


class ClientContext:
    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        return False


@override_settings(EBAY_SYNC_SECONDS=60)
class WorkerCommandTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(
            ebay_item_id="123456789",
            slug="worker-item",
            title="Worker item",
            price=Decimal("10.00"),
            currency="USD",
            listing_url="https://www.ebay.com/itm/123456789",
            listing_type="FixedPriceItem",
            quantity=3,
            last_synced_at=timezone.now(),
        )

    def order(self, status, reservation_status=InventoryReservation.Status.RESERVED):
        order = Order.objects.create(
            checkout_key=uuid.uuid4(),
            checkout_fingerprint="a" * 64,
            status=status,
            customer_email="buyer@example.com",
            customer_name="Buyer",
            shipping_line_1="1 Main Street",
            shipping_city="Baltimore",
            shipping_region="MD",
            shipping_postal_code="21201",
            shipping_country_code="US",
            subtotal=Decimal("10.00"),
            shipping_total=Decimal("0.00"),
            total=Decimal("10.00"),
            expires_at=timezone.now() + timedelta(minutes=30),
            paypal_order_id=f"PAYPAL-{uuid.uuid4()}",
        )
        item = OrderItem.objects.create(
            order=order,
            product=self.product,
            ebay_item_id=self.product.ebay_item_id,
            title=self.product.title,
            quantity=1,
            unit_price=self.product.price,
        )
        InventoryReservation.objects.create(
            order_item=item,
            quantity=1,
            status=reservation_status,
            expires_at=order.expires_at,
        )
        return order

    def command(self):
        command = Command()
        command.stdout = StringIO()
        command.stderr = StringIO()
        return command

    @patch("catalog.management.commands.run_worker.expire_due_orders", return_value=2)
    @patch("catalog.management.commands.run_worker.sync_catalog")
    @patch(
        "catalog.management.commands.run_worker.EbayTradingClient",
        return_value=ClientContext(),
    )
    @patch("catalog.management.commands.run_worker.PayPalClient", return_value=ClientContext())
    @patch("catalog.management.commands.run_worker.capture_paypal_order")
    def test_payment_failure_records_event_and_cycle_continues(
        self, capture, paypal_client, ebay_client, sync, expire
    ):
        first = self.order(Order.Status.PAYMENT_PROCESSING)
        second = self.order(Order.Status.CAPTURE_PENDING)
        capture.side_effect = [PaymentDataError("amount mismatch"), None]
        sync.return_value = SimpleNamespace(pk=9, imported_count=3)
        command = self.command()

        command._run_cycle()

        self.assertEqual(
            [call.args[0] for call in capture.call_args_list], [first.pk, second.pk]
        )
        sync.assert_called_once()
        expire.assert_called_once()
        event = OrderEvent.objects.get(order=first, kind="worker.reconciliation_failed")
        self.assertEqual(event.data["operation"], "payment_capture")
        self.assertEqual(event.data["error_type"], "PaymentDataError")
        self.assertIn(first.reference, command.stderr.getvalue())

    @patch("catalog.management.commands.run_worker.expire_due_orders", return_value=0)
    @patch("catalog.management.commands.run_worker.sync_catalog")
    @patch(
        "catalog.management.commands.run_worker.EbayTradingClient",
        return_value=ClientContext(),
    )
    @patch("catalog.management.commands.run_worker.PayPalClient", return_value=ClientContext())
    @patch("catalog.management.commands.run_worker.reconcile_due_funding_retry")
    def test_due_funding_retry_is_reconciled_once(
        self, reconcile, paypal_client, ebay_client, sync, expire
    ):
        order = self.order(Order.Status.FUNDING_RETRY)
        Order.objects.filter(pk=order.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        reconcile.return_value = SimpleNamespace(status=Order.Status.EXPIRED)
        sync.return_value = SimpleNamespace(pk=11, imported_count=3)
        command = self.command()

        command._run_cycle()

        self.assertEqual(reconcile.call_args.args[0], order.pk)
        self.assertIn("expired=1", command.stdout.getvalue())

    @patch("catalog.management.commands.run_worker.expire_due_orders", return_value=0)
    @patch("catalog.management.commands.run_worker.sync_catalog")
    @patch(
        "catalog.management.commands.run_worker.EbayTradingClient",
        return_value=ClientContext(),
    )
    @patch("catalog.management.commands.run_worker.PayPalClient", return_value=ClientContext())
    @patch("catalog.management.commands.run_worker.reconcile_pending_refund")
    def test_pending_refund_is_reconciled_once(
        self, reconcile, paypal_client, ebay_client, sync, expire
    ):
        order = self.order(Order.Status.PAID)
        refund = Refund.objects.create(
            order=order,
            paypal_refund_id="REFUND-PENDING",
            amount=Decimal("10.00"),
            status=Refund.Status.PENDING,
        )
        sync.return_value = SimpleNamespace(pk=12, imported_count=3)

        self.command()._run_cycle()

        self.assertEqual(reconcile.call_args.args[0], refund.pk)

    @patch("catalog.management.commands.run_worker.expire_due_orders", return_value=0)
    @patch("catalog.management.commands.run_worker.sync_catalog")
    @patch(
        "catalog.management.commands.run_worker.EbayTradingClient",
        return_value=ClientContext(),
    )
    @patch("catalog.management.commands.run_worker.PayPalClient", return_value=ClientContext())
    @patch("catalog.management.commands.run_worker.reconcile_paypal_tracking")
    def test_tracking_reconciliation_stops_only_after_paypal_shipment_is_terminal(
        self, reconcile, paypal_client, ebay_client, sync, expire
    ):
        untracked = self.order(Order.Status.PAID)
        active = self.order(Order.Status.SHIPPED)
        completed = self.order(Order.Status.SHIPPED)
        for order in (untracked, active, completed):
            Order.objects.filter(pk=order.pk).update(
                paid_at=timezone.now(),
                paypal_capture_id=f"CAPTURE-{order.pk}",
            )
        Shipment.objects.create(
            order=active,
            carrier="USPS",
            tracking_number="ACTIVE",
            status=Shipment.Status.SHIPPED,
            source=Shipment.Source.PAYPAL,
            completes_order=True,
        )
        Shipment.objects.create(
            order=completed,
            carrier="USPS",
            tracking_number="DELIVERED",
            status=Shipment.Status.DELIVERED,
            source=Shipment.Source.PAYPAL,
            completes_order=True,
        )
        sync.return_value = SimpleNamespace(pk=14, imported_count=3)

        self.command()._run_cycle()

        self.assertEqual(
            [call.args[0] for call in reconcile.call_args_list],
            [untracked.pk, active.pk],
        )

    @patch("catalog.management.commands.run_worker.expire_due_orders", return_value=0)
    @patch("catalog.management.commands.run_worker.sync_catalog")
    @patch(
        "catalog.management.commands.run_worker.EbayTradingClient",
        return_value=ClientContext(),
    )
    @patch("catalog.management.commands.run_worker.cancel_order")
    def test_release_failure_records_event_and_later_order_continues(
        self, cancel, ebay_client, sync, expire
    ):
        first = self.order(Order.Status.CANCELLED)
        second = self.order(Order.Status.CANCELLED)
        cancel.side_effect = [InventoryUnavailable("listing ended"), None]
        sync.return_value = SimpleNamespace(pk=10, imported_count=3)
        command = self.command()

        command._run_cycle()

        self.assertEqual(
            [call.args[0] for call in cancel.call_args_list], [first.pk, second.pk]
        )
        sync.assert_called_once()
        expire.assert_called_once()
        event = OrderEvent.objects.get(order=first, kind="worker.reconciliation_failed")
        self.assertEqual(event.data["operation"], "inventory_release")
        self.assertIn("listing ended", command.stderr.getvalue())

    @patch("catalog.management.commands.run_worker.capture_paypal_order")
    @patch("catalog.management.commands.run_worker.PayPalClient", return_value=ClientContext())
    def test_unexpected_reconciliation_error_fails_fast(self, paypal_client, capture):
        order = self.order(Order.Status.PAYMENT_PROCESSING)
        capture.side_effect = RuntimeError("programming error")

        with self.assertRaisesMessage(RuntimeError, "programming error"):
            self.command()._run_cycle()

        self.assertFalse(OrderEvent.objects.filter(order=order).exists())

    @patch("catalog.management.commands.run_worker.expire_due_orders")
    @patch("catalog.management.commands.run_worker.sync_catalog")
    @patch(
        "catalog.management.commands.run_worker.EbayTradingClient",
        return_value=ClientContext(),
    )
    def test_sync_failure_does_not_block_expiration(
        self, ebay_client, sync, expire
    ):
        calls = []

        def expire_orders(inventory, now):
            calls.append("expire")
            return 2

        def fail_sync(client):
            calls.append("sync")
            raise RuntimeError("sync failed")

        expire.side_effect = expire_orders
        sync.side_effect = fail_sync

        with self.assertRaisesMessage(RuntimeError, "sync failed"):
            self.command()._run_cycle()

        self.assertEqual(calls, ["expire", "sync"])

    @patch("catalog.management.commands.run_worker.expire_due_orders", return_value=0)
    @patch("catalog.management.commands.run_worker.sync_catalog")
    @patch(
        "catalog.management.commands.run_worker.EbayTradingClient",
        return_value=ClientContext(),
    )
    def test_cycle_purges_only_expired_sessions(self, ebay_client, sync, expire):
        now = timezone.now()
        Session.objects.create(
            session_key="expired-session",
            session_data="",
            expire_date=now - timedelta(seconds=1),
        )
        Session.objects.create(
            session_key="live-session",
            session_data="",
            expire_date=now + timedelta(days=1),
        )
        sync.return_value = SimpleNamespace(pk=13, imported_count=3)

        self.command()._run_cycle()

        self.assertFalse(
            Session.objects.filter(session_key="expired-session").exists()
        )
        self.assertTrue(Session.objects.filter(session_key="live-session").exists())

    def recent_sync(self):
        return SyncRun.objects.create(
            status=SyncRun.Status.SUCCEEDED,
            seller_username="fm2k244",
            completed_at=timezone.now(),
        )

    def age(self, instance):
        type(instance).objects.filter(pk=instance.pk).update(
            updated_at=timezone.now() - timedelta(minutes=3)
        )

    def test_worker_health_rejects_aged_payment_reconciliation(self):
        self.recent_sync()
        order = self.order(Order.Status.PAYMENT_PROCESSING)
        self.age(order)

        with self.assertRaisesMessage(CommandError, order.reference):
            call_command("worker_health", stdout=StringIO())

    def test_worker_health_rejects_overdue_funding_retry(self):
        self.recent_sync()
        order = self.order(Order.Status.FUNDING_RETRY)
        Order.objects.filter(pk=order.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        with self.assertRaisesMessage(CommandError, order.reference):
            call_command("worker_health", stdout=StringIO())

    def test_worker_health_rejects_stale_pending_refund(self):
        self.recent_sync()
        order = self.order(Order.Status.PAID)
        refund = Refund.objects.create(
            order=order,
            paypal_refund_id="REFUND-PENDING",
            amount=Decimal("10.00"),
            status=Refund.Status.PENDING,
        )
        self.age(refund)

        with self.assertRaisesMessage(CommandError, order.reference):
            call_command("worker_health", stdout=StringIO())

    def test_worker_health_rejects_aged_inventory_release(self):
        self.recent_sync()
        order = self.order(
            Order.Status.CANCELLED, InventoryReservation.Status.RELEASING
        )
        self.age(order.items.get().reservation)

        with self.assertRaisesMessage(CommandError, order.reference):
            call_command("worker_health", stdout=StringIO())

    def test_worker_health_rejects_aged_inventory_commit(self):
        self.recent_sync()
        order = self.order(
            Order.Status.CANCELLED, InventoryReservation.Status.COMMITTING
        )
        self.age(order.items.get().reservation)

        with self.assertRaisesMessage(CommandError, order.reference):
            call_command("worker_health", stdout=StringIO())

    def test_worker_health_accepts_fresh_reconciliation_states(self):
        self.recent_sync()
        self.order(Order.Status.CAPTURE_PENDING)
        self.order(Order.Status.CANCELLED, InventoryReservation.Status.COMMITTING)
        self.order(Order.Status.CANCELLED, InventoryReservation.Status.RELEASING)
        output = StringIO()

        call_command("worker_health", stdout=output)

        self.assertIn("Worker is healthy", output.getvalue())

    @patch("catalog.management.commands.run_worker.time.sleep", side_effect=KeyboardInterrupt)
    @patch("catalog.management.commands.run_worker.Command._run_cycle")
    def test_closed_account_keeps_worker_idle(self, run_cycle, sleep):
        EbayAccountClosure.objects.create(notification_id="notification-closed")
        command = self.command()

        with self.assertRaises(KeyboardInterrupt):
            command.handle()

        run_cycle.assert_not_called()
        self.assertIn("worker is idle", command.stdout.getvalue())

    def test_worker_health_accepts_closed_account(self):
        EbayAccountClosure.objects.create(notification_id="notification-closed")
        output = StringIO()

        call_command("worker_health", stdout=output)

        self.assertIn("seller account is closed", output.getvalue())

    def test_retries_preserve_reconciliation_state_age(self):
        payment = self.order(Order.Status.PAYMENT_PROCESSING)
        release = self.order(
            Order.Status.CANCELLED, InventoryReservation.Status.RELEASING
        ).items.get().reservation
        self.age(payment)
        self.age(release)
        payment.refresh_from_db()
        release.refresh_from_db()
        payment_age = payment.updated_at
        release_age = release.updated_at

        _begin_payment_processing(payment.pk, "APPROVED")

        class FailingInventory:
            def release(self, reservation):
                raise InventoryUnavailable("listing ended")

        with self.assertRaises(InventoryUnavailable):
            cancel_order(
                release.order_item.order_id,
                FailingInventory(),
                capture_definitely_absent=True,
            )

        payment.refresh_from_db()
        release.refresh_from_db()
        self.assertEqual(payment.updated_at, payment_age)
        self.assertEqual(release.updated_at, release_age)
