from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.common.signoffs import ensure_required_script_signoffs


class Command(BaseCommand):
    help = "Ensure required cutover script signoff records exist for the current inventory."

    def handle(self, *args, **options):
        signoffs = ensure_required_script_signoffs()
        self.stdout.write(
            self.style.SUCCESS(
                "ensured=" + ", ".join(signoff.script_name for signoff in signoffs)
            )
        )