from __future__ import annotations

from django.db import DEFAULT_DB_ALIAS, connections


class DatabaseConfigurationError(RuntimeError):
    pass


def get_connection(alias: str = DEFAULT_DB_ALIAS):
    return connections[alias]


def is_postgres(alias: str = DEFAULT_DB_ALIAS) -> bool:
    return get_connection(alias).vendor == "postgresql"


def require_postgres(alias: str = DEFAULT_DB_ALIAS):
    connection = get_connection(alias)
    if connection.vendor != "postgresql":
        raise DatabaseConfigurationError(
            f"Database alias '{alias}' uses '{connection.vendor}', but PostgreSQL is required"
        )
    return connection
