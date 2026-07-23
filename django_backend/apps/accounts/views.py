from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import transaction
from django.db.models import Prefetch
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.access import WORKER_GROUP_NAME, build_access_context, is_director, is_worker
from apps.accounts.models import Character


def _build_role_labels(user) -> list[str]:
    role_labels: list[str] = []
    if is_director(user):
        role_labels.append("director")
    if is_worker(user):
        role_labels.append("worker")
    if not role_labels:
        role_labels.append("account_only")
    return role_labels


def _build_account_context(user, *, error_message: str = "") -> dict[str, object]:
    return {
        **build_access_context(user),
        "characters": list(user.characters.order_by("-is_main", "name")),
        "role_labels": _build_role_labels(user),
        "is_director_user": is_director(user),
        "is_worker_user": is_worker(user),
        "error_message": error_message,
    }


def _parse_character_id(request: HttpRequest) -> int | None:
    raw_value = str(request.POST.get("characterId") or "").strip()
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _parse_json_body(request: HttpRequest) -> dict[str, object]:
    if "application/json" not in (request.content_type or ""):
        return {}
    if not request.body:
        return {}
    payload = request.body.decode("utf-8").strip()
    if not payload:
        return {}
    return json.loads(payload)


def _auth_required_json() -> JsonResponse:
    return JsonResponse({"error": "Authentication required"}, status=401)


def _forbidden_json(message: str) -> JsonResponse:
    return JsonResponse({"error": message}, status=403)


def _require_director_api_user(request: HttpRequest):
    if not request.user.is_authenticated:
        return _auth_required_json()
    if not is_director(request.user):
        return _forbidden_json("Director access is required")
    return request.user


def _serialize_character(character: Character) -> dict[str, object]:
    return {
        "id": character.id,
        "eveCharacterId": character.eve_character_id,
        "name": character.name,
        "corporationId": character.corporation_id,
        "allianceId": character.alliance_id,
        "isMain": character.is_main,
    }


def _serialize_account_summary(user, *, viewer_id: int | None = None) -> dict[str, object]:
    characters = list(user.characters.all().order_by("-is_main", "name"))
    group_names = {group.name for group in user.groups.all()}
    is_superuser = bool(user.is_superuser)
    managed_director = bool(user.is_staff and not user.is_superuser)
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.first_name or user.username,
        "isDirector": is_director(user),
        "isWorker": WORKER_GROUP_NAME in group_names or is_director(user),
        "hasWorkerGroup": WORKER_GROUP_NAME in group_names,
        "isStaff": bool(user.is_staff),
        "isSuperuser": is_superuser,
        "directorManagedByStaff": managed_director,
        "canEditDirector": not is_superuser and viewer_id != user.id,
        "canEditWorker": viewer_id != user.id,
        "linkedCharacterCount": len(characters),
        "mainCharacterName": next((character.name for character in characters if character.is_main), ""),
        "characters": [_serialize_character(character) for character in characters],
    }


@login_required
def account_home(request: HttpRequest) -> HttpResponse:
    user = request.user
    error_message = ""
    if request.method == "POST":
        action = str(request.POST.get("action") or "").strip()
        character_id = _parse_character_id(request)
        if action == "set_main_character":
            if character_id is None:
                error_message = "A valid character is required."
            else:
                character = get_object_or_404(user.characters, pk=character_id)
                with transaction.atomic():
                    user.characters.update(is_main=False)
                    character.is_main = True
                    character.save(update_fields=["is_main", "updated_at"])
                return redirect("account-home")
        elif action == "remove_character":
            if character_id is None:
                error_message = "A valid character is required."
            elif user.characters.count() <= 1:
                error_message = "At least one linked character must remain on the account."
            else:
                character = get_object_or_404(user.characters, pk=character_id)
                promoted_character = user.characters.exclude(pk=character.pk).order_by("name", "id").first()
                with transaction.atomic():
                    was_main = character.is_main
                    character.user = None
                    character.is_main = False
                    character.save(update_fields=["user", "is_main", "updated_at"])
                    if was_main and promoted_character is not None:
                        promoted_character.is_main = True
                        promoted_character.save(update_fields=["is_main", "updated_at"])
                return redirect("account-home")
        else:
            error_message = "Unsupported account action."

    return render(
        request,
        "auth/account.html",
        _build_account_context(user, error_message=error_message),
    )


@require_GET
def director_account_access_list(request: HttpRequest) -> JsonResponse:
    viewer = _require_director_api_user(request)
    if isinstance(viewer, JsonResponse):
        return viewer

    user_model = get_user_model()
    accounts = list(
        user_model.objects.order_by("username")
        .prefetch_related(
            "groups",
            Prefetch("characters", queryset=Character.objects.order_by("-is_main", "name")),
        )
    )
    return JsonResponse(
        {"accounts": [_serialize_account_summary(account, viewer_id=viewer.id) for account in accounts]}
    )


@require_POST
def update_account_roles(request: HttpRequest, user_id: int) -> JsonResponse:
    viewer = _require_director_api_user(request)
    if isinstance(viewer, JsonResponse):
        return viewer

    body = _parse_json_body(request)
    target = get_object_or_404(get_user_model(), pk=user_id)
    worker_group, _created = Group.objects.get_or_create(name=WORKER_GROUP_NAME)

    if "director" in body:
        director_enabled = bool(body.get("director"))
        if target.id == viewer.id and not director_enabled:
            return JsonResponse({"error": "You cannot remove your own director access."}, status=409)
        if target.is_superuser and not director_enabled:
            return JsonResponse({"error": "Superuser accounts are always directors."}, status=409)
        target.is_staff = director_enabled or target.is_superuser
        target.save(update_fields=["is_staff"])

    if "worker" in body:
        worker_enabled = bool(body.get("worker"))
        if worker_enabled:
            target.groups.add(worker_group)
        else:
            target.groups.remove(worker_group)

    target = (
        get_user_model()
        .objects.prefetch_related(
            "groups",
            Prefetch("characters", queryset=Character.objects.order_by("-is_main", "name")),
        )
        .get(pk=target.pk)
    )
    return JsonResponse({"account": _serialize_account_summary(target, viewer_id=viewer.id)})