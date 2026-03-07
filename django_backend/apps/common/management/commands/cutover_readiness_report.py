from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from apps.common.cutover import generate_cutover_readiness_report


class Command(BaseCommand):
    help = "Generate the current cutover readiness report for assisted mode rollout."

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(generate_cutover_readiness_report(), indent=2, sort_keys=True))