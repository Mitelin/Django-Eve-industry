from django.urls import path

from apps.workforce.views import director_jobs_screen, director_screen, jobs_board_screen, sde_admin_screen, ui_home, worker_screen


urlpatterns = [
    path("", ui_home, name="ui-home"),
    path("director/", director_screen, name="director-screen"),
    path("director/jobs/", director_jobs_screen, name="director-jobs-screen"),
    path("jobs/", jobs_board_screen, name="jobs-board-screen"),
    path("sde/", sde_admin_screen, name="sde-admin-screen"),
    path("worker/", worker_screen, name="worker-screen"),
]