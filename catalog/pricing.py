from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings


HUNDRED = Decimal("100")
CENT = Decimal("0.01")


def direct_price(ebay_price):
    factor = (HUNDRED - settings.DIRECT_DISCOUNT_PERCENT) / HUNDRED
    return (ebay_price * factor).quantize(CENT, rounding=ROUND_HALF_UP)
