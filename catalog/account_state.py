import json

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _marker_path():
    return settings.EBAY_ACCOUNT_STATE_DIRECTORY / "ebay-account-closed.json"


def account_closure_notification_id():
    path = _marker_path()
    if not path.exists():
        return ""
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or set(value) != {"notification_id"}
        or not isinstance(value["notification_id"], str)
        or not value["notification_id"]
        or len(value["notification_id"]) > 128
    ):
        raise ImproperlyConfigured("The eBay account closure marker is malformed.")
    return value["notification_id"]


def record_account_closure(notification_id):
    directory = settings.EBAY_ACCOUNT_STATE_DIRECTORY
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _marker_path().write_text(
        json.dumps({"notification_id": notification_id}, separators=(",", ":")),
        encoding="utf-8",
    )
