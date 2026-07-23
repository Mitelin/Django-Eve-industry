from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

from apps.accounts.views import account_home, director_account_access_list, update_account_roles


def healthcheck(_request):
    return JsonResponse({"status": "ok"})

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", healthcheck, name="healthcheck"),
    path("auth/", include("apps.eve_sso.urls")),
    path("account/", account_home, name="account-home"),
    path("api/accounts/access", director_account_access_list, name="accounts-access-list"),
    path("api/accounts/<int:user_id>/roles", update_account_roles, name="accounts-update-roles"),
    path("", include("apps.workforce.ui_urls")),
    path("api/", include("apps.common.urls")),
    path("api/", include("apps.industry_planner.urls")),
    path("api/", include("apps.workforce.urls")),
]
