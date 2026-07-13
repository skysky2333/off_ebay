from django.urls import path
from django.views.generic import RedirectView

from . import views

app_name = "storefront"

urlpatterns = [
    path(
        "favicon.ico",
        RedirectView.as_view(url="/static/storefront/favicon.svg", permanent=True),
    ),
    path("", views.catalog, name="catalog"),
    path("products/<slug:slug>/", views.product_detail, name="product_detail"),
    path("cart/", views.cart, name="cart"),
    path("cart/add/<slug:slug>/", views.cart_add, name="cart_add"),
    path("cart/update/<str:line_id>/", views.cart_update, name="cart_update"),
    path("cart/remove/<str:line_id>/", views.cart_remove, name="cart_remove"),
    path("checkout/", views.checkout, name="checkout"),
    path("checkout/paypal/create/", views.paypal_create, name="paypal_create"),
    path("checkout/paypal/capture/", views.paypal_capture, name="paypal_capture"),
    path("orders/<uuid:token>/confirmed/", views.order_confirmation, name="order_confirmation"),
    path("orders/<uuid:token>/", views.order_status, name="order_status"),
    path("webhooks/paypal/", views.paypal_webhook, name="paypal_webhook"),
    path("health/", views.health, name="health"),
    path("privacy/", views.privacy, name="privacy"),
    path("robots.txt", views.robots, name="robots"),
]
