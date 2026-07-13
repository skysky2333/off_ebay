import base64
import binascii
import json
import time
from urllib.parse import quote
from uuid import UUID

import httpx
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction

from .models import EbayPublicKeyLookupBudget


APPLICATION_SCOPE = "https://api.ebay.com/oauth/api_scope"
PUBLIC_KEY_CACHE_SECONDS = 3600
PUBLIC_KEY_LOOKUP_LIMIT = 20
SIGNATURE_FIELDS = {"alg", "kid", "signature", "digest"}
PUBLIC_KEY_BEGIN = "-----BEGIN PUBLIC KEY-----"
PUBLIC_KEY_END = "-----END PUBLIC KEY-----"


class EbayNotificationError(Exception):
    pass


class EbayNotificationMalformed(EbayNotificationError):
    pass


class EbayNotificationSignatureMismatch(EbayNotificationError):
    pass


class EbayNotificationSignatureMalformed(EbayNotificationMalformed):
    pass


class EbayNotificationProviderError(EbayNotificationError):
    pass


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate field: {key}")
        value[key] = item
    return value


def _signature_header(value):
    if not isinstance(value, str) or not value:
        raise EbayNotificationSignatureMalformed("X-EBAY-SIGNATURE is required.")
    try:
        encoded = base64.b64decode(value, validate=True)
        signature = json.loads(
            encoded.decode("ascii"), object_pairs_hook=_unique_object
        )
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        raise EbayNotificationSignatureMalformed(
            "X-EBAY-SIGNATURE is malformed."
        ) from error
    if not isinstance(signature, dict) or set(signature) != SIGNATURE_FIELDS:
        raise EbayNotificationSignatureMalformed(
            "X-EBAY-SIGNATURE has an invalid schema."
        )
    if signature["alg"] != "ecdsa" or signature["digest"] != "SHA1":
        raise EbayNotificationSignatureMalformed(
            "X-EBAY-SIGNATURE uses an unsupported algorithm."
        )
    if not isinstance(signature["kid"], str) or not signature["kid"]:
        raise EbayNotificationSignatureMalformed(
            "X-EBAY-SIGNATURE has an invalid key ID."
        )
    try:
        key_id = UUID(signature["kid"])
    except ValueError as error:
        raise EbayNotificationSignatureMalformed(
            "X-EBAY-SIGNATURE has an invalid key ID."
        ) from error
    if str(key_id) != signature["kid"].casefold():
        raise EbayNotificationSignatureMalformed(
            "X-EBAY-SIGNATURE has an invalid key ID."
        )
    if not isinstance(signature["signature"], str) or not signature["signature"]:
        raise EbayNotificationSignatureMalformed(
            "X-EBAY-SIGNATURE has an invalid signature."
        )
    try:
        signature_bytes = base64.b64decode(signature["signature"], validate=True)
        decode_dss_signature(signature_bytes)
    except (binascii.Error, ValueError) as error:
        raise EbayNotificationSignatureMalformed(
            "X-EBAY-SIGNATURE has an invalid signature."
        ) from error
    return signature["kid"], signature_bytes


def _configuration():
    values = {
        "EBAY_CLIENT_ID": settings.EBAY_CLIENT_ID,
        "EBAY_CLIENT_SECRET": settings.EBAY_CLIENT_SECRET,
        "EBAY_TOKEN_ENDPOINT": settings.EBAY_TOKEN_ENDPOINT,
        "EBAY_NOTIFICATION_PUBLIC_KEY_ENDPOINT": (
            settings.EBAY_NOTIFICATION_PUBLIC_KEY_ENDPOINT
        ),
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ImproperlyConfigured(f"Missing eBay settings: {', '.join(missing)}")
    return values


def _provider_json(response, resource):
    response.raise_for_status()
    try:
        value = response.json()
    except json.JSONDecodeError as error:
        raise EbayNotificationProviderError(
            f"eBay returned invalid JSON for {resource}."
        ) from error
    if not isinstance(value, dict):
        raise EbayNotificationProviderError(
            f"eBay returned an invalid {resource} response."
        )
    return value


def _access_token(http, configuration):
    response = http.post(
        configuration["EBAY_TOKEN_ENDPOINT"],
        auth=(
            configuration["EBAY_CLIENT_ID"],
            configuration["EBAY_CLIENT_SECRET"],
        ),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": APPLICATION_SCOPE},
    )
    token = _provider_json(response, "OAuth token").get("access_token")
    if not isinstance(token, str) or not token:
        raise EbayNotificationProviderError(
            "eBay OAuth response is missing access_token."
        )
    return token


def _public_key_document(http, configuration, key_id):
    endpoint = configuration["EBAY_NOTIFICATION_PUBLIC_KEY_ENDPOINT"]
    response = http.get(
        f"{endpoint}/{quote(key_id, safe='')}",
        headers={
            "Authorization": f"Bearer {_access_token(http, configuration)}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    document = _provider_json(response, "notification public key")
    if document.get("algorithm") != "ECDSA" or document.get("digest") != "SHA1":
        raise EbayNotificationProviderError(
            "eBay returned an unsupported notification public key."
        )
    key = document.get("key")
    if not isinstance(key, str) or not key:
        raise EbayNotificationProviderError(
            "eBay notification public key response is missing key."
        )
    return key


def _load_public_key(value):
    if not value.startswith(PUBLIC_KEY_BEGIN) or not value.endswith(
        PUBLIC_KEY_END
    ):
        raise EbayNotificationProviderError(
            "eBay returned a malformed notification public key."
        )
    body = (
        value.removeprefix(PUBLIC_KEY_BEGIN)
        .removesuffix(PUBLIC_KEY_END)
        .strip()
    )
    try:
        pem = f"{PUBLIC_KEY_BEGIN}\n{body}\n{PUBLIC_KEY_END}\n".encode("ascii")
        key = serialization.load_pem_public_key(pem)
    except (UnicodeEncodeError, ValueError, UnsupportedAlgorithm) as error:
        raise EbayNotificationProviderError(
            "eBay returned a malformed notification public key."
        ) from error
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise EbayNotificationProviderError(
            "eBay returned a non-ECDSA notification public key."
        )
    return key


@transaction.atomic
def _consume_public_key_lookup_budget():
    budget = EbayPublicKeyLookupBudget.objects.select_for_update().get(pk=1)
    window = int(time.time() // 60)
    if budget.window != window:
        budget.window = window
        budget.count = 0
    if budget.count >= PUBLIC_KEY_LOOKUP_LIMIT:
        raise EbayNotificationProviderError(
            "eBay notification public key lookup budget is exhausted."
        )
    budget.count += 1
    budget.save(update_fields=("window", "count"))


def _public_key(http, configuration, key_id):
    cache_key = f"ebay-notification-public-key:{key_id}"
    value = cache.get(cache_key)
    if value is None:
        _consume_public_key_lookup_budget()
        value = _public_key_document(http, configuration, key_id)
        _load_public_key(value)
        cache.set(cache_key, value, timeout=PUBLIC_KEY_CACHE_SECONDS)
    if not isinstance(value, str):
        raise EbayNotificationProviderError(
            "Cached eBay notification public key is malformed."
        )
    return _load_public_key(value)


def _message_bytes(message):
    if not isinstance(message, dict):
        raise EbayNotificationMalformed("eBay notification must be a JSON object.")
    try:
        return json.dumps(
            message,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise EbayNotificationMalformed("eBay notification is malformed.") from error


def marketplace_account_deletion_fields(message):
    if not isinstance(message, dict):
        raise EbayNotificationMalformed("eBay notification must be a JSON object.")
    metadata = message.get("metadata")
    notification = message.get("notification")
    if not isinstance(metadata, dict) or not isinstance(notification, dict):
        raise EbayNotificationMalformed("eBay notification has an invalid schema.")
    if (
        metadata.get("topic") != "MARKETPLACE_ACCOUNT_DELETION"
        or metadata.get("schemaVersion") != "1.0"
        or metadata.get("deprecated") is not False
    ):
        raise EbayNotificationMalformed("eBay notification has invalid metadata.")
    notification_id = notification.get("notificationId")
    event_date = notification.get("eventDate")
    publish_date = notification.get("publishDate")
    attempt_count = notification.get("publishAttemptCount")
    data = notification.get("data")
    if (
        not isinstance(notification_id, str)
        or not notification_id
        or len(notification_id) > 128
        or not isinstance(event_date, str)
        or not event_date
        or not isinstance(publish_date, str)
        or not publish_date
        or type(attempt_count) is not int
        or attempt_count < 1
        or not isinstance(data, dict)
    ):
        raise EbayNotificationMalformed("eBay notification has an invalid schema.")
    identifiers = tuple(data.get(name) for name in ("username", "userId", "eiasToken"))
    if any(not isinstance(value, str) or not value for value in identifiers):
        raise EbayNotificationMalformed("eBay notification has invalid account data.")
    return notification_id, *identifiers


def verify_ebay_notification(message, signature_header, http_client=None):
    key_id, signature = _signature_header(signature_header)
    configuration = _configuration()
    body = _message_bytes(message)
    if http_client is None:
        with httpx.Client(timeout=20) as http:
            public_key = _public_key(http, configuration, key_id)
    else:
        public_key = _public_key(http_client, configuration, key_id)
    try:
        public_key.verify(signature, body, ec.ECDSA(hashes.SHA1()))
    except InvalidSignature as error:
        raise EbayNotificationSignatureMismatch(
            "eBay notification signature does not match the payload."
        ) from error
    return True
