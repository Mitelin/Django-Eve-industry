from django.urls import path

from apps.eve_sso.views import eve_callback, eve_login_page, eve_login_start, eve_logout


urlpatterns = [
    path("login/", eve_login_page, name="eve-login-page"),
    path("start/", eve_login_start, name="eve-login-start"),
    path("callback/", eve_callback, name="eve-login-callback"),
    path("logout/", eve_logout, name="eve-logout"),
]