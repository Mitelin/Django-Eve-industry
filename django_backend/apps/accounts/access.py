from __future__ import annotations

from django.contrib.auth.models import AbstractBaseUser
from django.urls import reverse


WORKER_GROUP_NAME = "worker"


def is_director(user: AbstractBaseUser | None) -> bool:
    return bool(user and user.is_authenticated and (user.is_staff or user.is_superuser))


def is_worker(user: AbstractBaseUser | None) -> bool:
    return bool(user and user.is_authenticated and (is_director(user) or user.groups.filter(name=WORKER_GROUP_NAME).exists()))


def default_redirect_for_user(user: AbstractBaseUser) -> str:
    if is_director(user):
        return reverse("director-screen")
    if is_worker(user):
        return reverse("worker-screen")
    return reverse("account-home")


def build_access_context(user: AbstractBaseUser | None) -> dict[str, bool]:
    director = is_director(user)
    worker = is_worker(user)
    return {
        "can_view_director": director,
        "can_view_sde": director,
        "can_view_worker": worker,
        "can_manage_roles": director,
    }