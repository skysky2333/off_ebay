from django.conf import settings

from .models import StoreSettings


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
    )
    return bool(store and store.checkout_enabled and all(required))
