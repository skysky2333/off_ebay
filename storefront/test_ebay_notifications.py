import json
from hashlib import sha256
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from catalog.notifications import EbayNotificationSignatureMismatch


MESSAGE = {
    "metadata": {
        "topic": "MARKETPLACE_ACCOUNT_DELETION",
        "schemaVersion": "1.0",
        "deprecated": False,
    },
    "notification": {
        "notificationId": "notification-1",
        "eventDate": "2026-07-13T12:00:00.000Z",
        "publishDate": "2026-07-13T12:00:01.000Z",
        "publishAttemptCount": 1,
        "data": {
            "username": "fm2k244",
            "userId": "immutable-id",
            "eiasToken": "legacy-token",
        },
    },
}


@override_settings(
    ALLOWED_HOSTS=("testserver", "attacker.example"),
    STORE_DOMAIN="store.skyy.uk",
    EBAY_MARKETPLACE_DELETION_VERIFICATION_TOKEN="v" * 48,
)
class EbayAccountDeletionEndpointTests(TestCase):
    def setUp(self):
        self.url = reverse("storefront:ebay_account_deletion")

    def test_answers_endpoint_challenge_with_exact_public_url(self):
        response = self.client.get(
            self.url,
            {"challenge_code": "challenge-123"},
            HTTP_HOST="attacker.example",
        )

        expected = sha256(
            (
                "challenge-123"
                + "v" * 48
                + "https://store.skyy.uk/webhooks/ebay/account-deletion/"
            ).encode()
        ).hexdigest()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), {"challengeResponse": expected})

    def test_rejects_missing_or_duplicate_challenge(self):
        for query in ("", "?challenge_code=one&challenge_code=two"):
            with self.subTest(query=query):
                response = self.client.get(f"{self.url}{query}")
                self.assertEqual(response.status_code, 400)

    @patch("storefront.views.process_ebay_account_closure")
    @patch("storefront.views.verify_ebay_notification", return_value=True)
    def test_verifies_and_processes_signed_notification(self, verify, process):
        response = Client(enforce_csrf_checks=True).post(
            self.url,
            data=json.dumps(MESSAGE),
            content_type="application/json",
            HTTP_X_EBAY_SIGNATURE="signed-header",
        )

        self.assertEqual(response.status_code, 204)
        verify.assert_called_once_with(MESSAGE, "signed-header")
        process.assert_called_once_with(
            "notification-1", "fm2k244", "immutable-id", "legacy-token"
        )

    @patch("storefront.views.process_ebay_account_closure")
    @patch(
        "storefront.views.verify_ebay_notification",
        side_effect=EbayNotificationSignatureMismatch("mismatch"),
    )
    def test_rejects_signature_mismatch_without_processing(self, verify, process):
        response = self.client.post(
            self.url,
            data=json.dumps(MESSAGE),
            content_type="application/json",
            HTTP_X_EBAY_SIGNATURE="invalid-header",
        )

        self.assertEqual(response.status_code, 412)
        process.assert_not_called()

    @patch("storefront.views.verify_ebay_notification")
    def test_rejects_invalid_payload_before_signature_lookup(self, verify):
        response = self.client.post(
            self.url,
            data=json.dumps({"metadata": {}}),
            content_type="application/json",
            HTTP_X_EBAY_SIGNATURE="signed-header",
        )

        self.assertEqual(response.status_code, 400)
        verify.assert_not_called()

    def test_rejects_non_json_content_type(self):
        response = self.client.post(self.url, data="{}", content_type="text/plain")

        self.assertEqual(response.status_code, 400)

    def test_rejects_missing_signature_as_failed_verification(self):
        response = self.client.post(
            self.url,
            data=json.dumps(MESSAGE),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 412)
