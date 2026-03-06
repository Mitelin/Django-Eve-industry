from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from cryptography.fernet import Fernet
from django.conf import settings
from django.utils import timezone

from apps.accounts.models import Character
from apps.eve_sso.models import EsiToken


class TokenError(RuntimeError):
    pass


class TokenValidationError(TokenError):
    pass


class TokenRefreshError(TokenError):
    pass


@dataclass(frozen=True)
class ParsedAccessToken:
    character_id: int
    character_name: str | None
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class CorpMembership:
    character_id: int
    corporation_id: int
    alliance_id: int | None


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def parse_access_token(access_token: str) -> ParsedAccessToken:
    try:
        payload = json.loads(_b64url_decode(access_token.split(".")[1]).decode("utf-8"))
        sub = str(payload.get("sub", ""))
        character_id = int(sub.split(":")[2])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TokenValidationError("Invalid access token payload") from exc

    scopes = payload.get("scp") or []
    if isinstance(scopes, str):
        scopes = scopes.split()

    return ParsedAccessToken(
        character_id=character_id,
        character_name=payload.get("name"),
        scopes=tuple(str(scope) for scope in scopes),
    )


def _build_fernet(secret: str) -> Fernet:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_refresh_token(refresh_token: str) -> str:
    fernet = _build_fernet(settings.ESI_TOKEN_ENCRYPTION_KEY)
    return fernet.encrypt(refresh_token.encode("utf-8")).decode("utf-8")


def decrypt_refresh_token(refresh_token_enc: str) -> str:
    fernet = _build_fernet(settings.ESI_TOKEN_ENCRYPTION_KEY)
    return fernet.decrypt(refresh_token_enc.encode("utf-8")).decode("utf-8")


class EsiTokenService:
    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(timeout=60.0)

    def close(self) -> None:
        self._client.close()

    def upsert_token_response(self, token_payload: dict[str, Any], purpose: str = "full") -> EsiToken:
        parsed = parse_access_token(token_payload["access_token"])
        expires_in = int(token_payload.get("expires_in", 0) or 0)
        scope_raw = token_payload.get("scope") or parsed.scopes
        if isinstance(scope_raw, str):
            scopes = scope_raw
        else:
            scopes = " ".join(str(scope) for scope in scope_raw)

        character, _ = Character.objects.update_or_create(
            eve_character_id=parsed.character_id,
            defaults={
                "name": parsed.character_name or f"Character {parsed.character_id}",
                "corporation_id": 0,
            },
        )

        token, _ = EsiToken.objects.update_or_create(
            owner_character=character,
            purpose=purpose,
            defaults={
                "refresh_token_enc": encrypt_refresh_token(token_payload["refresh_token"]),
                "access_token": token_payload.get("access_token", ""),
                "expires_at": timezone.now() + timedelta(seconds=expires_in),
                "scopes": scopes,
                "last_refresh_error": "",
            },
        )
        return token

    def get_valid_access_token(self, token: EsiToken) -> str:
        if token.expires_at >= timezone.now() + timedelta(minutes=5) and token.access_token:
            return token.access_token
        refreshed = self.refresh_access_token(token)
        return refreshed.access_token

    def refresh_access_token(self, token: EsiToken) -> EsiToken:
        if not settings.EVE_CLIENT_ID or not settings.EVE_CLIENT_SECRET:
            raise TokenRefreshError("EVE_CLIENT_ID / EVE_CLIENT_SECRET missing")

        auth = base64.b64encode(
            f"{settings.EVE_CLIENT_ID}:{settings.EVE_CLIENT_SECRET}".encode("utf-8")
        ).decode("ascii")
        response = self._client.post(
            settings.EVE_TOKEN_API,
            data={
                "grant_type": "refresh_token",
                "refresh_token": decrypt_refresh_token(token.refresh_token_enc),
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "Authorization": f"Basic {auth}",
            },
        )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            token.last_refresh_error = response.text
            token.save(update_fields=["last_refresh_error", "updated_at"])
            raise TokenRefreshError(f"Token refresh failed: {response.status_code}") from exc

        payload = response.json()
        token.access_token = payload.get("access_token", "")
        token.refresh_token_enc = encrypt_refresh_token(
            payload.get("refresh_token") or decrypt_refresh_token(token.refresh_token_enc)
        )
        token.expires_at = timezone.now() + timedelta(seconds=int(payload.get("expires_in", 0) or 0))
        token.scopes = payload.get("scope", token.scopes)
        token.last_refresh_error = ""
        token.save()
        return token

    def fetch_corp_membership(self, access_token: str) -> CorpMembership:
        parsed = parse_access_token(access_token)
        response = self._client.get(
            f"{settings.EVE_API_BASE}/characters/{parsed.character_id}",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        return CorpMembership(
            character_id=parsed.character_id,
            corporation_id=int(payload["corporation_id"]),
            alliance_id=int(payload["alliance_id"]) if payload.get("alliance_id") else None,
        )

    def validate_bearer_token(self, authorization_header: str | None) -> ParsedAccessToken:
        if not authorization_header:
            raise TokenValidationError("No token")
        split = authorization_header.split(" ")
        if len(split) != 2 or split[0] != "Bearer":
            raise TokenValidationError("Invalid token")

        access_token = split[1]
        parsed = parse_access_token(access_token)
        membership = self.fetch_corp_membership(access_token)
        if settings.EVE_CORPORATION_ID and membership.corporation_id != settings.EVE_CORPORATION_ID:
            raise TokenValidationError("Unauthorized corporation member")

        Character.objects.update_or_create(
            eve_character_id=parsed.character_id,
            defaults={
                "name": parsed.character_name or f"Character {parsed.character_id}",
                "corporation_id": membership.corporation_id,
                "alliance_id": membership.alliance_id,
            },
        )
        return parsed
