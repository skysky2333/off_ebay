import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email

from catalog.account_state import account_closure_notification_id
from catalog.models import EbayAccountClosure
from storefront.configuration import PROVIDER_ENDPOINTS, provider_endpoints


def worker_account_closed():
    return bool(
        account_closure_notification_id() or EbayAccountClosure.objects.exists()
    )


class Command(BaseCommand):
    def add_arguments(self, parser):
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument("--web", action="store_true")
        modes.add_argument("--worker", action="store_true")

    def handle(self, *args, **options):
        database_password = settings.DATABASES["default"].get("PASSWORD", "")
        values = {
            "DJANGO_SECRET_KEY": settings.SECRET_KEY,
            "DJANGO_ALLOWED_HOSTS": settings.ALLOWED_HOSTS,
            "STORE_DOMAIN": settings.STORE_DOMAIN,
            "SUPPORT_EMAIL": settings.SUPPORT_EMAIL,
            "EBAY_SELLER_USERNAME": settings.EBAY_SELLER_USERNAME,
            "EBAY_MARKETPLACE_DELETION_VERIFICATION_TOKEN": (
                settings.EBAY_MARKETPLACE_DELETION_VERIFICATION_TOKEN
            ),
            "POSTGRES_PASSWORD": database_password,
        }
        if not options["web"] and not (
            options["worker"] and worker_account_closed()
        ):
            values.update(
                {
                    "EBAY_CLIENT_ID": settings.EBAY_CLIENT_ID,
                    "EBAY_CLIENT_SECRET": settings.EBAY_CLIENT_SECRET,
                    "EBAY_REFRESH_TOKEN": settings.EBAY_REFRESH_TOKEN,
                    "EBAY_COMPATIBILITY_LEVEL": settings.EBAY_COMPATIBILITY_LEVEL,
                    "PAYPAL_CLIENT_ID": settings.PAYPAL_CLIENT_ID,
                    "PAYPAL_CLIENT_SECRET": settings.PAYPAL_CLIENT_SECRET,
                    "PAYPAL_WEBHOOK_ID": settings.PAYPAL_WEBHOOK_ID,
                }
            )
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise CommandError(f"Missing required settings: {', '.join(missing)}")
        try:
            validate_email(settings.SUPPORT_EMAIL)
        except ValidationError as error:
            raise CommandError("SUPPORT_EMAIL must be a valid email address.") from error
        if settings.DEBUG:
            raise CommandError("DJANGO_DEBUG must be disabled in production.")
        if (
            settings.SECRET_KEY.startswith(("replace-", "django-insecure-"))
            or len(settings.SECRET_KEY) < 50
            or len(set(settings.SECRET_KEY)) < 5
        ):
            raise CommandError(
                "DJANGO_SECRET_KEY must be at least 50 characters with at least "
                "5 unique characters."
            )
        if settings.STORE_DOMAIN not in settings.ALLOWED_HOSTS:
            raise CommandError(
                "STORE_DOMAIN must be listed exactly in DJANGO_ALLOWED_HOSTS."
            )
        if "*" in settings.ALLOWED_HOSTS:
            raise CommandError("DJANGO_ALLOWED_HOSTS cannot contain a wildcard.")
        reserved_domains = ("example.com", "example.net", "example.org")
        if any(
            settings.STORE_DOMAIN == domain
            or settings.STORE_DOMAIN.endswith(f".{domain}")
            for domain in reserved_domains
        ):
            raise CommandError("STORE_DOMAIN must be your real production hostname.")
        origin = f"https://{settings.STORE_DOMAIN}"
        if origin not in settings.CSRF_TRUSTED_ORIGINS:
            raise CommandError(
                f"DJANGO_CSRF_TRUSTED_ORIGINS must include exactly {origin}."
            )
        if not re.fullmatch(
            r"[A-Za-z0-9_-]{32,80}",
            settings.EBAY_MARKETPLACE_DELETION_VERIFICATION_TOKEN,
        ):
            raise CommandError(
                "EBAY_MARKETPLACE_DELETION_VERIFICATION_TOKEN must be 32 to 80 "
                "characters using only letters, numbers, underscores, and hyphens."
            )
        if provider_endpoints(settings) != PROVIDER_ENDPOINTS["live"]:
            raise CommandError("Production requires the exact live eBay and PayPal endpoints.")
        if database_password.startswith("replace-") or len(database_password) < 16:
            raise CommandError(
                "POSTGRES_PASSWORD must be a unique value of at least 16 characters."
            )
        if settings.ORDER_RESERVATION_MINUTES <= 0 or settings.EBAY_SYNC_SECONDS <= 0:
            raise CommandError("Reservation and synchronization intervals must be positive.")
        if not 0 <= settings.DIRECT_DISCOUNT_PERCENT < 100:
            raise CommandError(
                "DIRECT_DISCOUNT_PERCENT must be at least 0 and less than 100."
            )
        if settings.DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
            raise CommandError("Production requires PostgreSQL.")
        self.stdout.write("Configuration is valid.")
