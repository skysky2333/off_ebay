import logging
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.http import HttpResponse
from django.test import Client, SimpleTestCase, override_settings
from django.urls import path


def _raise_production_exception(request, token):
    raise RuntimeError("production logging failure")


def _server_error(request):
    return HttpResponse(status=500)


urlpatterns = [path("orders/<uuid:token>/", _raise_production_exception)]
handler500 = _server_error


@override_settings(DEBUG=False, ROOT_URLCONF=__name__)
class ProductionExceptionLoggingTests(SimpleTestCase):
    def test_view_exception_logs_traceback_without_private_request_path(self):
        token = uuid4()
        request_path = f"/orders/{token}/"
        handler = next(
            handler
            for handler in logging.getLogger("django.request").handlers
            if handler.name == "request_exception"
        )
        output = StringIO()

        with patch.object(handler, "stream", output):
            response = Client(raise_request_exception=False).get(
                request_path, secure=True
            )

        logged_exception = output.getvalue()
        self.assertEqual(response.status_code, 500)
        self.assertIn("Traceback (most recent call last):", logged_exception)
        self.assertIn(
            "RuntimeError: production logging failure", logged_exception
        )
        self.assertNotIn(request_path, logged_exception)
        self.assertNotIn(str(token), logged_exception)
