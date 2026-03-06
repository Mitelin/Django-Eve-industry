# Postgres Advisory Locks

This project uses Postgres advisory locks to prevent overlapping sync and verification workers.

## Key schema

Lock keys are generated as two signed 32-bit integers from a stable SHA-256 digest.

Namespace material:
- global namespace: `DJANGO_ADVISORY_LOCK_NAMESPACE`
- logical namespace: `sync` or `verify`
- lock kind: for example `jobs`, `assets`, `wallet_journal`, `start_job`
- resource identifiers: corporation id, wallet id, character id, or other relevant scope parts

Examples:
- `sync:jobs:123`
- `sync:wallet_journal:123:7`
- `verify:start_job:123:90000001`

## Rules

- Use `build_sync_lock_key(...)` for ingestion workers.
- Use `build_verify_lock_key(...)` for verification workers.
- Acquire with `advisory_lock(...)` or `try_advisory_lock(...)`.
- Fail fast if the active DB connection is not PostgreSQL.
- Keep lock scope as narrow as possible while still preventing duplicate work.
