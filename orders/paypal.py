import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class PayPalClient:
    def __init__(
        self,
        client_id=None,
        client_secret=None,
        webhook_id=None,
        base_url=None,
        http_client=None,
    ):
        self.client_id = client_id if client_id is not None else settings.PAYPAL_CLIENT_ID
        self.client_secret = (
            client_secret if client_secret is not None else settings.PAYPAL_CLIENT_SECRET
        )
        self.webhook_id = (
            webhook_id if webhook_id is not None else settings.PAYPAL_WEBHOOK_ID
        )
        self.base_url = (
            base_url if base_url is not None else settings.PAYPAL_API_BASE_URL
        ).rstrip("/")
        if not self.client_id or not self.client_secret:
            raise ImproperlyConfigured("PayPal client credentials are required.")
        self.http = http_client or httpx.Client(timeout=20)
        self._owns_http = http_client is None

    def close(self):
        if self._owns_http:
            self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def access_token(self):
        response = self.http.post(
            f"{self.base_url}/v1/oauth2/token",
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        return response.json()["access_token"]

    def _headers(self, request_id=None):
        headers = {
            "Authorization": f"Bearer {self.access_token()}",
            "Content-Type": "application/json",
        }
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        return headers

    def create_order(self, payload, request_id):
        response = self.http.post(
            f"{self.base_url}/v2/checkout/orders",
            headers={**self._headers(request_id), "Prefer": "return=representation"},
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    def get_order(self, paypal_order_id):
        response = self.http.get(
            f"{self.base_url}/v2/checkout/orders/{paypal_order_id}",
            headers=self._headers(),
        )
        response.raise_for_status()
        return response.json()

    def capture_order(self, paypal_order_id, request_id):
        response = self.http.post(
            f"{self.base_url}/v2/checkout/orders/{paypal_order_id}/capture",
            headers={**self._headers(request_id), "Prefer": "return=representation"},
            json={},
        )
        response.raise_for_status()
        return response.json()

    def refund_capture(self, capture_id, amount, currency, invoice_id, request_id):
        response = self.http.post(
            f"{self.base_url}/v2/payments/captures/{capture_id}/refund",
            headers={**self._headers(request_id), "Prefer": "return=representation"},
            json={
                "amount": {"value": str(amount), "currency_code": currency},
                "invoice_id": invoice_id,
            },
        )
        response.raise_for_status()
        return response.json()

    def verify_webhook_signature(self, headers, event):
        if not self.webhook_id:
            raise ImproperlyConfigured("PAYPAL_WEBHOOK_ID is required.")
        payload = {
            "auth_algo": headers["PAYPAL-AUTH-ALGO"],
            "cert_url": headers["PAYPAL-CERT-URL"],
            "transmission_id": headers["PAYPAL-TRANSMISSION-ID"],
            "transmission_sig": headers["PAYPAL-TRANSMISSION-SIG"],
            "transmission_time": headers["PAYPAL-TRANSMISSION-TIME"],
            "webhook_id": self.webhook_id,
            "webhook_event": event,
        }
        response = self.http.post(
            f"{self.base_url}/v1/notifications/verify-webhook-signature",
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()
        return response.json()["verification_status"] == "SUCCESS"
