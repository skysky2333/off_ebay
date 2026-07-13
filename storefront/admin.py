from django.contrib import admin

from .models import StoreSettings


@admin.register(StoreSettings)
class StoreSettingsAdmin(admin.ModelAdmin):
    fieldsets = [
        (
            None,
            {
                "fields": [
                    "flat_shipping_amount",
                    "checkout_enabled",
                ]
            },
        )
    ]

    def get_form(self, request, obj=None, change=False, **kwargs):
        form = super().get_form(request, obj, change, **kwargs)
        if "flat_shipping_amount" in form.base_fields:
            form.base_fields["flat_shipping_amount"].label = "Flat shipping amount (USD)"
            form.base_fields["flat_shipping_amount"].help_text = (
                "Charged once per order, regardless of item count."
            )
        if "checkout_enabled" in form.base_fields:
            form.base_fields["checkout_enabled"].help_text = (
                "Checkout opens only when eBay, PayPal, and customer support are configured."
            )
        return form

    def has_add_permission(self, request):
        return super().has_add_permission(request) and not StoreSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
