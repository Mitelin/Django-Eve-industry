from __future__ import annotations

import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from apps.accounts.access import build_access_context, default_redirect_for_user
from apps.eve_sso.services import EsiTokenService, TokenError


_OAUTH_SESSION_KEY = "eve_sso_oauth"


def eve_login_page(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect(default_redirect_for_user(request.user))
    return render(
        request,
        "auth/login.html",
        {
            **build_access_context(request.user),
            "eve_login_ready": bool(settings.EVE_CLIENT_ID),
        },
    )


def eve_login_start(request: HttpRequest) -> HttpResponse:
    if not settings.EVE_CLIENT_ID:
        return render(
            request,
            "auth/login.html",
            {
                **build_access_context(request.user),
                "error_message": "EVE SSO is not configured yet.",
            },
            status=503,
        )

    mode = "link" if request.GET.get("mode") == "link" and request.user.is_authenticated else "login"
    state = secrets.token_urlsafe(24)
    next_url = request.GET.get("next") or (reverse("account-home") if mode == "link" else reverse("ui-home"))
    request.session[_OAUTH_SESSION_KEY] = {"state": state, "mode": mode, "next": next_url}

    service = EsiTokenService()
    try:
        authorization_url = service.build_authorization_url(
            redirect_uri=request.build_absolute_uri(reverse("eve-login-callback")),
            state=state,
            scopes=settings.EVE_LOGIN_SCOPES,
        )
    finally:
        service.close()
    return redirect(authorization_url)


def eve_callback(request: HttpRequest) -> HttpResponse:
    oauth_state = dict(request.session.get(_OAUTH_SESSION_KEY) or {})
    if not oauth_state or oauth_state.get("state") != request.GET.get("state"):
        return render(
            request,
            "auth/login.html",
            {
                **build_access_context(request.user),
                "error_message": "EVE login state is invalid or expired.",
            },
            status=400,
        )

    request.session.pop(_OAUTH_SESSION_KEY, None)
    if request.GET.get("error"):
        return render(
            request,
            "auth/login.html",
            {
                **build_access_context(request.user),
                "error_message": f"EVE login failed: {request.GET.get('error')}",
            },
            status=400,
        )

    code = str(request.GET.get("code") or "").strip()
    if not code:
        return render(
            request,
            "auth/login.html",
            {
                **build_access_context(request.user),
                "error_message": "EVE login callback is missing the authorization code.",
            },
            status=400,
        )

    service = EsiTokenService()
    try:
        token_payload = service.exchange_authorization_code(
            code=code,
            redirect_uri=request.build_absolute_uri(reverse("eve-login-callback")),
        )
        token = service.upsert_token_response(token_payload, purpose="full")
        access_token = service.get_valid_access_token(token)
        membership = service.fetch_corp_membership(access_token)
    except TokenError as exc:
        service.close()
        return render(
            request,
            "auth/login.html",
            {
                **build_access_context(request.user),
                "error_message": str(exc),
            },
            status=400,
        )
    finally:
        service.close()

    character = token.owner_character
    character.name = character.name or f"Character {membership.character_id}"
    character.corporation_id = membership.corporation_id
    character.alliance_id = membership.alliance_id

    mode = oauth_state.get("mode")
    if mode == "link":
        if not request.user.is_authenticated:
            return render(
                request,
                "auth/login.html",
                {
                    **build_access_context(request.user),
                    "error_message": "Linking another character requires an active account session.",
                },
                status=403,
            )
        if character.user_id and character.user_id != request.user.id:
            return render(
                request,
                "auth/account.html",
                {
                    **build_access_context(request.user),
                    "characters": list(request.user.characters.order_by("-is_main", "name")),
                    "role_labels": ["director"] if request.user.is_staff else ["worker"] if request.user.groups.filter(name="worker").exists() else ["account_only"],
                    "is_director_user": request.user.is_staff or request.user.is_superuser,
                    "is_worker_user": request.user.is_staff or request.user.is_superuser or request.user.groups.filter(name="worker").exists(),
                    "error_message": "This character is already linked to another account.",
                },
                status=409,
            )
        character.user = request.user
        if not request.user.characters.exclude(pk=character.pk).filter(is_main=True).exists():
            character.is_main = True
        character.save(update_fields=["user", "name", "corporation_id", "alliance_id", "is_main", "updated_at"])
        messages.success(request, f"Character {character.name} was linked to your account.")
        return redirect(oauth_state.get("next") or reverse("account-home"))

    user = character.user or _create_account_user(character.name, character.eve_character_id)
    character.user = user
    if not user.characters.exclude(pk=character.pk).filter(is_main=True).exists():
        character.is_main = True
    character.save(update_fields=["user", "name", "corporation_id", "alliance_id", "is_main", "updated_at"])
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return redirect(oauth_state.get("next") or default_redirect_for_user(user))


@login_required
def eve_logout(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect("eve-login-page")


def _create_account_user(character_name: str, character_id: int):
    user_model = get_user_model()
    user = user_model.objects.create(username=f"eve-{character_id}", first_name=character_name)
    user.set_unusable_password()
    user.save(update_fields=["password", "updated_at"] if hasattr(user, "updated_at") else ["password"])
    return user