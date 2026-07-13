import json
import uuid
from hashlib import sha256
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import connections
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from catalog.ebay import EbayResponseError
from catalog.inventory import EbayInventoryGateway
from catalog.models import Product
from orders.inventory import InventoryUnavailable
from orders.models import Order
from orders.paypal import PayPalClient
from orders.services import (
    CheckoutLine,
    OrderStateError,
    PayPalOrderInactive,
    PaymentDataError,
    ShippingAddress,
    capture_paypal_order,
    cancel_order,
    create_guest_order,
    create_paypal_checkout,
    process_paypal_webhook,
)

from .cart import Cart
from .configuration import checkout_enabled, store_settings
from .forms import CheckoutForm


def _cart_summary(request):
    cart = Cart(request)
    store = store_settings()
    items = cart.lines()
    shipping = store.flat_shipping_amount if store and items else Decimal("0.00")
    subtotal, shipping, total = cart.totals(shipping)
    return {
        "cart": cart,
        "items": items,
        "subtotal": subtotal,
        "shipping": shipping,
        "total": total,
        "checkout_enabled": checkout_enabled(store),
        "cart_valid": all(
            0 < line.quantity <= line.available_quantity for line in items
        ),
    }


@require_GET
def catalog(request):
    products = Product.objects.purchasable().prefetch_related("images", "variants")
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


@require_GET
def product_detail(request, slug):
    product = get_object_or_404(
        Product.objects.purchasable().prefetch_related("images", "variants"),
        slug=slug,
    )
    variants = [
        variant
        for variant in product.variants.all()
        if variant.active and variant.purchasable and variant.quantity > 0
    ]
    return render(
        request,
        "catalog/product_detail.html",
        {
            "product": product,
            "images": product.images.all(),
            "variants": variants,
            "checkout_enabled": checkout_enabled(),
        },
    )


@require_GET
def cart(request):
    return render(request, "orders/cart.html", _cart_summary(request))


@require_POST
def cart_add(request, slug):
    product = get_object_or_404(Product, slug=slug)
    variant_value = request.POST.get("variant_id")
    try:
        variant_id = int(variant_value) if variant_value else None
    except ValueError:
        messages.error(request, "Choose a valid product option.")
        return redirect("storefront:product_detail", slug=slug)
    variant = get_object_or_404(product.variants, pk=variant_id) if variant_id else None
    try:
        Cart(request).add(product, int(request.POST["quantity"]), variant)
    except (KeyError, ValueError) as error:
        messages.error(request, str(error))
        return redirect("storefront:product_detail", slug=slug)
    messages.success(request, "Item added to your cart.")
    return redirect("storefront:cart")


@require_POST
def cart_update(request, line_id):
    try:
        Cart(request).update(line_id, int(request.POST["quantity"]))
    except (KeyError, StopIteration):
        raise Http404("Cart item not found.")
    except ValueError as error:
        messages.error(request, str(error))
    return redirect("storefront:cart")


@require_POST
def cart_remove(request, line_id):
    try:
        Cart(request).remove(line_id)
    except KeyError:
        raise Http404("Cart item not found.")
    return redirect("storefront:cart")


@never_cache
@require_GET
def checkout(request):
    context = _cart_summary(request)
    if not context["items"]:
        return redirect("storefront:cart")
    if not context["checkout_enabled"]:
        messages.error(request, "Checkout is temporarily unavailable.")
        return redirect("storefront:cart")
    if not context["cart_valid"]:
        messages.error(request, "Your cart contains an unavailable quantity.")
        return redirect("storefront:cart")
    context.update(
        form=CheckoutForm(initial={"shipping_country_code": "US"}),
        paypal_client_id=settings.PAYPAL_CLIENT_ID,
    )
    return render(request, "orders/checkout.html", context)


@never_cache
@require_POST
def paypal_create(request):
    context = _cart_summary(request)
    if not context["items"] or not context["checkout_enabled"]:
        return JsonResponse({"error": "Checkout is unavailable."}, status=409)
    form = CheckoutForm(request.POST)
    if not form.is_valid():
        return JsonResponse(
            {"error": "Review the shipping details and try again."}, status=400
        )
    data = form.cleaned_data
    form_fingerprint = sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    previous_order_id = request.session.get(Cart.checkout_order_key)
    previous_order = Order.objects.filter(pk=previous_order_id).first()
    previous_fingerprint = request.session.get(Cart.checkout_form_key)
    if previous_order and previous_order.paid_at:
        redirect_url = reverse(
            "storefront:order_confirmation",
            kwargs={"token": previous_order.status_token},
        )
        Cart(request).clear()
        return JsonResponse({"redirect_url": redirect_url})
    if (
        previous_order
        and previous_fingerprint == form_fingerprint
        and previous_order.status
        in {Order.Status.PAYMENT_PROCESSING, Order.Status.CAPTURE_PENDING}
    ):
        try:
            with PayPalClient() as client:
                previous_order = capture_paypal_order(
                    previous_order.pk, client, EbayInventoryGateway()
                )
        except (
            EbayResponseError,
            InventoryUnavailable,
            OrderStateError,
            PaymentDataError,
        ) as error:
            return JsonResponse({"error": str(error)}, status=409)
        redirect_url = reverse(
            "storefront:order_confirmation",
            kwargs={"token": previous_order.status_token},
        )
        Cart(request).clear()
        return JsonResponse({"redirect_url": redirect_url})
    rotate_checkout = previous_fingerprint != form_fingerprint or (
        previous_order
        and previous_order.status
        not in {Order.Status.AWAITING_PAYMENT, Order.Status.PAYMENT_PROCESSING, Order.Status.CAPTURE_PENDING}
    )
    if rotate_checkout and previous_order:
        if previous_order.status == Order.Status.AWAITING_PAYMENT:
            cancel_order(previous_order.pk, EbayInventoryGateway())
        elif previous_order.status in {
            Order.Status.PAYMENT_PROCESSING,
            Order.Status.CAPTURE_PENDING,
        }:
            return JsonResponse(
                {"error": "The previous payment is still being confirmed."}, status=409
            )
    key = request.session.get(Cart.checkout_key)
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
            inventory=EbayInventoryGateway(),
        )
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
        request.session.pop(Cart.checkout_key, None)
        request.session.pop(Cart.checkout_order_key, None)
        request.session.pop(Cart.checkout_form_key, None)
        return JsonResponse({"error": str(error)}, status=409)
    return JsonResponse({"paypal_order_id": order.paypal_order_id})


@never_cache
@require_POST
def paypal_capture(request):
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
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
    except (EbayResponseError, InventoryUnavailable, OrderStateError, PaymentDataError) as error:
        return JsonResponse({"error": str(error)}, status=409)
    Cart(request).clear()
    redirect_url = reverse("storefront:order_confirmation", kwargs={"token": order.status_token})
    return JsonResponse({"redirect_url": redirect_url})


@never_cache
@require_GET
def order_confirmation(request, token):
    order = get_object_or_404(
        Order.objects.prefetch_related("items", "shipments"),
        status_token=token,
        paid_at__isnull=False,
    )
    return render(request, "orders/order_confirmation.html", {"order": order})


@never_cache
@require_GET
def order_status(request, token):
    order = get_object_or_404(
        Order.objects.prefetch_related("items", "shipments"), status_token=token
    )
    return render(request, "orders/order_status.html", {"order": order})


@csrf_exempt
@require_POST
def paypal_webhook(request):
    event = json.loads(request.body)
    header_names = (
        "PAYPAL-AUTH-ALGO",
        "PAYPAL-CERT-URL",
        "PAYPAL-TRANSMISSION-ID",
        "PAYPAL-TRANSMISSION-SIG",
        "PAYPAL-TRANSMISSION-TIME",
    )
    headers = {name: request.headers[name] for name in header_names}
    with PayPalClient() as client:
        process_paypal_webhook(headers, event, client, EbayInventoryGateway())
    return JsonResponse({"received": True})


@require_GET
def health(request):
    with connections["default"].cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return JsonResponse({"status": "ok"})


@require_GET
def robots(request):
    return HttpResponse("User-agent: *\nDisallow: /\n", content_type="text/plain")


@require_GET
def privacy(request):
    return render(request, "privacy.html")
