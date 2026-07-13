from decimal import Decimal

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class DefaultSettingsMigrationTests(TransactionTestCase):
    migrate_before = ("storefront", "0001_initial")
    migrate_default = ("storefront", "0002_default_settings")

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        return executor.loader.project_state([target]).apps

    def tearDown(self):
        self.migrate(self.migrate_default)
        super().tearDown()

    def test_noop_rollback_can_be_reapplied_without_replacing_settings(self):
        apps = self.migrate(self.migrate_before)
        StoreSettings = apps.get_model("storefront", "StoreSettings")
        StoreSettings.objects.update_or_create(
            pk=1,
            defaults={
                "flat_shipping_amount": Decimal("7.25"),
                "checkout_enabled": True,
            },
        )

        self.migrate(self.migrate_default)
        self.migrate(self.migrate_before)
        apps = self.migrate(self.migrate_default)
        StoreSettings = apps.get_model("storefront", "StoreSettings")
        settings = StoreSettings.objects.get()

        self.assertEqual(settings.flat_shipping_amount, Decimal("7.25"))
        self.assertTrue(settings.checkout_enabled)
