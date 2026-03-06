from django.contrib import admin

from apps.workforce.models import WorkEvent, WorkItem


admin.site.register(WorkItem)
admin.site.register(WorkEvent)
