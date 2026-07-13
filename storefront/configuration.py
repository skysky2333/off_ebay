from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .models import StoreSettings


PROVIDER_ENDPOINTS = {
    "live": (
        "https://api.ebay.com/ws/api.dll",
        "https://api.ebay.com/identity/v1/oauth2/token",
        "https://api.ebay.com/commerce/notification/v1/public_key",
        "https://api-m.paypal.com",
    ),
    "sandbox": (
        "https://api.sandbox.ebay.com/ws/api.dll",
        "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
        "https://api.sandbox.ebay.com/commerce/notification/v1/public_key",
        "https://api-m.sandbox.paypal.com",
    ),
}


def provider_endpoints(configuration=settings):
    return (
        configuration.EBAY_TRADING_ENDPOINT,
        configuration.EBAY_TOKEN_ENDPOINT,
        configuration.EBAY_NOTIFICATION_PUBLIC_KEY_ENDPOINT,
        configuration.PAYPAL_API_BASE_URL,
    )


def integration_environment(configuration=settings):
    endpoints = provider_endpoints(configuration)
    matches = [name for name, values in PROVIDER_ENDPOINTS.items() if values == endpoints]
    if len(matches) != 1:
        raise ImproperlyConfigured(
            "eBay and PayPal endpoints must all use the same live or sandbox environment."
        )
    return matches[0]


def store_settings():
    return StoreSettings.objects.filter(pk=1).first()


def checkout_enabled(store=None):
    store = store or store_settings()
    required = (
        settings.EBAY_CLIENT_ID,
        settings.EBAY_CLIENT_SECRET,
        settings.EBAY_REFRESH_TOKEN,
        settings.EBAY_COMPATIBILITY_LEVEL,
        settings.PAYPAL_CLIENT_ID,
        settings.PAYPAL_CLIENT_SECRET,
        settings.PAYPAL_WEBHOOK_ID,
        settings.SUPPORT_EMAIL,
    )
    if not store or not store.checkout_enabled or not all(required):
        return False
    integration_environment()
    return True
