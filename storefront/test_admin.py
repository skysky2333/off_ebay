from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from .models import StoreSettings


class StoreSettingsAdminTests(TestCase):
    def test_missing_singleton_still_requires_add_permission(self):
        StoreSettings.objects.all().delete()
        operator = get_user_model().objects.create_user(
            "settings-operator",
            "settings@example.com",
            "admin-test-password",
            is_staff=True,
        )
        self.client.force_login(operator)
        url = reverse("admin:storefront_storesettings_add")

        self.assertEqual(self.client.get(url).status_code, 403)

        operator.user_permissions.add(
            Permission.objects.get(codename="add_storesettings")
        )
        self.client.force_login(get_user_model().objects.get(pk=operator.pk))

        self.assertEqual(self.client.get(url).status_code, 200)
