# Django Backend Bootstrap

## Current status

This folder contains the initial Django rewrite baseline for the corporation industry backend.

Implemented:
- Django project skeleton (`config`, `manage.py`)
- DRF and Postgres driver dependency baseline
- Environment-based settings with Postgres-ready configuration
- Advisory lock helper baseline for Postgres worker coordination
- EVE SSO token security service baseline with encrypted refresh-token storage
- Sync coordinator foundation with `SyncRun` status/freshness handling
- First real corporation ingest services for assets and jobs snapshots
- Initial domain apps:
  - `apps.accounts`
  - `apps.eve_sso`
  - `apps.corp_sync`
  - `apps.industry_planner`
  - `apps.workforce`
- First-pass schema models and migrations
- Admin registrations for the initial models
- `/health/` route for quick liveness checks

## Local run

1. Copy `.env.example` to `.env` for SQLite bootstrap, or `.env.postgres.example` to `.env` for local Postgres.
2. For local Postgres, start it with:
   - `docker compose -f docker-compose.postgres.yml up -d`
3. Fill or adjust DB variables in `.env`.
   - optional: set `DJANGO_ADVISORY_LOCK_NAMESPACE`
   - set `EVE_CLIENT_ID`, `EVE_CLIENT_SECRET`, `EVE_CORPORATION_ID`, `ESI_TOKEN_ENCRYPTION_KEY` when testing token flows
4. Run migrations:
   - `..\.venv\Scripts\python.exe manage.py migrate`
5. Verify Postgres advisory locks when using Postgres:
   - `..\.venv\Scripts\python.exe manage.py check_postgres_locks`
6. Create admin user:
   - `..\.venv\Scripts\python.exe manage.py createsuperuser`
7. Start server:
   - `..\.venv\Scripts\python.exe manage.py runserver`

## Delivery plan

1. Keep this project as the new Django system of record candidate.
2. Implement Postgres-first runtime settings for real local/staging DB.
3. Add token encryption and sync worker foundation.
4. Port planner parity logic behind frozen contract tests.
5. Build `START_JOB` work-item lifecycle and verifier.
6. Run Django in shadow mode before any assisted cutover.

Execution reference:
- See `kontext/SKELETON_TO_FULL_PROJECT_EXECUTION_PLAN.md` for the authoritative end-to-end implementation order.

## Rules for this rewrite

- Parity-first before behavior changes.
- No hard cut of legacy text errors until critical scripts are updated and validated.
- Worker overlap prevention uses Postgres advisory locks.

Supporting docs:
- `POSTGRES_LOCKS.md`

Current service baselines:
- `apps.eve_sso.services.EsiTokenService`
- `apps.corp_sync.services.SyncCoordinator`
- `apps.corp_sync.services.CorporationSyncService`
