from django.contrib import admin

from apps.common.models import CutoverRoleAssignment, CutoverRoleEvent, ReportSnapshot, ScriptSignoff, ScriptSignoffEvent


@admin.register(ReportSnapshot)
class ReportSnapshotAdmin(admin.ModelAdmin):
    list_display = ("snapshot_date", "report_name", "incident_count", "go_no_go", "updated_at")
    list_filter = ("report_name", "snapshot_date", "go_no_go")
    search_fields = ("report_name",)


@admin.register(ScriptSignoff)
class ScriptSignoffAdmin(admin.ModelAdmin):
    list_display = ("script_name", "status", "signed_off_by", "signed_off_at", "updated_at")
    list_filter = ("status",)
    search_fields = ("script_name", "signed_off_by", "notes")


@admin.register(ScriptSignoffEvent)
class ScriptSignoffEventAdmin(admin.ModelAdmin):
    list_display = ("signoff", "previous_status", "new_status", "changed_by", "effective_at")
    list_filter = ("new_status", "effective_at")
    search_fields = ("signoff__script_name", "changed_by", "notes")


@admin.register(CutoverRoleAssignment)
class CutoverRoleAssignmentAdmin(admin.ModelAdmin):
    list_display = ("role_name", "assigned_to", "assigned_at", "updated_at")
    search_fields = ("role_name", "assigned_to", "notes")


@admin.register(CutoverRoleEvent)
class CutoverRoleEventAdmin(admin.ModelAdmin):
    list_display = ("assignment", "previous_assigned_to", "new_assigned_to", "changed_by", "effective_at")
    list_filter = ("effective_at",)
    search_fields = ("assignment__role_name", "changed_by", "notes")