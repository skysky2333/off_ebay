from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    def handle(self, *args, **options):
        values = {
            "DJANGO_SECRET_KEY": settings.SECRET_KEY,
            "DJANGO_ALLOWED_HOSTS": settings.ALLOWED_HOSTS,
            "EBAY_CLIENT_ID": settings.EBAY_CLIENT_ID,
            "EBAY_CLIENT_SECRET": settings.EBAY_CLIENT_SECRET,
            "EBAY_REFRESH_TOKEN": settings.EBAY_REFRESH_TOKEN,
            "EBAY_COMPATIBILITY_LEVEL": settings.EBAY_COMPATIBILITY_LEVEL,
            "EBAY_SELLER_USERNAME": settings.EBAY_SELLER_USERNAME,
            "PAYPAL_CLIENT_ID": settings.PAYPAL_CLIENT_ID,
            "PAYPAL_CLIENT_SECRET": settings.PAYPAL_CLIENT_SECRET,
            "PAYPAL_WEBHOOK_ID": settings.PAYPAL_WEBHOOK_ID,
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise CommandError(f"Missing required settings: {', '.join(missing)}")
        if settings.DEBUG:
            raise CommandError("DJANGO_DEBUG must be disabled in production.")
        if settings.SECRET_KEY.startswith("replace-") or len(settings.SECRET_KEY) < 50:
            raise CommandError("DJANGO_SECRET_KEY must be a unique value of at least 50 characters.")
        if settings.EBAY_SELLER_USERNAME.casefold() != "fm2k244":
            raise CommandError("EBAY_SELLER_USERNAME must be fm2k244.")
        if settings.ORDER_RESERVATION_MINUTES <= 0 or settings.EBAY_SYNC_SECONDS <= 0:
            raise CommandError("Reservation and synchronization intervals must be positive.")
        if settings.DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
            raise CommandError("Production requires PostgreSQL.")
        self.stdout.write("Configuration is valid.")
