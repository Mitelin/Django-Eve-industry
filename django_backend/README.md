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
- First real corporation ingest services for assets, jobs, wallet journal, and wallet transactions snapshots
- Planner parity service baseline with legacy-compatible queue/material/job calculation flow
- Planner API contract baseline with frozen recursive golden scenario and `Project` plan persistence
- Direct parity tests against the legacy Python planner on shared fixture datasets
- Planner project API baseline for create, detail, and rebuild flows over `Project`, `ProjectTarget`, `PlanJob`, and `PlanMaterial`
- Planner project API now supports post-create update of project metadata and targets
- Planner frozen scenarios now cover recursive manufacturing, copyBPO, reaction, and `buildT1=false` ship-gate behavior
- Workforce MVP backend slice with dispatch, claim, temp-done, release, queue/progress queries, and START_JOB verification against ESI job snapshots
- Workforce safeguards: batch verification endpoint/command, stale jobs-sync guard, and retry-cap escalation path
- Project progress payload now includes jobs freshness and stale-warning fields for director/worker UI banners
- Director/worker UI payload baseline now includes /api/dashboard/director and /api/work-items/my-active-detail
- Director project drilldown now includes /api/projects/{id}/director-detail with targets, queue breakdown, bottlenecks, active workers, and recent events
- Internal pilot UI now exists at /, /director/, and /worker/ over the existing workforce/director API routes
- Shadow-mode planner report now exists at /api/reports/shadow/planner and via manage.py shadow_planner_report
- Cross-slice shadow summary now exists at /api/reports/shadow/summary and via manage.py shadow_summary_report for planner, sync, and workforce posture
- Cutover readiness now exists at /api/reports/cutover/readiness and via manage.py cutover_readiness_report, including mode, read-only guardrails, pilot users, ownership completeness, and go/no-go blockers
- Critical script sign-off tracking now exists at /api/reports/cutover/script-signoffs and is included in cutover readiness so compatibility-shim retirement can be governed explicitly
- Observation-window evidence can now be persisted via manage.py persist_report_snapshots and reviewed at /api/reports/history
- manage.py sync_script_signoffs bootstraps persistent rows for the required script inventory, and manage.py set_script_signoff updates one script to pending, validated, or blocked
- /api/reports/history supports reportName filtering such as /api/reports/history?reportName=cutover_readiness&limit=6, and the director flight deck now renders that cutover evidence window directly
- Explicit sign-off changes now append ScriptSignoffEvent audit rows, and cutover script-signoff payloads include recentEvents so the director screen can show who changed what and when
- Cutover ownership can now be managed persistently through CutoverRoleAssignment and CutoverRoleEvent rows; readiness uses DB assignments with env fallback, and the director screen shows current owners plus recent role changes
- /api/reports/cutover/trend derives readiness trend points from stored cutover snapshots so the director UI can show daily movement in assigned roles, validated sign-offs, blockers, and go/no-go state
- manage.py cutover_preflight aggregates live readiness, persisted evidence trend, snapshot deltas, and recommended next actions for assisted cutover checks; use --persist to refresh evidence before evaluating
- GET /api/reports/cutover/preflight exposes the same aggregated preflight payload for the director UI, including current governance counts, delta vs the latest stored snapshot, and recommended actions
- POST /api/reports/cutover/script-signoffs/update and POST /api/reports/cutover/roles/update now allow the director screen to update required sign-offs and cutover ownership directly from the browser with standard CSRF protection
- The director flight deck now includes Update Script Sign-Off and Assign Cutover Role action panels so the current preflight recommendations can be acted on without terminal commands
- Daily evidence persistence now also stores a `cutover_preflight` snapshot, and `manage.py cutover_preflight --persist` refreshes that stored preflight artifact in addition to readiness/shadow evidence
- POST /api/reports/history/persist now exposes the same evidence persistence flow over HTTP, and the director screen includes a Persist Evidence action so observation-window snapshots can be refreshed without shell access
- The director flight deck now renders a dedicated Preflight Snapshot History panel backed by `/api/reports/history?reportName=cutover_preflight&limit=6` so stored preflight posture and recommended actions remain visible after refreshes
- The cutover preflight payload now includes `changesVsStoredPreflight`, and the director screen renders that diff as a dedicated panel showing GO/NO-GO changes plus added or removed blockers and recommended actions versus the latest stored preflight
- The cutover preflight payload now also includes structured `recommendedActionItems`, allowing the director UI to turn actionable recommendations into quick actions for evidence persistence, role assignment, and script sign-off focus without parsing free-form text
- POST `/api/reports/cutover/roles/sync-missing` now bulk-fills missing cutover role owners from env/default role inventory with audit events, and the director screen exposes the same operation as both a dedicated button and a preflight quick action when env-backed missing roles exist
- POST `/api/reports/cutover/script-signoffs/sync-missing` now bootstraps missing required script sign-off rows from the configured inventory with pending audit events, and the director screen exposes the same operation as both a dedicated button and a preflight quick action when persistent sign-off rows are still missing
- The director screen now also exposes a composite `Bootstrap Governance` action that combines evidence persistence, missing role-owner sync, and missing sign-off sync into one refreshable workflow step; the same operation is exposed as a preflight quick action via `recommendedActionItems`
- Manual preflight actions now carry structured operator guidance (`guidanceTitle`, `guidanceSteps`, optional `targetSetting`), and the Director Flight Deck renders that guidance in an `Action Guidance` panel so unresolved manual blockers are still actionable from the same screen; sync and workforce recovery actions also jump directly to the Shadow Summary or the first problematic project detail instead of ending at a dead-end manual label
- Bottleneck work items now also include server-side `risk` metadata and are sorted by cleanup severity before aging, so `escalated` and `verify miss` items surface above routine `temp-done` or assigned backlog rows in the Director Flight Deck.
- Director project detail now shows a `risk.reason` line for each bottleneck item and exposes a `High Risk Only` filter so operators can narrow the panel to bad-tone cleanup cases without losing the underlying server-side severity ordering.
- Bottleneck risk metadata now also carries a stable `risk.code`, and project detail renders a dedicated `Needs Manual Attention` section sourced from the server-side high-risk subset so manual-cleanup and verify-miss cases are separated from routine retry backlog.
- High-risk bottleneck metadata now also includes `risk.nextAction`, and project detail renders a grouped `Manual Attention Summary` by `risk.code` so operators can see whether the dominant project issue needs `retry_verify`, `requeue`, or broader `manual_review` before drilling into individual items.
- Director project detail now maps actionable `risk.nextAction` values directly onto a `Run Recommended` button that reuses the existing item cleanup routes (`director-verify`, `director-requeue`, `director-release`), reducing operator guesswork when a recommended intervention is already executable.
- `manualAttentionSummary` now also reports `oldestAgeSeconds` plus the first actionable work-item id for each risk-code group, and the Director Flight Deck exposes that as a `Run First Recommended` shortcut so the oldest grouped blocker can be acted on directly from the summary.
- Non-executable recommendations such as `manual_review` and `wait_for_verify` now map to guidance-focused buttons in project detail and manual-attention summary, so operators are routed to explicit next-step instructions instead of being left with a dead-end recommendation label.
- Director cleanup and single-item verify events now also expose structured `source` provenance (`recommended_action`, `manual_action`, or future values) in payloads and project recent-event history, so the Director Flight Deck can distinguish guided actions from manual interventions without parsing summary text.
- The Director Flight Deck now also exposes quick Recent Events source filters (`All`, `Recommended`, `Manual`, `System`), and the Worker Command screen renders the same provenance labels in its recent-event list so audit context stays visible across both operational views.
- Director project detail now also exposes an `Event Provenance` summary sourced from the same recent-event feed, so operators can see the current mix of recommended, manual, and system actions before drilling into the full timeline.
- Shadow summary, cutover readiness, and cutover preflight snapshots now also carry workforce recent-event provenance counts, so recommended/manual/system action mix is visible both live and in stored preflight evidence history.
- Preflight diff against the latest stored baseline now also tracks workforce provenance deltas and flags when manual interventions grow faster than recommended-action handling, so assisted-cutover degradation is visible directly in the comparison panel.
- The same provenance degradation signal now also becomes an explicit manual preflight recommendation (`review_manual_intervention_growth`) with operator guidance, so rising manual cleanup load is actionable even before a human reads the diff details.
- Assisted and primary preflight now also compute an `effectiveGoNoGo` and `preflightBlockers`, so baseline-aware manual-dominance degradation can block rollout in preflight even when the live readiness payload is otherwise green.
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
   - optional: set `CUTOVER_REQUIRED_SCRIPT_SIGNOFFS` to the comma-separated Sheets scripts that must be signed off before primary mode or compatibility removal
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
- `apps.industry_planner.services.IndustryPlannerService`
- `apps.workforce.services.WorkforceService`
