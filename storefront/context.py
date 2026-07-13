from django.conf import settings

from .cart import Cart


def store_context(request):
    return {
        "store_name": settings.STORE_NAME,
        "support_email": settings.SUPPORT_EMAIL,
        "cart_count": Cart(request).count,
    }
