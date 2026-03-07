from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.common.ownership import ensure_cutover_role_assignments


class Command(BaseCommand):
    help = "Ensure required cutover role assignments exist for the current role inventory."

    def handle(self, *args, **options):
        assignments = ensure_cutover_role_assignments()
        self.stdout.write(
            self.style.SUCCESS(
                "ensured=" + ", ".join(assignment.role_name for assignment in assignments)
            )
        )