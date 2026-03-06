from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from django.conf import settings


class AdvisoryLockError(RuntimeError):
    pass


@dataclass(frozen=True)
class AdvisoryLockKey:
    group_id: int
    resource_id: int


def _signed_int32(raw: bytes) -> int:
    return int.from_bytes(raw, byteorder="big", signed=True)


def build_advisory_lock_key(namespace: str, *parts: object) -> AdvisoryLockKey:
    material = ":".join(str(part) for part in (settings.ADVISORY_LOCK_NAMESPACE, namespace, *parts))
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return AdvisoryLockKey(
        group_id=_signed_int32(digest[:4]),
        resource_id=_signed_int32(digest[4:8]),
    )


def build_sync_lock_key(kind: str, corporation_id: int, *parts: object) -> AdvisoryLockKey:
    return build_advisory_lock_key("sync", kind, corporation_id, *parts)


def build_verify_lock_key(kind: str, corporation_id: int, *parts: object) -> AdvisoryLockKey:
    return build_advisory_lock_key("verify", kind, corporation_id, *parts)


def try_advisory_lock(connection, key: AdvisoryLockKey) -> bool:
    if connection.vendor != "postgresql":
        raise AdvisoryLockError("Postgres advisory locks require a PostgreSQL database connection")

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [key.group_id, key.resource_id])
        row = cursor.fetchone()

    return bool(row and row[0])


def advisory_unlock(connection, key: AdvisoryLockKey) -> bool:
    if connection.vendor != "postgresql":
        raise AdvisoryLockError("Postgres advisory locks require a PostgreSQL database connection")

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [key.group_id, key.resource_id])
        row = cursor.fetchone()

    return bool(row and row[0])


@contextmanager
def advisory_lock(connection, key: AdvisoryLockKey) -> Iterator[AdvisoryLockKey]:
    acquired = try_advisory_lock(connection, key)
    if not acquired:
        raise AdvisoryLockError(
            f"Could not acquire advisory lock for group={key.group_id} resource={key.resource_id}"
        )

    try:
        yield key
    finally:
        advisory_unlock(connection, key)
