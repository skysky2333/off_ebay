import json
import uuid
from decimal import Decimal
from hashlib import sha256

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import connections
from django.db.models import Prefetch, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template import loader
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods, require_safe

from catalog.ebay import EbayResponseError
from catalog.inventory import EbayInventoryGateway
from catalog.models import Product, ProductVariant
from catalog.notifications import (
    EbayNotificationMalformed,
    EbayNotificationSignatureMalformed,
    EbayNotificationSignatureMismatch,
    marketplace_account_deletion_fields,
    verify_ebay_notification,
)
from catalog.services import process_ebay_account_closure
from orders.inventory import InventoryUnavailable
from orders.models import Order, Shipment
from orders.paypal import PayPalClient, PayPalInstrumentDeclined
from orders.services import (
    CheckoutLine,
    IdempotencyConflict,
    OrderReservationExpired,
    OrderStateError,
    PayPalOrderInactive,
    PaymentDataError,
    ShippingAddress,
    WebhookVerificationError,
    capture_paypal_order,
    cancel_order,
    create_guest_order,
    create_paypal_checkout,
    process_paypal_webhook,
)

from .cart import Cart
from .configuration import checkout_enabled, store_settings
from .forms import CheckoutForm


def _payment_response(request, order):
    if order.paid_at:
        Cart(request).complete(order.pk)
        redirect_url = reverse(
            "storefront:order_confirmation", kwargs={"token": order.status_token}
        )
        return JsonResponse({"redirect_url": redirect_url})
    if order.status == Order.Status.CAPTURE_PENDING:
        redirect_url = reverse(
            "storefront:order_status", kwargs={"token": order.status_token}
        )
        return JsonResponse({"redirect_url": redirect_url})
    if order.status == Order.Status.CANCELLED and order.paypal_status in {
        "DECLINED",
        "FAILED",
    }:
        Cart(request).reset_checkout()
        return JsonResponse(
            {
                "error": "Payment was declined. Choose another payment method and try again.",
                "code": "PAYMENT_DECLINED",
            },
            status=422,
        )
    raise OrderStateError("PayPal returned an unresolved payment state.")


def _checkout_quote(context):
    return {
        "shipping": format(context["shipping"], ".2f"),
        "total": format(context["total"], ".2f"),
        "lines": [
            {
                "line_id": line.line_id,
                "quantity": line.quantity,
                "unit_price": format(line.unit_price, ".2f"),
            }
            for line in sorted(context["items"], key=lambda item: item.line_id)
        ],
    }


def _fingerprint(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _paid_checkout_redirect(request):
    order = Order.objects.filter(
        pk=request.session.get(Cart.checkout_order_key), paid_at__isnull=False
    ).first()
    if order is None:
        return None
    Cart(request).complete(order.pk)
    return redirect("storefront:order_confirmation", token=order.status_token)


def _cart_summary(request):
    cart = Cart(request)
    store = store_settings()
    fixed_order = cart.fixed_order()
    items = cart.lines(fixed_order)
    shipping = Decimal("0.00")
    if items and fixed_order:
        shipping = fixed_order.shipping_total
    elif items and store:
        shipping = store.flat_shipping_amount
    subtotal = sum((line.line_total for line in items), Decimal("0.00"))
    total = subtotal + shipping
    return {
        "cart": cart,
        "items": items,
        "item_count": sum(line.quantity for line in items),
        "subtotal": subtotal,
        "shipping": shipping,
        "total": total,
        "checkout_enabled": checkout_enabled(store),
        "cart_valid": all(
            0 < line.quantity <= line.available_quantity for line in items
        ),
        "fixed_order": fixed_order,
    }


def _posted_quantity(request):
    value = request.POST.get("quantity", "")
    if not value.isascii() or not value.isdigit() or len(value) > 10:
        raise ValueError("Choose a valid quantity.")
    return int(value)


def _guard_cart_mutation(request, start_new_cart=False):
    order_id = request.session.get(Cart.checkout_order_key)
    if not order_id:
        return None
    order = Order.objects.filter(pk=order_id).first()
    if not order:
        return None
    if order.paid_at:
        if start_new_cart:
            Cart(request).clear()
            return None
        return redirect(
            "storefront:order_confirmation", token=order.status_token
        )
    if order.status in {
        Order.Status.PAYMENT_PROCESSING,
        Order.Status.CAPTURE_PENDING,
        Order.Status.FUNDING_RETRY,
    }:
        return redirect("storefront:order_status", token=order.status_token)
    if order.status == Order.Status.AWAITING_PAYMENT:
        cancel_order(order.pk, EbayInventoryGateway())
    return None


@require_safe
def catalog(request):
    products = Product.objects.purchasable().prefetch_related(
        "images",
        Prefetch(
            "variants", queryset=ProductVariant.objects.with_availability()
        ),
    )
    query = request.GET.get("q", "").strip()
    category = request.GET.get("category", "").strip()
    if query:
        products = products.filter(
            Q(title__icontains=query)
            | Q(condition__icontains=query)
            | Q(category_name__icontains=query)
        )
    if category:
        products = products.filter(category_name=category)
    categories = list(
        Product.objects.purchasable()
        .exclude(category_name="")
        .order_by("category_name")
        .values_list("category_name", flat=True)
        .distinct()
    )
    return render(
        request,
        "catalog/catalog.html",
        {
            "products": list(products),
            "categories": categories,
            "query": query,
            "selected_category": category,
        },
    )


@require_safe
def product_detail(request, slug):
    product = get_object_or_404(
        Product.objects.purchasable().prefetch_related(
            "images",
            Prefetch(
                "variants", queryset=ProductVariant.objects.with_availability()
            ),
        ),
        slug=slug,
    )
    variants = [
        variant
        for variant in product.variants.all()
        if variant.active
        and variant.purchasable
        and variant.sku
        and variant.available_quantity > 0
    ]
    return render(
        request,
        "catalog/product_detail.html",
        {
            "product": product,
            "images": product.images.all(),
            "variants": variants,
            "variant_max_quantity": max(
                (variant.available_quantity for variant in variants), default=0
            ),
            "checkout_enabled": checkout_enabled(),
        },
    )


@never_cache
@require_safe
def cart(request):
    recovery = _paid_checkout_redirect(request)
    if recovery:
        return recovery
    fixed_order = Cart(request).fixed_order()
    if fixed_order:
        return redirect("storefront:order_status", token=fixed_order.status_token)
    return render(request, "orders/cart.html", _cart_summary(request))


@require_POST
def cart_add(request, slug):
    guard = _guard_cart_mutation(request, start_new_cart=True)
    if guard:
        return guard
    product = get_object_or_404(Product, slug=slug)
    if not product.is_purchasable:
        messages.error(request, "This item is no longer available.")
        return redirect("storefront:catalog")
    variant_value = request.POST.get("variant_id")
    try:
        variant_id = int(variant_value) if variant_value else None
    except ValueError:
        messages.error(request, "Choose a valid product option.")
        return redirect("storefront:product_detail", slug=slug)
    variant = get_object_or_404(product.variants, pk=variant_id) if variant_id else None
    try:
        Cart(request).add(product, _posted_quantity(request), variant)
    except ValueError as error:
        messages.error(request, str(error))
        return redirect("storefront:product_detail", slug=slug)
    messages.success(request, "Item added to your cart.")
    return redirect("storefront:cart")


@require_POST
def cart_update(request, line_id):
    guard = _guard_cart_mutation(request)
    if guard:
        return guard
    try:
        quantity = _posted_quantity(request)
    except ValueError as error:
        messages.error(request, str(error))
        return redirect("storefront:cart")
    try:
        Cart(request).update(line_id, quantity)
    except (KeyError, StopIteration):
        raise Http404("Cart item not found.")
    except ValueError as error:
        messages.error(request, str(error))
    return redirect("storefront:cart")


@require_POST
def cart_remove(request, line_id):
    guard = _guard_cart_mutation(request)
    if guard:
        return guard
    try:
        Cart(request).remove(line_id)
    except KeyError:
        raise Http404("Cart item not found.")
    return redirect("storefront:cart")


@never_cache
@require_safe
def checkout(request):
    recovery = _paid_checkout_redirect(request)
    if recovery:
        return recovery
    context = _cart_summary(request)
    fixed_order = context["fixed_order"]
    if fixed_order and fixed_order.status != Order.Status.FUNDING_RETRY:
        return redirect(
            "storefront:order_status", token=fixed_order.status_token
        )
    if not context["items"]:
        return redirect("storefront:cart")
    if not context["checkout_enabled"] and fixed_order is None:
        if request.method == "GET":
            messages.error(request, "Checkout is temporarily unavailable.")
        return redirect("storefront:cart")
    if not context["cart_valid"]:
        if request.method == "GET":
            messages.error(request, "Your cart contains an unavailable quantity.")
        return redirect("storefront:cart")
    if request.method == "GET" and not request.session.get(Cart.checkout_key):
        request.session[Cart.checkout_key] = str(uuid.uuid4())
    initial = {"shipping_country_code": "US"}
    if context["fixed_order"]:
        order = context["fixed_order"]
        initial.update(
            customer_email=order.customer_email,
            customer_phone=order.customer_phone,
            customer_name=order.customer_name,
            shipping_line_1=order.shipping_line_1,
            shipping_line_2=order.shipping_line_2,
            shipping_city=order.shipping_city,
            shipping_region=order.shipping_region,
            shipping_postal_code=order.shipping_postal_code,
            shipping_country_code=order.shipping_country_code,
        )
    context.update(
        form=CheckoutForm(initial=initial),
        paypal_client_id=settings.PAYPAL_CLIENT_ID,
        quote_fingerprint=_fingerprint(_checkout_quote(context)),
    )
    return render(request, "orders/checkout.html", context)


@never_cache
@require_POST
def paypal_create(request):
    previous_order_id = request.session.get(Cart.checkout_order_key)
    previous_order = Order.objects.filter(pk=previous_order_id).first()
    session_checkout_key = request.session.get(Cart.checkout_key)
    if previous_order is None and session_checkout_key:
        previous_order = Order.objects.filter(
            checkout_key=session_checkout_key
        ).first()
        if previous_order:
            request.session[Cart.checkout_order_key] = previous_order.pk
    if previous_order and previous_order.paid_at:
        return _payment_response(request, previous_order)
    if previous_order and previous_order.status in {
        Order.Status.PAYMENT_PROCESSING,
        Order.Status.CAPTURE_PENDING,
    }:
        try:
            with PayPalClient() as client:
                previous_order = capture_paypal_order(
                    previous_order.pk, client, EbayInventoryGateway()
                )
        except PayPalInstrumentDeclined:
            return JsonResponse({"paypal_order_id": previous_order.paypal_order_id})
        except (
            EbayResponseError,
            InventoryUnavailable,
            OrderStateError,
            PaymentDataError,
        ) as error:
            return JsonResponse({"error": str(error)}, status=409)
        return _payment_response(request, previous_order)
    if previous_order and previous_order.status == Order.Status.FUNDING_RETRY:
        return JsonResponse({"paypal_order_id": previous_order.paypal_order_id})
    context = _cart_summary(request)
    if not context["items"] or not context["checkout_enabled"]:
        return JsonResponse({"error": "Checkout is unavailable."}, status=409)
    quote = _checkout_quote(context)
    if request.POST.get("quote_fingerprint") != _fingerprint(quote):
        return JsonResponse(
            {
                "error": (
                    "The order total changed. Refresh checkout and review the updated total."
                )
            },
            status=409,
        )
    form = CheckoutForm(request.POST)
    if not form.is_valid():
        return JsonResponse(
            {"error": "Review the shipping details and try again."}, status=400
        )
    data = form.cleaned_data
    form_fingerprint = _fingerprint({"customer": data, "quote": quote})
    previous_fingerprint = request.session.get(Cart.checkout_form_key)
    checkout_conflicted = request.session.pop(Cart.checkout_conflict_key, False)
    rotate_checkout = bool(
        previous_order
        and (
            (
                checkout_conflicted
                or (
                    previous_fingerprint is not None
                    and previous_fingerprint != form_fingerprint
                )
            )
            or previous_order.status
            not in {
                Order.Status.AWAITING_PAYMENT,
                Order.Status.PAYMENT_PROCESSING,
                Order.Status.CAPTURE_PENDING,
                Order.Status.FUNDING_RETRY,
            }
        )
    )
    if rotate_checkout and previous_order:
        if previous_order.status == Order.Status.AWAITING_PAYMENT:
            cancel_order(previous_order.pk, EbayInventoryGateway())
        elif previous_order.status in {
            Order.Status.PAYMENT_PROCESSING,
            Order.Status.CAPTURE_PENDING,
            Order.Status.FUNDING_RETRY,
        }:
            return JsonResponse(
                {"error": "The previous payment is still being confirmed."}, status=409
            )
    key = session_checkout_key
    if rotate_checkout or not key:
        key = str(uuid.uuid4())
        request.session[Cart.checkout_key] = key
        request.session.pop(Cart.checkout_order_key, None)
    request.session[Cart.checkout_form_key] = form_fingerprint
    address = ShippingAddress(
        name=data["customer_name"],
        line_1=data["shipping_line_1"],
        line_2=data["shipping_line_2"],
        city=data["shipping_city"],
        region=data["shipping_region"],
        postal_code=data["shipping_postal_code"],
        country_code=data["shipping_country_code"],
        phone=data["customer_phone"],
    )
    lines = [
        CheckoutLine(
            product_id=line.product.pk,
            variant_id=line.variant.pk if line.variant else None,
            quantity=line.quantity,
        )
        for line in context["items"]
    ]
    try:
        order = create_guest_order(
            checkout_key=uuid.UUID(key),
            email=data["customer_email"],
            address=address,
            lines=lines,
            shipping_total=context["shipping"],
            expected_total=context["total"],
            inventory=EbayInventoryGateway(),
        )
    except IdempotencyConflict as error:
        conflicting_order = previous_order or Order.objects.filter(
            checkout_key=key
        ).first()
        if conflicting_order:
            request.session[Cart.checkout_order_key] = conflicting_order.pk
            request.session[Cart.checkout_conflict_key] = True
        return JsonResponse({"error": str(error)}, status=409)
    except (InventoryUnavailable, ValidationError, ValueError) as error:
        return JsonResponse({"error": str(error)}, status=409)
    request.session[Cart.checkout_order_key] = order.pk
    try:
        with PayPalClient() as client:
            order = create_paypal_checkout(
                order.pk,
                request.build_absolute_uri(reverse("storefront:checkout")),
                request.build_absolute_uri(reverse("storefront:cart")),
                client,
            )
    except PayPalOrderInactive as error:
        cancel_order(order.pk, EbayInventoryGateway(), capture_definitely_absent=True)
        Cart(request).reset_checkout()
        return JsonResponse({"error": str(error)}, status=409)
    except OrderReservationExpired as error:
        order.refresh_from_db()
        if order.status == Order.Status.AWAITING_PAYMENT:
            cancel_order(order.pk, EbayInventoryGateway())
        Cart(request).reset_checkout()
        return JsonResponse({"error": str(error)}, status=409)
    except OrderStateError as error:
        return JsonResponse({"error": str(error)}, status=409)
    return JsonResponse({"paypal_order_id": order.paypal_order_id})


@never_cache
@require_POST
def paypal_capture(request):
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid checkout request."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "Invalid checkout request."}, status=400)
    order_id = request.session.get(Cart.checkout_order_key)
    if not order_id:
        return JsonResponse({"error": "Checkout session expired."}, status=409)
    order = get_object_or_404(Order, pk=order_id)
    if payload.get("paypal_order_id") != order.paypal_order_id:
        return JsonResponse({"error": "PayPal order does not match checkout."}, status=400)
    try:
        with PayPalClient() as client:
            order = capture_paypal_order(order.pk, client, EbayInventoryGateway())
    except PayPalInstrumentDeclined as error:
        return JsonResponse(
            {"error": str(error), "code": "INSTRUMENT_DECLINED"}, status=422
        )
    except (EbayResponseError, InventoryUnavailable, OrderStateError, PaymentDataError) as error:
        return JsonResponse({"error": str(error)}, status=409)
    return _payment_response(request, order)


@never_cache
@require_safe
def order_confirmation(request, token):
    order = get_object_or_404(
        Order.objects.prefetch_related("items", "shipments"),
        status_token=token,
        paid_at__isnull=False,
    )
    if order.status == Order.Status.REFUNDED:
        Cart(request).forget_order(order.pk)
        return redirect("storefront:order_status", token=order.status_token)
    Cart(request).forget_order(order.pk)
    shipments = [
        shipment
        for shipment in order.shipments.all()
        if shipment.status != Shipment.Status.CANCELLED
    ]
    return render(
        request,
        "orders/order_confirmation.html",
        {"order": order, "shipments": shipments},
    )


@never_cache
@require_safe
def order_status(request, token):
    order = get_object_or_404(
        Order.objects.prefetch_related("items", "shipments"), status_token=token
    )
    if (
        order.paid_at
        and request.session.get(Cart.checkout_order_key) == order.pk
    ):
        Cart(request).complete(order.pk)
        return redirect("storefront:order_confirmation", token=order.status_token)
    shipments = [
        shipment
        for shipment in order.shipments.all()
        if shipment.status != Shipment.Status.CANCELLED
    ]
    has_shipped = any(
        shipment.status in {Shipment.Status.SHIPPED, Shipment.Status.DELIVERED}
        for shipment in shipments
    )
    has_delivered = any(
        shipment.status == Shipment.Status.DELIVERED for shipment in shipments
    )
    is_delivered = any(shipment.completes_order for shipment in shipments) and all(
        shipment.status == Shipment.Status.DELIVERED for shipment in shipments
    )
    return render(
        request,
        "orders/order_status.html",
        {
            "order": order,
            "has_shipped": has_shipped,
            "has_delivered": has_delivered,
            "is_delivered": is_delivered,
            "shipments": shipments,
            "can_resume_payment": (
                order.status == Order.Status.FUNDING_RETRY
                and request.session.get(Cart.checkout_order_key) == order.pk
            ),
        },
    )


@csrf_exempt
@require_POST
def paypal_webhook(request):
    header_names = (
        "PAYPAL-AUTH-ALGO",
        "PAYPAL-CERT-URL",
        "PAYPAL-TRANSMISSION-ID",
        "PAYPAL-TRANSMISSION-SIG",
        "PAYPAL-TRANSMISSION-TIME",
    )
    try:
        event = json.loads(request.body)
        headers = {name: request.headers[name] for name in header_names}
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError):
        return JsonResponse({"error": "Invalid PayPal webhook request."}, status=400)
    if not isinstance(event, dict):
        return JsonResponse({"error": "Invalid PayPal webhook request."}, status=400)
    try:
        with PayPalClient() as client:
            process_paypal_webhook(headers, event, client, EbayInventoryGateway())
    except (PaymentDataError, WebhookVerificationError) as error:
        return JsonResponse({"error": str(error)}, status=400)
    return JsonResponse({"received": True})


@csrf_exempt
@require_http_methods(("GET", "POST"))
def ebay_account_deletion(request):
    if request.method == "GET":
        challenge_codes = request.GET.getlist("challenge_code")
        token = settings.EBAY_MARKETPLACE_DELETION_VERIFICATION_TOKEN
        if len(challenge_codes) != 1 or not challenge_codes[0]:
            return JsonResponse({"error": "Invalid eBay challenge."}, status=400)
        if not settings.STORE_DOMAIN or not token:
            raise ImproperlyConfigured(
                "The eBay account deletion endpoint is not configured."
            )
        endpoint = (
            f"https://{settings.STORE_DOMAIN}"
            f"{reverse('storefront:ebay_account_deletion')}"
        )
        response = sha256(f"{challenge_codes[0]}{token}{endpoint}".encode()).hexdigest()
        return JsonResponse({"challengeResponse": response})

    if request.content_type != "application/json":
        return JsonResponse({"error": "Invalid eBay notification."}, status=400)
    try:
        message = json.loads(request.body)
        notification_id, username, user_id, eias_token = (
            marketplace_account_deletion_fields(message)
        )
        verify_ebay_notification(message, request.headers.get("X-EBAY-SIGNATURE"))
    except (EbayNotificationSignatureMalformed, EbayNotificationSignatureMismatch):
        return JsonResponse(
            {"error": "eBay notification signature verification failed."}, status=412
        )
    except (json.JSONDecodeError, UnicodeDecodeError, EbayNotificationMalformed):
        return JsonResponse({"error": "Invalid eBay notification."}, status=400)
    process_ebay_account_closure(notification_id, username, user_id, eias_token)
    return HttpResponse(status=204)


@require_safe
def health(request):
    with connections["default"].cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return JsonResponse({"status": "ok"})


@require_safe
def robots(request):
    return HttpResponse("User-agent: *\nDisallow: /\n", content_type="text/plain")


def server_error(request):
    template = loader.get_template("500.html")
    return HttpResponse(
        template.render(
            {
                "store_name": settings.STORE_NAME,
                "support_email": settings.SUPPORT_EMAIL,
            }
        ),
        status=500,
    )


@require_safe
def privacy(request):
    return render(request, "privacy.html")


def csrf_failure(request, reason=""):
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse(
            {
                "error": (
                    "Your secure session expired. Refresh this page and try again."
                )
            },
            status=403,
        )
    return render(request, "403_csrf.html", status=403)
