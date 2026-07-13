from django.core.management.base import BaseCommand

from catalog.services import enforce_recorded_ebay_account_closure


class Command(BaseCommand):
    def handle(self, *args, **options):
        closure = enforce_recorded_ebay_account_closure()
        if closure:
            self.stdout.write("Recorded eBay account closure enforced.")
