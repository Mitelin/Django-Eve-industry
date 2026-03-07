from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from apps.common.pilot import generate_cutover_pilot_readiness_report


class Command(BaseCommand):
    help = "Generate the assisted-mode pilot readiness report and first-cycle evidence summary."

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(generate_cutover_pilot_readiness_report(), indent=2, sort_keys=True))