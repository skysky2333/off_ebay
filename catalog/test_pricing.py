from decimal import Decimal

from django.test import SimpleTestCase, override_settings

from .pricing import direct_price


class DirectPriceTests(SimpleTestCase):
    def test_applies_ten_percent_discount_and_rounds_each_unit_to_cents(self):
        self.assertEqual(direct_price(Decimal("19.95")), Decimal("17.96"))
        self.assertEqual(direct_price(Decimal("0.01")), Decimal("0.01"))

    @override_settings(DIRECT_DISCOUNT_PERCENT=Decimal("12.5"))
    def test_uses_the_configured_discount(self):
        self.assertEqual(direct_price(Decimal("19.95")), Decimal("17.46"))
