from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from apps.common.shadow import generate_shadow_summary_report


class Command(BaseCommand):
    help = "Generate the current cross-slice shadow summary report."

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(generate_shadow_summary_report(), indent=2, sort_keys=True))