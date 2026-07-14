import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib import admin
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import InventoryReservation, Order, OrderEvent, PayPalCase, Refund, Shipment
from .paypal import PayPalRefundError
from .services import OrderStateError, orders_needing_fulfillment, record_manual_shipment


class PayPalAdminDouble:
    calls = []
    status = "COMPLETED"

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        return False

    def refund_capture(self, capture_id, amount, currency, invoice_id, request_id):
        type(self).calls.append((capture_id, amount, currency, invoice_id, request_id))
        return {
            "id": f"REFUND-{capture_id}",
            "status": type(self).status,
            "amount": {"currency_code": currency, "value": amount},
        }


class PayPalRejectingAdminDouble(PayPalAdminDouble):
    def refund_capture(self, capture_id, amount, currency, invoice_id, request_id):
        raise PayPalRefundError(
            "This capture cannot be refunded (REFUND_NOT_ALLOWED). "
            "PayPal debug ID: DEBUG-REFUND-1."
        )


class OrderAdminTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            "operator", "operator@example.com", "admin-test-password"
        )
        self.client.force_login(self.user)
        PayPalAdminDouble.calls = []
        PayPalAdminDouble.status = "COMPLETED"

    def order(self, status=Order.Status.AWAITING_PAYMENT, captured=False):
        key = uuid.uuid4().hex
        return Order.objects.create(
            checkout_key=uuid.uuid4(),
            checkout_fingerprint=key,
            status=status,
            customer_email=f"{key}@example.com",
            customer_name="Ada Buyer",
            shipping_line_1="1 Main Street",
            shipping_city="Baltimore",
            shipping_region="MD",
            shipping_postal_code="21201",
            shipping_country_code="US",
            currency="USD",
            subtotal=Decimal("10.00"),
            shipping_total=Decimal("0.00"),
            total=Decimal("10.00"),
            paypal_order_id=f"ORDER-{key}",
            paypal_capture_id=f"CAPTURE-{key}" if captured else None,
            paypal_status="COMPLETED" if captured else "CREATED",
            expires_at=timezone.now() + timedelta(minutes=30),
            paid_at=timezone.now() if captured else None,
        )

    def action_data(self, *orders, confirmed=False):
        data = {
            "action": "refund_one_order",
            ACTION_CHECKBOX_NAME: [str(order.pk) for order in orders],
            "select_across": "0",
        }
        if confirmed:
            data["confirm_refund"] = "yes"
        return data

    def test_order_fields_are_read_only_and_paid_order_links_to_prefilled_shipment(self):
        paid = self.order(Order.Status.PAID, captured=True)
        unpaid = self.order()
        refunded = self.order(Order.Status.REFUNDED, captured=True)
        request = RequestFactory().get(reverse("admin:orders_order_change", args=[paid.pk]))
        request.user = self.user
        model_admin = admin.site._registry[Order]

        self.assertEqual(model_admin.get_form(request, paid).base_fields, {})
        paid_response = self.client.get(
            reverse("admin:orders_order_change", args=[paid.pk])
        )
        unpaid_response = self.client.get(
            reverse("admin:orders_order_change", args=[unpaid.pk])
        )
        refunded_response = self.client.get(
            reverse("admin:orders_order_change", args=[refunded.pk])
        )
        shipment_url = f'{reverse("admin:orders_shipment_add")}?order={paid.pk}'

        self.assertContains(paid_response, shipment_url)
        self.assertContains(paid_response, "Open order status")
        self.assertNotContains(paid_response, "Checkout fingerprint")
        self.assertLess(
            paid_response.content.index(b"Open order status"),
            paid_response.content.index(b">Customer</h2>"),
        )
        self.assertNotContains(paid_response, 'name="status"')
        self.assertNotContains(paid_response, 'name="_save"')
        self.assertContains(unpaid_response, "Available after payment")
        self.assertContains(refunded_response, "Unavailable for this order")
        self.assertNotContains(
            unpaid_response,
            f'{reverse("admin:orders_shipment_add")}?order={unpaid.pk}',
        )

    def test_view_only_operator_does_not_see_add_shipment_action(self):
        order = self.order(Order.Status.PAID, captured=True)
        viewer = get_user_model().objects.create_user(
            "order-viewer",
            "viewer@example.com",
            "admin-test-password",
            is_staff=True,
        )
        viewer.user_permissions.add(
            Permission.objects.get(codename="view_order"),
            Permission.objects.get(codename="view_orderitem"),
            Permission.objects.get(codename="view_shipment"),
            Permission.objects.get(codename="view_orderevent"),
        )
        self.client.force_login(viewer)

        response = self.client.get(
            reverse("admin:orders_order_change", args=[order.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Add shipment")

    def test_order_changelist_is_labeled_as_read_only(self):
        response = self.client.get(reverse("admin:orders_order_changelist"))

        self.assertContains(response, "<h1>Orders</h1>", html=True)
        self.assertNotContains(response, "Select order to change")

    def test_shipment_form_filters_orders_and_rejects_forged_unpaid_order_inline(self):
        paid = self.order(Order.Status.PAID, captured=True)
        unpaid = self.order()
        url = reverse("admin:orders_shipment_add")

        response = self.client.get(url, {"order": paid.pk})
        form = response.context["adminform"].form
        self.assertEqual(set(form.fields["order"].queryset), {paid})
        self.assertEqual(str(form.initial["order"]), str(paid.pk))

        response = self.client.post(
            url,
            {
                "order": unpaid.pk,
                "carrier": "USPS",
                "tracking_number": "940000000000",
                "status": Shipment.Status.SHIPPED,
                "_save": "Save",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("order", response.context["adminform"].form.errors)
        self.assertFalse(Shipment.objects.exists())

        response = self.client.post(
            url,
            {
                "order": paid.pk,
                "carrier": "USPS",
                "tracking_number": "940000000001",
                "status": Shipment.Status.SHIPPED,
                "_save": "Save",
            },
        )

        self.assertRedirects(response, reverse("admin:orders_shipment_changelist"))
        self.assertTrue(
            Shipment.objects.filter(order=paid, tracking_number="940000000001").exists()
        )

    def test_shipment_update_on_ineligible_order_returns_form_error(self):
        order = self.order(Order.Status.PAID, captured=True)
        shipment = Shipment.objects.create(
            order=order,
            carrier="USPS",
            tracking_number="940000000000",
            status=Shipment.Status.SHIPPED,
        )
        order.status = Order.Status.REFUNDED
        order.refunded_total = order.total
        order.refunded_at = timezone.now()
        order.save(
            update_fields=("status", "refunded_total", "refunded_at", "updated_at")
        )

        response = self.client.post(
            reverse("admin:orders_shipment_change", args=[shipment.pk]),
            {"status": Shipment.Status.DELIVERED, "_save": "Save"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("__all__", response.context["adminform"].form.errors)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, Shipment.Status.SHIPPED)

    def test_pending_refund_blocks_new_fulfillment_but_allows_shipment_correction(self):
        order = self.order(Order.Status.PAID, captured=True)
        shipment = Shipment.objects.create(
            order=order,
            carrier="USP",
            tracking_number="940000000000",
            status=Shipment.Status.LABEL_CREATED,
            completes_order=False,
        )
        Refund.objects.create(
            order=order,
            paypal_refund_id="REFUND-PENDING",
            amount=order.total,
            status=Refund.Status.PENDING,
        )

        add_response = self.client.get(reverse("admin:orders_shipment_add"))
        order_response = self.client.get(
            reverse("admin:orders_order_change", args=[order.pk])
        )

        self.assertNotIn(
            order, add_response.context["adminform"].form.fields["order"].queryset
        )
        self.assertFalse(orders_needing_fulfillment().filter(pk=order.pk).exists())
        self.assertContains(order_response, "Unavailable while a refund is pending")
        with self.assertRaisesMessage(
            OrderStateError,
            "Orders with a pending refund cannot receive a new shipment.",
        ):
            record_manual_shipment(order.pk, "USPS", "940000000001")

        corrected = record_manual_shipment(
            order.pk,
            "USPS",
            shipment.tracking_number,
            Shipment.Status.LABEL_CREATED,
            False,
        )

        self.assertEqual(corrected.pk, shipment.pk)
        self.assertEqual(corrected.carrier, "USPS")

    def test_existing_shipment_carrier_is_editable_and_identity_is_fixed(self):
        order = self.order(Order.Status.PAID, captured=True)
        shipment = Shipment.objects.create(
            order=order,
            carrier="USP",
            tracking_number="940000000000",
            status=Shipment.Status.SHIPPED,
        )
        url = reverse("admin:orders_shipment_change", args=[shipment.pk])

        page = self.client.get(url)
        response = self.client.post(
            url,
            {
                "carrier": "USPS",
                "status": Shipment.Status.SHIPPED,
                "completes_order": "on",
                "_save": "Save",
            },
        )

        self.assertContains(page, 'name="carrier"')
        self.assertNotContains(page, 'name="tracking_number"')
        self.assertContains(page, "no more packages are expected")
        self.assertRedirects(response, reverse("admin:orders_shipment_changelist"))
        shipment.refresh_from_db()
        self.assertEqual(shipment.carrier, "USPS")

    def test_paypal_local_pickup_can_be_marked_final_without_a_carrier(self):
        order = self.order(Order.Status.FULFILLING, captured=True)
        shipment = Shipment.objects.create(
            order=order,
            carrier="",
            tracking_number="",
            status=Shipment.Status.DELIVERED,
            source=Shipment.Source.PAYPAL,
            completes_order=False,
        )

        response = self.client.post(
            reverse("admin:orders_shipment_change", args=[shipment.pk]),
            {
                "carrier": "",
                "status": Shipment.Status.DELIVERED,
                "completes_order": "on",
                "_save": "Save",
            },
        )

        self.assertRedirects(response, reverse("admin:orders_shipment_changelist"))
        shipment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(shipment.carrier, "")
        self.assertTrue(shipment.completes_order)
        self.assertEqual(order.status, Order.Status.SHIPPED)

    def test_tracking_number_requires_carrier_in_admin_and_service(self):
        order = self.order(Order.Status.PAID, captured=True)
        url = reverse("admin:orders_shipment_add")

        response = self.client.post(
            url,
            {
                "order": order.pk,
                "carrier": "",
                "tracking_number": "TRACK-1",
                "status": Shipment.Status.SHIPPED,
                "_save": "Save",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("carrier", response.context["adminform"].form.errors)
        with self.assertRaisesMessage(
            ValueError, "Enter a carrier when a tracking number is provided."
        ):
            record_manual_shipment(order.pk, "", "TRACK-2")
        with self.assertRaisesMessage(
            ValueError, "Enter a carrier when a tracking number is provided."
        ):
            record_manual_shipment(order.pk, "   ", "TRACK-3")
        self.assertFalse(Shipment.objects.exists())

    def test_audit_record_detail_pages_are_view_only(self):
        order = self.order(Order.Status.PAID, captured=True)
        event = OrderEvent.objects.create(
            order=order,
            kind="order.tested",
            source=OrderEvent.Source.SYSTEM,
        )
        refund = Refund.objects.create(
            order=order,
            paypal_refund_id="REFUND-COMPLETE",
            amount=order.total,
        )
        request = RequestFactory().get("/admin/")
        request.user = self.user

        for model, instance in (
            (OrderEvent, event),
            (Refund, refund),
            (InventoryReservation, InventoryReservation(pk=1)),
        ):
            with self.subTest(model=model.__name__):
                model_admin = admin.site._registry[model]
                self.assertFalse(model_admin.has_change_permission(request, instance))
                if instance.pk and model is not InventoryReservation:
                    response = self.client.get(
                        reverse(
                            f"admin:orders_{model._meta.model_name}_change",
                            args=[instance.pk],
                        )
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertNotContains(response, 'name="_save"')

    @patch("orders.admin.PayPalClient", PayPalAdminDouble)
    def test_refund_action_requires_dedicated_permission(self):
        order = self.order(Order.Status.PAID, captured=True)
        operator = get_user_model().objects.create_user(
            "order-editor",
            "editor@example.com",
            "admin-test-password",
            is_staff=True,
        )
        operator.user_permissions.add(
            Permission.objects.get(codename="view_order"),
            Permission.objects.get(codename="change_order"),
        )
        self.client.force_login(operator)
        url = reverse("admin:orders_order_changelist")

        page = self.client.get(url)
        denied = self.client.post(
            url, self.action_data(order, confirmed=True), follow=True
        )

        self.assertNotContains(page, "Refund one captured order through PayPal")
        self.assertEqual(denied.status_code, 200)
        self.assertEqual(PayPalAdminDouble.calls, [])
        self.assertFalse(Refund.objects.exists())

        operator.user_permissions.add(Permission.objects.get(codename="refund_order"))
        operator = get_user_model().objects.get(pk=operator.pk)
        self.client.force_login(operator)

        allowed_page = self.client.get(url)
        completed = self.client.post(
            url, self.action_data(order, confirmed=True), follow=True
        )

        self.assertContains(allowed_page, "Refund one captured order through PayPal")
        self.assertContains(completed, f"Refunded {order.reference}")
        self.assertEqual(len(PayPalAdminDouble.calls), 1)

    @patch("orders.admin.PayPalClient", PayPalAdminDouble)
    def test_refund_rejects_multiple_or_uncaptured_orders_before_paypal(self):
        paid = self.order(Order.Status.PAID, captured=True)
        unpaid = self.order()
        url = reverse("admin:orders_order_changelist")

        multiple = self.client.post(url, self.action_data(paid, unpaid), follow=True)
        uncaptured = self.client.post(url, self.action_data(unpaid), follow=True)

        self.assertContains(multiple, "Select exactly one order to refund.")
        self.assertContains(uncaptured, "Only captured orders can be refunded.")
        self.assertEqual(PayPalAdminDouble.calls, [])
        paid.refresh_from_db()
        self.assertEqual(paid.status, Order.Status.PAID)

    @patch("orders.admin.PayPalClient", PayPalAdminDouble)
    def test_refund_requires_confirmation_then_refunds_one_order(self):
        order = self.order(Order.Status.PAID, captured=True)
        url = reverse("admin:orders_order_changelist")

        confirmation = self.client.post(url, self.action_data(order))

        self.assertEqual(confirmation.status_code, 200)
        self.assertTemplateUsed(
            confirmation, "admin/orders/order/refund_confirmation.html"
        )
        self.assertContains(confirmation, order.reference)
        self.assertContains(confirmation, "USD 10.00")
        self.assertEqual(PayPalAdminDouble.calls, [])

        completed = self.client.post(
            url, self.action_data(order, confirmed=True), follow=True
        )

        self.assertContains(completed, f"Refunded {order.reference} for 10.00 USD.")
        self.assertEqual(len(PayPalAdminDouble.calls), 1)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.REFUNDED)
        self.assertEqual(order.refunded_total, order.total)

    @patch("orders.admin.PayPalClient", PayPalRejectingAdminDouble)
    def test_refund_provider_rejection_is_reported_without_server_error(self):
        order = self.order(Order.Status.PAID, captured=True)

        response = self.client.post(
            reverse("admin:orders_order_changelist"),
            self.action_data(order, confirmed=True),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f"Refund request for {order.reference} was not completed",
        )
        self.assertContains(response, "REFUND_NOT_ALLOWED")
        self.assertContains(response, "PayPal debug ID: DEBUG-REFUND-1")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.refunded_total, Decimal("0.00"))
        self.assertFalse(order.refunds.exists())

    @patch("orders.admin.PayPalClient", PayPalAdminDouble)
    def test_pending_refund_is_reported_without_claiming_completion(self):
        order = self.order(Order.Status.PAID, captured=True)
        PayPalAdminDouble.status = "PENDING"

        response = self.client.post(
            reverse("admin:orders_order_changelist"),
            self.action_data(order, confirmed=True),
            follow=True,
        )

        self.assertContains(response, f"PayPal refund for {order.reference} is pending")
        self.assertNotContains(response, f"Refunded {order.reference}")
        self.assertEqual(Refund.objects.get().status, Refund.Status.PENDING)
        order.refresh_from_db()
        self.assertEqual(order.refunded_total, Decimal("0.00"))

    @patch("orders.admin.PayPalClient", PayPalAdminDouble)
    def test_paypal_case_review_queue_blocks_actions_until_acknowledged(self):
        order = self.order(Order.Status.PAID, captured=True)
        case = PayPalCase.objects.create(
            order=order,
            kind=PayPalCase.Kind.DISPUTE,
            paypal_case_id="PP-D-ADMIN-1",
            status=PayPalCase.Status.WAITING_FOR_SELLER_RESPONSE,
            reason="ITEM_NOT_RECEIVED",
            stage="INQUIRY",
            amount=Decimal("10.00"),
            currency="USD",
            last_event_type="CUSTOMER.DISPUTE.CREATED",
            provider_updated_at=timezone.now(),
        )
        case_url = reverse("admin:orders_paypalcase_changelist")

        order_page = self.client.get(
            reverse("admin:orders_order_change", args=[order.pk])
        )
        refund_response = self.client.post(
            reverse("admin:orders_order_changelist"),
            self.action_data(order, confirmed=True),
            follow=True,
        )
        queue = self.client.get(case_url, {"case_review": "needed"})

        self.assertContains(
            order_page, "Unavailable while a PayPal case needs review"
        )
        self.assertContains(order_page, "PP-D-ADMIN-1")
        self.assertContains(
            refund_response,
            "Review the open PayPal case before refunding this order.",
        )
        self.assertEqual(PayPalAdminDouble.calls, [])
        self.assertFalse(
            orders_needing_fulfillment().filter(pk=order.pk).exists()
        )
        self.assertContains(queue, "PP-D-ADMIN-1")

        reviewed = self.client.post(
            case_url,
            {
                "action": "mark_reviewed",
                ACTION_CHECKBOX_NAME: [str(case.pk)],
                "select_across": "0",
            },
            follow=True,
        )

        self.assertContains(reviewed, "Marked 1 PayPal case(s) reviewed.")
        case.refresh_from_db()
        self.assertFalse(case.needs_review)
        self.assertIsNotNone(case.reviewed_at)
        self.assertTrue(
            orders_needing_fulfillment().filter(pk=order.pk).exists()
        )
        refreshed_order_page = self.client.get(
            reverse("admin:orders_order_change", args=[order.pk])
        )
        self.assertContains(refreshed_order_page, "Add shipment")
