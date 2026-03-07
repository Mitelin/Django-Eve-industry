from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.common.history import persist_all_report_snapshots


class Command(BaseCommand):
    help = "Persist daily shadow, cutover, pilot, and preflight report snapshots for observation-window evidence."

    def handle(self, *args, **options):
        snapshots = persist_all_report_snapshots()
        self.stdout.write(
            self.style.SUCCESS(
                "stored=" + ", ".join(f"{snapshot.report_name}:{snapshot.snapshot_date.isoformat()}" for snapshot in snapshots)
            )
        )