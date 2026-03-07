from django.core.management.base import BaseCommand

from apps.workforce.services import WorkforceService


class Command(BaseCommand):
    help = "Run batch verification for TEMP_DONE work items"

    def handle(self, *args, **options):
        service = WorkforceService()
        result = service.verify_batch()
        self.stdout.write(
            self.style.SUCCESS(
                "verified={verified} requeued={requeued} stale={stale} escalated={escalated}".format(**result)
            )
        )