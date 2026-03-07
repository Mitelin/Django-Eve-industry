from django.urls import path

from apps.common.views import (
    cutover_pilot_readiness_report,
    cutover_pilot_trend_report,
    cutover_preflight_report,
    cutover_readiness_report,
    cutover_script_signoffs,
    cutover_trend_report,
    persist_report_history,
    report_snapshot_history,
    shadow_summary_report,
    sync_missing_cutover_script_signoffs,
    sync_missing_cutover_roles,
    update_cutover_role_owner,
    update_cutover_script_signoff,
)


urlpatterns = [
    path("reports/cutover/pilot-readiness", cutover_pilot_readiness_report, name="reports-cutover-pilot-readiness"),
    path("reports/cutover/pilot-trend", cutover_pilot_trend_report, name="reports-cutover-pilot-trend"),
    path("reports/cutover/preflight", cutover_preflight_report, name="reports-cutover-preflight"),
    path("reports/cutover/readiness", cutover_readiness_report, name="reports-cutover-readiness"),
    path("reports/cutover/script-signoffs", cutover_script_signoffs, name="reports-cutover-script-signoffs"),
    path("reports/cutover/script-signoffs/sync-missing", sync_missing_cutover_script_signoffs, name="reports-cutover-script-signoffs-sync-missing"),
    path("reports/cutover/script-signoffs/update", update_cutover_script_signoff, name="reports-cutover-script-signoffs-update"),
    path("reports/cutover/trend", cutover_trend_report, name="reports-cutover-trend"),
    path("reports/cutover/roles/sync-missing", sync_missing_cutover_roles, name="reports-cutover-roles-sync-missing"),
    path("reports/cutover/roles/update", update_cutover_role_owner, name="reports-cutover-roles-update"),
    path("reports/history", report_snapshot_history, name="reports-history"),
    path("reports/history/persist", persist_report_history, name="reports-history-persist"),
    path("reports/shadow/summary", shadow_summary_report, name="reports-shadow-summary"),
]