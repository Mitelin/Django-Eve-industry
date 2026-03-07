from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.common.models import ScriptSignoff
from apps.common.signoffs import update_script_signoff


class Command(BaseCommand):
    help = "Update the status of one cutover script signoff record."

    def add_arguments(self, parser):
        parser.add_argument("script_name")
        parser.add_argument("status", choices=[choice for choice, _label in ScriptSignoff.Status.choices])
        parser.add_argument("--by", default="")
        parser.add_argument("--notes", default="")

    def handle(self, *args, **options):
        script_name = (options.get("script_name") or "").strip()
        if not script_name:
            raise CommandError("script_name is required")

        signoff = update_script_signoff(
            script_name=script_name,
            status=options["status"],
            signed_off_by=options["by"],
            notes=options["notes"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"script={signoff.script_name} status={signoff.status} by={signoff.signed_off_by or '-'}"
            )
        )