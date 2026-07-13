from django.conf import settings

from .cart import Cart


def store_context(request):
    return {
        "store_name": settings.STORE_NAME,
        "direct_discount_percent": settings.DIRECT_DISCOUNT_PERCENT,
        "support_email": settings.SUPPORT_EMAIL,
        "cart_count": Cart(request).count,
    }
