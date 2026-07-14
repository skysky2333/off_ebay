from decimal import Decimal

from django.test import SimpleTestCase, override_settings

from .pricing import direct_price, ebay_price


class DirectPriceTests(SimpleTestCase):
    def test_applies_ten_percent_discount_and_rounds_each_unit_to_cents(self):
        self.assertEqual(direct_price(Decimal("19.95")), Decimal("17.96"))
        self.assertEqual(direct_price(Decimal("0.01")), Decimal("0.01"))

    @override_settings(DIRECT_DISCOUNT_PERCENT=Decimal("12.5"))
    def test_uses_the_configured_discount(self):
        self.assertEqual(direct_price(Decimal("19.95")), Decimal("17.46"))

    def test_applies_the_highest_qualifying_ebay_volume_tier_first(self):
        tiers = (
            {"min_quantity": 4, "percent_off": "10"},
            {"min_quantity": 2, "percent_off": "5"},
        )

        self.assertEqual(ebay_price(Decimal("20.00"), 1, tiers), Decimal("20.00"))
        self.assertEqual(ebay_price(Decimal("20.00"), 2, tiers), Decimal("19.00"))
        self.assertEqual(ebay_price(Decimal("20.00"), 4, tiers), Decimal("18.00"))
        self.assertEqual(direct_price(Decimal("20.00"), 4, tiers), Decimal("16.20"))
