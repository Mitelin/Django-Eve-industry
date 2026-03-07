from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from apps.common.preflight import generate_cutover_preflight_report


class Command(BaseCommand):
    help = "Generate an actionable assisted-cutover preflight report with current readiness, trend, and next steps."

    def add_arguments(self, parser):
        parser.add_argument("--persist", action="store_true")
        parser.add_argument("--trend-limit", type=int, default=7)

    def handle(self, *args, **options):
        payload = generate_cutover_preflight_report(
            persist=bool(options.get("persist")),
            trend_limit=int(options.get("trend_limit") or 7),
        )
        self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))