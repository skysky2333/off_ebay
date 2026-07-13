from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models


class StoreSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    flat_shipping_amount = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    checkout_enabled = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = "store settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        if self._state.adding and type(self).objects.filter(pk=1).exists():
            self._state.adding = False
        super().save(*args, **kwargs)

    def __str__(self):
        return "Store settings"
