from django.core.management.base import BaseCommand

from catalog.ebay import EbayTradingClient
from catalog.services import sync_catalog


class Command(BaseCommand):
    help = "Synchronize the catalog from the configured eBay seller account"

    def handle(self, *args, **options):
        with EbayTradingClient() as client:
            run = sync_catalog(client)
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {run.imported_count} of {run.indexed_count} active listings"
            )
        )
