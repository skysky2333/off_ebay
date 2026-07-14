from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Prefetch

from catalog.models import Product, ProductVariant
from orders.models import Order, OrderItem


_UNSET = object()


@dataclass(frozen=True)
class CartLine:
    line_id: str
    product: Product
    variant: ProductVariant | None
    quantity: int
    fixed_unit_price: Decimal | None = None

    @property
    def unit_price(self):
        if self.fixed_unit_price is not None:
            return self.fixed_unit_price
        return self.variant.direct_price if self.variant else self.product.direct_price

    @property
    def available_quantity(self):
        inventory = self.variant if self.line_id.startswith("variant:") else self.product
        owned_quantity = getattr(inventory, "_owned_checkout_quantity", 0)
        if owned_quantity:
            return owned_quantity
        if (
            not self.product.active
            or self.product.checkout_excluded
            or self.product.currency != "USD"
        ):
            return 0
        if self.line_id.startswith("variant:"):
            if (
                self.variant is None
                or not self.variant.active
                or not self.variant.purchasable
            ):
                return 0
            return self.variant.available_quantity
        if any(variant.active for variant in self.product.variants.all()):
            return 0
        return self.product.available_quantity

    @property
    def line_total(self):
        return self.unit_price * self.quantity


class Cart:
    session_key = "cart"
    checkout_key = "checkout_key"
    checkout_order_key = "checkout_order_id"
    checkout_form_key = "checkout_form_fingerprint"
    checkout_conflict_key = "checkout_conflicted"
    fixed_order_statuses = (
        Order.Status.PAYMENT_PROCESSING,
        Order.Status.CAPTURE_PENDING,
        Order.Status.FUNDING_RETRY,
    )

    def __init__(self, request):
        self.session = request.session
        self.read_only = request.method == "HEAD"

    @property
    def entries(self):
        return self.session.get(self.session_key, {})

    @property
    def count(self):
        return sum(entry["quantity"] for entry in self.entries.values())

    def fixed_order(self):
        order_id = self.session.get(self.checkout_order_key)
        if not order_id:
            return None
        return Order.objects.filter(
            pk=order_id,
            status__in=self.fixed_order_statuses,
        ).first()

    def lines(self, fixed_order=_UNSET):
        entries = self.entries
        exclude_order_id = self.session.get(self.checkout_order_key)
        if fixed_order is _UNSET:
            fixed_order = self.fixed_order()
        fixed_prices = {}
        if fixed_order:
            fixed_prices = {
                (
                    f"variant:{item.variant_id}"
                    if item.variant_id
                    else f"product:{item.product_id}"
                ): item.unit_price
                for item in OrderItem.objects.filter(order=fixed_order)
            }
        product_ids = {entry["product_id"] for entry in entries.values()}
        variant_ids = {
            entry["variant_id"] for entry in entries.values() if entry["variant_id"]
        }
        products = {
            product.pk: product
            for product in Product.objects.with_availability(
                exclude_order_id,
                fixed_order.pk if fixed_order else None,
            )
            .prefetch_related(
                "images",
                Prefetch(
                    "variants",
                    queryset=ProductVariant.objects.with_availability(
                        exclude_order_id,
                        fixed_order.pk if fixed_order else None,
                    ),
                ),
            )
            .filter(pk__in=product_ids)
        }
        variants = {
            variant.pk: variant
            for variant in ProductVariant.objects.with_availability(
                exclude_order_id,
                fixed_order.pk if fixed_order else None,
            ).filter(pk__in=variant_ids)
        }
        valid_entries = {
            line_id: entry
            for line_id, entry in entries.items()
            if entry["product_id"] in products
        }
        if len(valid_entries) != len(entries):
            self._save(valid_entries)
        lines = []
        for line_id, entry in valid_entries.items():
            product = products[entry["product_id"]]
            variant = variants.get(entry["variant_id"])
            lines.append(
                CartLine(
                    line_id,
                    product,
                    variant,
                    entry["quantity"],
                    fixed_prices.get(line_id),
                )
            )
        return lines

    def add(self, product, quantity, variant=None):
        if not product.is_purchasable:
            raise ValueError("This item is no longer available.")
        if variant:
            if (
                variant.product_id != product.pk
                or not variant.active
                or not variant.purchasable
            ):
                raise ValueError("This product option is unavailable.")
            available = variant.available_quantity
            line_id = f"variant:{variant.pk}"
        else:
            if any(variant.active for variant in product.variants.all()):
                raise ValueError("Choose a product option.")
            available = product.available_quantity
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
        if self.read_only:
            return
        self.session.pop(self.session_key, None)
        self.reset_checkout()

    def reset_checkout(self):
        if self.read_only:
            return
        self.session.pop(self.checkout_key, None)
        self.session.pop(self.checkout_order_key, None)
        self.session.pop(self.checkout_form_key, None)
        self.session.pop(self.checkout_conflict_key, None)
        self.session.modified = True

    def complete(self, order_id):
        if self.read_only:
            return
        self.session.pop(self.session_key, None)
        self.session.pop(self.checkout_key, None)
        self.session.pop(self.checkout_form_key, None)
        self.session.pop(self.checkout_conflict_key, None)
        self.session[self.checkout_order_key] = order_id
        self.session.modified = True

    def forget_order(self, order_id):
        if self.read_only:
            return
        if self.session.get(self.checkout_order_key) == order_id:
            self.session.pop(self.checkout_order_key)
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
        if self.read_only:
            return
        self.session[self.session_key] = entries
        self.session.pop(self.checkout_key, None)
        self.session.pop(self.checkout_order_key, None)
        self.session.pop(self.checkout_form_key, None)
        self.session.pop(self.checkout_conflict_key, None)
        self.session.modified = True
