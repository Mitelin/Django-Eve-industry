from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.common.db import DatabaseConfigurationError, require_postgres
from apps.common.locks import AdvisoryLockError, advisory_lock, build_advisory_lock_key


class Command(BaseCommand):
    help = "Verify that the configured database supports PostgreSQL advisory locks"

    def handle(self, *args, **options):
        try:
            connection = require_postgres()
            key = build_advisory_lock_key("healthcheck", "default")
            with advisory_lock(connection, key):
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Advisory lock OK for group={key.group_id} resource={key.resource_id}"
                    )
                )
        except (DatabaseConfigurationError, AdvisoryLockError) as exc:
            raise CommandError(str(exc)) from exc
