from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from catalog.models import SyncRun


class Command(BaseCommand):
    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(seconds=settings.EBAY_SYNC_SECONDS * 2)
        if not SyncRun.objects.filter(
            status=SyncRun.Status.SUCCEEDED, completed_at__gte=cutoff
        ).exists():
            raise CommandError("No recent successful eBay synchronization.")
        self.stdout.write("Worker is healthy.")
