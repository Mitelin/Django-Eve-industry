from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from apps.industry_planner.shadow import generate_shadow_planner_report


class Command(BaseCommand):
    help = "Generate the current planner shadow/parity report against frozen and legacy scenarios."

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(generate_shadow_planner_report(), indent=2, sort_keys=True))