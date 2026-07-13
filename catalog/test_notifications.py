import base64
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

import httpx
from django.core.cache import cache
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, override_settings

from .models import EbayPublicKeyLookupBudget
from .notifications import (
    EbayNotificationMalformed,
    EbayNotificationProviderError,
    EbayNotificationSignatureMismatch,
    _consume_public_key_lookup_budget,
    verify_ebay_notification,
)


SIGNATURE = (
    "eyJhbGciOiJlY2RzYSIsImtpZCI6Ijk5MzYyNjFhLTdkN2ItNDYyMS1hMGYxLTk2Y2Ni"
    "NDI4YWY0OSIsInNpZ25hdHVyZSI6Ik1FWUNJUUNmeGZJV3V4bVdjSUJRSjljNS9YN2lHRE"
    "pxczJSQ0dzQkVhQWppbnlycmZBSWhBSVY2d0djVGlCdVY1S0pVaWYyaG9reXJMK1E5c3NI"
    "a2FkK214Mm5FRTI1dyIsImRpZ2VzdCI6IlNIQTEifQ=="
)
PUBLIC_KEY = (
    "-----BEGIN PUBLIC KEY-----"
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEZhhxXKtR+TOvtDbgTPCkSof02qgBB7IsYO"
    "yf76ilExJ/upAa/vKIKheOoCyOpcLmi4t0b4uepb7LLjmMr90FUg=="
    "-----END PUBLIC KEY-----"
)
MESSAGE = {
    "metadata": {
        "topic": "MARKETPLACE_ACCOUNT_DELETION",
        "schemaVersion": "1.0",
        "deprecated": False,
    },
    "notification": {
        "notificationId": (
            "49feeaeb-4982-42d9-a377-9645b8479411_"
            "33f7e043-fed8-442b-9d44-791923bd9a6d"
        ),
        "eventDate": "2021-03-19T20:43:59.462Z",
        "publishDate": "2021-03-19T20:43:59.679Z",
        "publishAttemptCount": 1,
        "data": {
            "username": "test_user",
            "userId": "ma8vp1jySJC",
            "eiasToken": "nY+sHZ2PrBmdj6wVnY+sEZ2PrA2dj6wJnY+gAZGEpwmdj6x9nY+seQ==",
        },
    },
}


@override_settings(
    EBAY_CLIENT_ID="client-id",
    EBAY_CLIENT_SECRET="client-secret",
    EBAY_TOKEN_ENDPOINT="https://api.ebay.com/identity/v1/oauth2/token",
    EBAY_NOTIFICATION_PUBLIC_KEY_ENDPOINT=(
        "https://api.ebay.com/commerce/notification/v1/public_key"
    ),
)
class EbayNotificationVerifierTests(TestCase):
    def setUp(self):
        cache.clear()
        EbayPublicKeyLookupBudget.objects.update_or_create(
            pk=1, defaults={"window": 0, "count": 0}
        )
        self.requests = []

    def tearDown(self):
        cache.clear()

    def handler(self, request):
        self.requests.append(request)
        if request.url.path == "/identity/v1/oauth2/token":
            self.assertTrue(request.headers["authorization"].startswith("Basic "))
            form = httpx.QueryParams(request.content.decode())
            self.assertEqual(form["grant_type"], "client_credentials")
            self.assertEqual(form["scope"], "https://api.ebay.com/oauth/api_scope")
            return httpx.Response(200, json={"access_token": "application-token"})
        self.assertEqual(
            request.url.path,
            "/commerce/notification/v1/public_key/9936261a-7d7b-4621-a0f1-96ccb428af49",
        )
        self.assertEqual(request.headers["authorization"], "Bearer application-token")
        return httpx.Response(
            200,
            json={"key": PUBLIC_KEY, "algorithm": "ECDSA", "digest": "SHA1"},
        )

    def test_verifies_official_ebay_fixture_and_caches_public_key(self):
        with httpx.Client(transport=httpx.MockTransport(self.handler)) as http:
            self.assertTrue(verify_ebay_notification(MESSAGE, SIGNATURE, http))
            self.assertTrue(verify_ebay_notification(MESSAGE, SIGNATURE, http))

        self.assertEqual(len(self.requests), 2)
        self.assertEqual(EbayPublicKeyLookupBudget.objects.get(pk=1).count, 1)

    def test_rejects_signature_mismatch(self):
        message = json.loads(json.dumps(MESSAGE))
        message["notification"]["data"]["username"] = "different_user"

        with httpx.Client(transport=httpx.MockTransport(self.handler)) as http:
            with self.assertRaises(EbayNotificationSignatureMismatch):
                verify_ebay_notification(message, SIGNATURE, http)

    def test_rejects_malformed_signature_header(self):
        signature = json.loads(base64.b64decode(SIGNATURE))
        signature["alg"] = "ECDSA"
        header = base64.b64encode(
            json.dumps(signature, separators=(",", ":")).encode()
        ).decode()

        with self.assertRaises(EbayNotificationMalformed):
            verify_ebay_notification(MESSAGE, header)

        self.assertEqual(self.requests, [])

    def test_limits_unknown_public_key_lookups(self):
        signature = json.loads(base64.b64decode(SIGNATURE))
        signature["kid"] = "00000000-0000-4000-8000-000000000001"
        second_header = base64.b64encode(
            json.dumps(signature, separators=(",", ":")).encode()
        ).decode()

        with (
            patch("catalog.notifications.PUBLIC_KEY_LOOKUP_LIMIT", 1),
            httpx.Client(transport=httpx.MockTransport(self.handler)) as http,
        ):
            verify_ebay_notification(MESSAGE, SIGNATURE, http)
            cache.clear()
            with self.assertRaisesMessage(
                EbayNotificationProviderError,
                "eBay notification public key lookup budget is exhausted.",
            ):
                verify_ebay_notification(MESSAGE, second_header, http)

        self.assertEqual(len(self.requests), 2)
        self.assertEqual(EbayPublicKeyLookupBudget.objects.get(pk=1).count, 1)


@skipUnless(connection.vendor == "postgresql", "PostgreSQL concurrency test")
class EbayPublicKeyLookupBudgetConcurrencyTests(TransactionTestCase):
    def setUp(self):
        EbayPublicKeyLookupBudget.objects.update_or_create(
            pk=1, defaults={"window": 0, "count": 0}
        )

    def test_lookup_budget_is_shared_across_database_connections(self):
        barrier = Barrier(2)

        def consume():
            connections.close_all()
            barrier.wait()
            try:
                _consume_public_key_lookup_budget()
            except EbayNotificationProviderError:
                return False
            finally:
                connections.close_all()
            return True

        with (
            patch("catalog.notifications.PUBLIC_KEY_LOOKUP_LIMIT", 1),
            patch("catalog.notifications.time.time", return_value=60),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            results = list(executor.map(lambda _: consume(), range(2)))

        self.assertEqual(results.count(True), 1)
        budget = EbayPublicKeyLookupBudget.objects.get(pk=1)
        self.assertEqual((budget.window, budget.count), (1, 1))
