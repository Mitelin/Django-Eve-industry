from django.contrib import admin

from apps.industry_planner.models import PlanJob, PlanMaterial, Project, ProjectTarget


admin.site.register(Project)
admin.site.register(ProjectTarget)
admin.site.register(PlanJob)
admin.site.register(PlanMaterial)
