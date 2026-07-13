from dataclasses import dataclass
from decimal import Decimal

from catalog.models import Product, ProductVariant


@dataclass(frozen=True)
class CartLine:
    line_id: str
    product: Product
    variant: ProductVariant | None
    quantity: int

    @property
    def unit_price(self):
        return self.variant.price if self.variant else self.product.price

    @property
    def available_quantity(self):
        if not self.product.active or self.product.checkout_excluded:
            return 0
        if self.line_id.startswith("variant:"):
            if (
                self.variant is None
                or not self.variant.active
                or not self.variant.purchasable
                or not self.variant.sku
            ):
                return 0
            return self.variant.quantity
        if any(variant.active for variant in self.product.variants.all()):
            return 0
        return self.product.quantity

    @property
    def line_total(self):
        return self.unit_price * self.quantity


class Cart:
    session_key = "cart"
    checkout_key = "checkout_key"
    checkout_order_key = "checkout_order_id"
    checkout_form_key = "checkout_form_fingerprint"

    def __init__(self, request):
        self.session = request.session

    @property
    def entries(self):
        return self.session.get(self.session_key, {})

    @property
    def count(self):
        return sum(entry["quantity"] for entry in self.entries.values())

    def lines(self):
        entries = self.entries
        product_ids = {entry["product_id"] for entry in entries.values()}
        variant_ids = {
            entry["variant_id"] for entry in entries.values() if entry["variant_id"]
        }
        products = {
            product.pk: product
            for product in Product.objects.prefetch_related("images", "variants").filter(
                pk__in=product_ids
            )
        }
        variants = {
            variant.pk: variant
            for variant in ProductVariant.objects.filter(pk__in=variant_ids)
        }
        lines = []
        for line_id, entry in entries.items():
            product = products[entry["product_id"]]
            variant = variants.get(entry["variant_id"])
            lines.append(CartLine(line_id, product, variant, entry["quantity"]))
        return lines

    def add(self, product, quantity, variant=None):
        if not product.is_purchasable:
            raise ValueError("This item is no longer available.")
        if variant:
            if (
                variant.product_id != product.pk
                or not variant.active
                or not variant.purchasable
                or not variant.sku
            ):
                raise ValueError("This product option is unavailable.")
            available = variant.quantity
            line_id = f"variant:{variant.pk}"
        else:
            if product.variants.filter(active=True).exists():
                raise ValueError("Choose a product option.")
            available = product.quantity
            line_id = f"product:{product.pk}"
        current = self.entries.get(line_id, {}).get("quantity", 0)
        self._set(line_id, product.pk, variant.pk if variant else None, current + quantity, available)

    def update(self, line_id, quantity):
        entry = self.entries[line_id]
        line = next(line for line in self.lines() if line.line_id == line_id)
        self._set(
            line_id,
            entry["product_id"],
            entry["variant_id"],
            quantity,
            line.available_quantity,
        )

    def remove(self, line_id):
        entries = self.entries.copy()
        entries.pop(line_id)
        self._save(entries)

    def clear(self):
        self.session.pop(self.session_key, None)
        self.session.pop(self.checkout_key, None)
        self.session.pop(self.checkout_order_key, None)
        self.session.pop(self.checkout_form_key, None)
        self.session.modified = True

    def _set(self, line_id, product_id, variant_id, quantity, available):
        if quantity < 1 or quantity > available:
            raise ValueError(f"Choose a quantity between 1 and {available}.")
        entries = self.entries.copy()
        entries[line_id] = {
            "product_id": product_id,
            "variant_id": variant_id,
            "quantity": quantity,
        }
        self._save(entries)

    def _save(self, entries):
        self.session[self.session_key] = entries
        self.session.pop(self.checkout_key, None)
        self.session.pop(self.checkout_order_key, None)
        self.session.pop(self.checkout_form_key, None)
        self.session.modified = True

    def totals(self, shipping):
        subtotal = sum((line.line_total for line in self.lines()), Decimal("0.00"))
        return subtotal, shipping, subtotal + shipping
