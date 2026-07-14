from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings


HUNDRED = Decimal("100")
CENT = Decimal("0.01")


def _discount_percent(volume_discounts, quantity):
    percent = Decimal("0")
    minimum = 0
    for tier in volume_discounts:
        tier_minimum = int(tier["min_quantity"])
        if quantity >= tier_minimum and tier_minimum > minimum:
            minimum = tier_minimum
            percent = Decimal(tier["percent_off"])
    return percent


def ebay_price(source_price, quantity=1, volume_discounts=()):
    percent = _discount_percent(volume_discounts, quantity)
    factor = (HUNDRED - percent) / HUNDRED
    return (source_price * factor).quantize(CENT, rounding=ROUND_HALF_UP)


def direct_price(source_price, quantity=1, volume_discounts=()):
    source_price = ebay_price(source_price, quantity, volume_discounts)
    factor = (HUNDRED - settings.DIRECT_DISCOUNT_PERCENT) / HUNDRED
    return (source_price * factor).quantize(CENT, rounding=ROUND_HALF_UP)
