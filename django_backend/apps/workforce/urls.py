from django.urls import path

from apps.workforce.views import (
    claim_work_item,
    director_dashboard,
    director_project_detail,
    director_release_work_item,
    director_requeue_work_item,
    director_verify_work_item,
    dispatch_project,
    my_active,
    my_active_detail,
    project_progress,
    queue,
    release_work_item,
    temp_done_work_item,
    verify_batch,
)


urlpatterns = [
    path("work-items/claim", claim_work_item, name="work-items-claim"),
    path("work-items/<int:work_item_id>/director-release", director_release_work_item, name="work-items-director-release"),
    path("work-items/<int:work_item_id>/director-requeue", director_requeue_work_item, name="work-items-director-requeue"),
    path("work-items/<int:work_item_id>/director-verify", director_verify_work_item, name="work-items-director-verify"),
    path("work-items/<int:work_item_id>/temp-done", temp_done_work_item, name="work-items-temp-done"),
    path("work-items/<int:work_item_id>/release", release_work_item, name="work-items-release"),
    path("work-items/my-active", my_active, name="work-items-my-active"),
    path("work-items/my-active-detail", my_active_detail, name="work-items-my-active-detail"),
    path("work-items/queue", queue, name="work-items-queue"),
    path("work-items/verify-batch", verify_batch, name="work-items-verify-batch"),
    path("projects/<int:project_id>/dispatch", dispatch_project, name="projects-dispatch"),
    path("projects/<int:project_id>/progress", project_progress, name="projects-progress"),
    path("projects/<int:project_id>/director-detail", director_project_detail, name="projects-director-detail"),
    path("dashboard/director", director_dashboard, name="dashboard-director"),
]