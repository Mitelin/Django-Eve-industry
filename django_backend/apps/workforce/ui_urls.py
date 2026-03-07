from django.urls import path

from apps.workforce.views import director_screen, ui_home, worker_screen


urlpatterns = [
    path("", ui_home, name="ui-home"),
    path("director/", director_screen, name="director-screen"),
    path("worker/", worker_screen, name="worker-screen"),
]