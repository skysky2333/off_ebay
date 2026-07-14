import os
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = os.environ.get("DJANGO_DEBUG") == "1"
ALLOWED_HOSTS = [host for host in os.environ["DJANGO_ALLOWED_HOSTS"].split(",") if host]
CSRF_TRUSTED_ORIGINS = [
    origin for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if origin
]
CSRF_FAILURE_VIEW = "storefront.views.csrf_failure"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "catalog",
    "orders",
    "storefront",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "storefront.middleware.NoIndexMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "storefront.context.store_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "request_exception": {"format": "{levelname} {name}", "style": "{"},
    },
    "handlers": {
        "request_exception": {
            "class": "logging.StreamHandler",
            "formatter": "request_exception",
            "level": "ERROR",
            "stream": "ext://sys.stderr",
        },
    },
    "loggers": {
        "django.request": {
            "handlers": ["request_exception"],
            "level": "ERROR",
            "propagate": False,
        },
    },
}

database_value = os.environ.get("DATABASE_URL", "")
postgres_values = {
    name: os.environ.get(name, "")
    for name in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_HOST")
}
if database_value:
    database_url = urlparse(database_value)
else:
    database_url = None
if database_url and database_url.scheme in {"postgres", "postgresql"}:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": database_url.path.lstrip("/"),
            "USER": unquote(database_url.username or ""),
            "PASSWORD": unquote(database_url.password or ""),
            "HOST": database_url.hostname,
            "PORT": database_url.port or 5432,
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
        }
    }
elif database_url and database_url.scheme == "sqlite":
    sqlite_path = unquote(database_url.path)
    if sqlite_path.startswith("//"):
        sqlite_name = Path(sqlite_path[1:])
    else:
        relative_path = sqlite_path.lstrip("/")
        sqlite_name = (
            relative_path if relative_path == ":memory:" else BASE_DIR / relative_path
        )
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": sqlite_name,
        }
    }
elif all(postgres_values.values()):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": postgres_values["POSTGRES_DB"],
            "USER": postgres_values["POSTGRES_USER"],
            "PASSWORD": postgres_values["POSTGRES_PASSWORD"],
            "HOST": postgres_values["POSTGRES_HOST"],
            "PORT": int(os.environ.get("POSTGRES_PORT", "5432")),
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
        }
    }
else:
    raise ImproperlyConfigured(
        "Set DATABASE_URL or all POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD, and POSTGRES_HOST values."
    )

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/New_York"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin-allow-popups"
SECURE_REFERRER_POLICY = "same-origin"
SECURE_HSTS_SECONDS = 0 if DEBUG else 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG
X_FRAME_OPTIONS = "DENY"

STORE_NAME = os.environ.get("STORE_NAME", "Off-Ebay")
DIRECT_DISCOUNT_PERCENT = Decimal(os.environ.get("DIRECT_DISCOUNT_PERCENT", "10"))
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "")
STORE_DOMAIN = os.environ.get("STORE_DOMAIN", "")

EBAY_TRADING_ENDPOINT = os.environ.get(
    "EBAY_TRADING_ENDPOINT", "https://api.ebay.com/ws/api.dll"
)
EBAY_MARKETING_ENDPOINT = os.environ.get(
    "EBAY_MARKETING_ENDPOINT", "https://api.ebay.com/sell/marketing/v1"
)
EBAY_TOKEN_ENDPOINT = os.environ.get(
    "EBAY_TOKEN_ENDPOINT", "https://api.ebay.com/identity/v1/oauth2/token"
)
EBAY_NOTIFICATION_PUBLIC_KEY_ENDPOINT = os.environ.get(
    "EBAY_NOTIFICATION_PUBLIC_KEY_ENDPOINT",
    "https://api.ebay.com/commerce/notification/v1/public_key",
)
EBAY_CLIENT_ID = os.environ.get("EBAY_CLIENT_ID", "")
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")
EBAY_REFRESH_TOKEN = os.environ.get("EBAY_REFRESH_TOKEN", "")
EBAY_COMPATIBILITY_LEVEL = os.environ.get("EBAY_COMPATIBILITY_LEVEL", "")
EBAY_SELLER_USERNAME = os.environ.get("EBAY_SELLER_USERNAME", "")
EBAY_MARKETPLACE_DELETION_VERIFICATION_TOKEN = os.environ.get(
    "EBAY_MARKETPLACE_DELETION_VERIFICATION_TOKEN", ""
)
EBAY_ACCOUNT_STATE_DIRECTORY = Path(
    os.environ.get(
        "EBAY_ACCOUNT_STATE_DIRECTORY", BASE_DIR / ".local" / "integration-state"
    )
)
EBAY_CHECKOUT_EXCLUDED_ITEMS = set(
    filter(None, os.environ.get("EBAY_CHECKOUT_EXCLUDED_ITEMS", "").split(","))
)

PAYPAL_API_BASE_URL = os.environ.get("PAYPAL_API_BASE_URL", "https://api-m.paypal.com")
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID", "")

ORDER_RESERVATION_MINUTES = int(os.environ.get("ORDER_RESERVATION_MINUTES", "30"))
EBAY_SYNC_SECONDS = int(os.environ.get("EBAY_SYNC_SECONDS", "900"))
