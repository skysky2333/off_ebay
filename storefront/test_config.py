from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from .configuration import checkout_enabled, integration_environment
from .models import StoreSettings


class ValidateConfigTests(SimpleTestCase):
    def configuration(self, **overrides):
        values = {
            "SECRET_KEY": "a-unique-production-secret-key-with-more-than-fifty-characters",
            "DEBUG": False,
            "ALLOWED_HOSTS": ["shop.fm2k244.com", "localhost", "127.0.0.1"],
            "CSRF_TRUSTED_ORIGINS": ["https://shop.fm2k244.com"],
            "STORE_DOMAIN": "shop.fm2k244.com",
            "SUPPORT_EMAIL": "support@fm2k244.com",
            "EBAY_TRADING_ENDPOINT": "https://api.ebay.com/ws/api.dll",
            "EBAY_TOKEN_ENDPOINT": "https://api.ebay.com/identity/v1/oauth2/token",
            "EBAY_NOTIFICATION_PUBLIC_KEY_ENDPOINT": (
                "https://api.ebay.com/commerce/notification/v1/public_key"
            ),
            "EBAY_CLIENT_ID": "ebay-client",
            "EBAY_CLIENT_SECRET": "ebay-secret",
            "EBAY_REFRESH_TOKEN": "ebay-refresh",
            "EBAY_COMPATIBILITY_LEVEL": "1423",
            "EBAY_SELLER_USERNAME": "fm2k244",
            "EBAY_MARKETPLACE_DELETION_VERIFICATION_TOKEN": "a" * 48,
            "PAYPAL_CLIENT_ID": "paypal-client",
            "PAYPAL_CLIENT_SECRET": "paypal-secret",
            "PAYPAL_WEBHOOK_ID": "paypal-webhook",
            "PAYPAL_API_BASE_URL": "https://api-m.paypal.com",
            "DIRECT_DISCOUNT_PERCENT": Decimal("10"),
            "ORDER_RESERVATION_MINUTES": 30,
            "EBAY_SYNC_SECONDS": 900,
            "DATABASES": {
                "default": {
                    "ENGINE": "django.db.backends.postgresql",
                    "PASSWORD": "a-unique-database-password",
                }
            },
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def validate(self, web=False, worker=False, **overrides):
        output = StringIO()
        with patch(
            "storefront.management.commands.validate_config.settings",
            self.configuration(**overrides),
        ):
            call_command(
                "validate_config", web=web, worker=worker, stdout=output
            )
        return output.getvalue()

    def test_accepts_consistent_production_domain(self):
        self.assertIn("Configuration is valid.", self.validate())

    def test_accepts_the_configured_ebay_seller(self):
        self.assertIn(
            "Configuration is valid.",
            self.validate(EBAY_SELLER_USERNAME="another-seller"),
        )

    def test_rejects_invalid_direct_discount(self):
        for value in (Decimal("-0.01"), Decimal("100")):
            with self.subTest(value=value), self.assertRaisesMessage(
                CommandError,
                "DIRECT_DISCOUNT_PERCENT must be at least 0 and less than 100.",
            ):
                self.validate(DIRECT_DISCOUNT_PERCENT=value)

    def test_web_bootstrap_does_not_require_worker_integrations(self):
        self.assertIn(
            "Configuration is valid.",
            self.validate(
                web=True,
                EBAY_CLIENT_ID="",
                EBAY_CLIENT_SECRET="",
                EBAY_REFRESH_TOKEN="",
                EBAY_COMPATIBILITY_LEVEL="",
                PAYPAL_CLIENT_ID="",
                PAYPAL_CLIENT_SECRET="",
                PAYPAL_WEBHOOK_ID="",
            ),
        )

    @patch(
        "storefront.management.commands.validate_config.worker_account_closed",
        return_value=True,
    )
    def test_closed_worker_does_not_require_retired_integrations(self, closed):
        self.assertIn(
            "Configuration is valid.",
            self.validate(
                worker=True,
                EBAY_CLIENT_ID="",
                EBAY_CLIENT_SECRET="",
                EBAY_REFRESH_TOKEN="",
                EBAY_COMPATIBILITY_LEVEL="",
                PAYPAL_CLIENT_ID="",
                PAYPAL_CLIENT_SECRET="",
                PAYPAL_WEBHOOK_ID="",
            ),
        )

    def test_full_validation_requires_ebay_client_credentials(self):
        with self.assertRaisesMessage(
            CommandError,
            "Missing required settings: EBAY_CLIENT_ID, EBAY_CLIENT_SECRET",
        ):
            self.validate(EBAY_CLIENT_ID="", EBAY_CLIENT_SECRET="")

    @patch(
        "storefront.management.commands.validate_config.worker_account_closed",
        return_value=False,
    )
    def test_open_worker_requires_ebay_client_credentials(self, closed):
        with self.assertRaisesMessage(
            CommandError,
            "Missing required settings: EBAY_CLIENT_ID, EBAY_CLIENT_SECRET",
        ):
            self.validate(
                worker=True, EBAY_CLIENT_ID="", EBAY_CLIENT_SECRET=""
            )

    def test_rejects_low_uniqueness_secret(self):
        with self.assertRaisesMessage(
            CommandError,
            "DJANGO_SECRET_KEY must be at least 50 characters with at least 5 unique characters.",
        ):
            self.validate(SECRET_KEY="a" * 50)

    def test_rejects_django_insecure_secret(self):
        with self.assertRaisesMessage(
            CommandError,
            "DJANGO_SECRET_KEY must be at least 50 characters with at least 5 unique characters.",
        ):
            self.validate(
                SECRET_KEY="django-insecure-a-valid-looking-but-development-only-secret-key"
            )

    def test_requires_store_domain(self):
        with self.assertRaisesMessage(
            CommandError, "Missing required settings: STORE_DOMAIN"
        ):
            self.validate(STORE_DOMAIN="")

    def test_requires_support_email(self):
        with self.assertRaisesMessage(
            CommandError, "Missing required settings: SUPPORT_EMAIL"
        ):
            self.validate(SUPPORT_EMAIL="")

    def test_requires_valid_support_email(self):
        with self.assertRaisesMessage(
            CommandError, "SUPPORT_EMAIL must be a valid email address."
        ):
            self.validate(SUPPORT_EMAIL="not-an-email")

    def test_requires_exact_store_domain_in_allowed_hosts(self):
        with self.assertRaisesMessage(
            CommandError,
            "STORE_DOMAIN must be listed exactly in DJANGO_ALLOWED_HOSTS.",
        ):
            self.validate(ALLOWED_HOSTS=[".example.com", "localhost", "127.0.0.1"])

    def test_rejects_wildcard_allowed_host(self):
        with self.assertRaisesMessage(
            CommandError, "DJANGO_ALLOWED_HOSTS cannot contain a wildcard."
        ):
            self.validate(
                ALLOWED_HOSTS=["shop.fm2k244.com", "localhost", "127.0.0.1", "*"]
            )

    def test_rejects_reserved_production_domain(self):
        with self.assertRaisesMessage(
            CommandError, "STORE_DOMAIN must be your real production hostname."
        ):
            self.validate(
                STORE_DOMAIN="store.example.com",
                ALLOWED_HOSTS=["store.example.com"],
                CSRF_TRUSTED_ORIGINS=["https://store.example.com"],
            )

    def test_requires_exact_https_origin(self):
        with self.assertRaisesMessage(
            CommandError,
            "DJANGO_CSRF_TRUSTED_ORIGINS must include exactly https://shop.fm2k244.com.",
        ):
            self.validate(CSRF_TRUSTED_ORIGINS=["http://shop.fm2k244.com"])

    def test_production_requires_exact_live_provider_endpoints(self):
        with self.assertRaisesMessage(
            CommandError,
            "Production requires the exact live eBay and PayPal endpoints.",
        ):
            self.validate(PAYPAL_API_BASE_URL="https://api-m.sandbox.paypal.com")

    def test_requires_valid_ebay_deletion_verification_token(self):
        for token in ("short", "a" * 81, "a" * 31 + "."):
            with self.subTest(token=token), self.assertRaisesMessage(
                CommandError,
                "EBAY_MARKETPLACE_DELETION_VERIFICATION_TOKEN must be 32 to 80 "
                "characters using only letters, numbers, underscores, and hyphens.",
            ):
                self.validate(EBAY_MARKETPLACE_DELETION_VERIFICATION_TOKEN=token)

    def test_rejects_placeholder_database_password(self):
        with self.assertRaisesMessage(
            CommandError,
            "POSTGRES_PASSWORD must be a unique value of at least 16 characters.",
        ):
            self.validate(
                DATABASES={
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "PASSWORD": "replace-with-a-database-password",
                    }
                }
            )


@override_settings(
    EBAY_TRADING_ENDPOINT="https://api.sandbox.ebay.com/ws/api.dll",
    EBAY_TOKEN_ENDPOINT="https://api.sandbox.ebay.com/identity/v1/oauth2/token",
    EBAY_NOTIFICATION_PUBLIC_KEY_ENDPOINT=(
        "https://api.sandbox.ebay.com/commerce/notification/v1/public_key"
    ),
    EBAY_CLIENT_ID="ebay-client",
    EBAY_CLIENT_SECRET="ebay-secret",
    EBAY_REFRESH_TOKEN="ebay-refresh",
    EBAY_COMPATIBILITY_LEVEL="1423",
    PAYPAL_CLIENT_ID="paypal-client",
    PAYPAL_CLIENT_SECRET="paypal-secret",
    PAYPAL_WEBHOOK_ID="paypal-webhook",
    PAYPAL_API_BASE_URL="https://api-m.sandbox.paypal.com",
    SUPPORT_EMAIL="support@example.com",
)
class CheckoutConfigurationTests(TestCase):
    @override_settings(PAYPAL_WEBHOOK_ID="")
    def test_checkout_requires_paypal_webhook_id(self):
        store, _ = StoreSettings.objects.update_or_create(
            pk=1,
            defaults={
                "flat_shipping_amount": Decimal("0.00"),
                "checkout_enabled": True,
            },
        )

        self.assertFalse(checkout_enabled(store))

    @override_settings(SUPPORT_EMAIL="")
    def test_checkout_requires_support_email(self):
        store, _ = StoreSettings.objects.update_or_create(
            pk=1,
            defaults={
                "flat_shipping_amount": Decimal("0.00"),
                "checkout_enabled": True,
            },
        )

        self.assertFalse(checkout_enabled(store))

    def test_mixed_provider_environments_fail(self):
        configuration = SimpleNamespace(
            EBAY_TRADING_ENDPOINT="https://api.sandbox.ebay.com/ws/api.dll",
            EBAY_TOKEN_ENDPOINT=(
                "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
            ),
            EBAY_NOTIFICATION_PUBLIC_KEY_ENDPOINT=(
                "https://api.sandbox.ebay.com/commerce/notification/v1/public_key"
            ),
            PAYPAL_API_BASE_URL="https://api-m.paypal.com",
        )

        with self.assertRaisesMessage(
            ImproperlyConfigured,
            "eBay and PayPal endpoints must all use the same live or sandbox environment.",
        ):
            integration_environment(configuration)
