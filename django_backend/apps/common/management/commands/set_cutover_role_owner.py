from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.common.ownership import get_required_cutover_roles, update_cutover_role_assignment


class Command(BaseCommand):
    help = "Assign or clear one cutover ownership role."

    def add_arguments(self, parser):
        parser.add_argument("role_name", choices=sorted(get_required_cutover_roles().keys()))
        parser.add_argument("assigned_to")
        parser.add_argument("--by", default="")
        parser.add_argument("--notes", default="")

    def handle(self, *args, **options):
        role_name = (options.get("role_name") or "").strip()
        assigned_to = (options.get("assigned_to") or "").strip()
        if not role_name:
            raise CommandError("role_name is required")

        assignment = update_cutover_role_assignment(
            role_name=role_name,
            assigned_to=assigned_to,
            changed_by=options["by"],
            notes=options["notes"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"role={assignment.role_name} owner={assignment.assigned_to or '-'}"
            )
        )