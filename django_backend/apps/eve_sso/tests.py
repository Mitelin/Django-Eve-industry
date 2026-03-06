from __future__ import annotations

import base64
import json
from datetime import timedelta
from unittest.mock import MagicMock

import httpx
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.eve_sso.models import EsiToken
from apps.eve_sso.services import (
    EsiTokenService,
    TokenRefreshError,
    TokenValidationError,
    decrypt_refresh_token,
    encrypt_refresh_token,
    parse_access_token,
)


def _build_access_token(character_id: int = 90000001, name: str = "Aubislav", scopes: list[str] | None = None) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode("utf-8")).decode("utf-8").rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "sub": f"CHARACTER:EVE:{character_id}",
                "name": name,
                "scp": scopes or ["esi-assets.read_corporation_assets.v1"],
            }
        ).encode("utf-8")
    ).decode("utf-8").rstrip("=")
    return f"{header}.{payload}.signature"


@override_settings(ESI_TOKEN_ENCRYPTION_KEY="unit-test-key", EVE_CORPORATION_ID=123)
class EsiTokenServiceTests(TestCase):
    def test_encrypt_refresh_token_roundtrip(self) -> None:
        encrypted = encrypt_refresh_token("refresh-token")
        self.assertNotEqual(encrypted, "refresh-token")
        self.assertEqual(decrypt_refresh_token(encrypted), "refresh-token")

    def test_parse_access_token_extracts_identity(self) -> None:
        parsed = parse_access_token(_build_access_token())
        self.assertEqual(parsed.character_id, 90000001)
        self.assertEqual(parsed.character_name, "Aubislav")
        self.assertEqual(parsed.scopes, ("esi-assets.read_corporation_assets.v1",))

    def test_upsert_token_response_encrypts_refresh_token(self) -> None:
        service = EsiTokenService(client=MagicMock())

        token = service.upsert_token_response(
            {
                "access_token": _build_access_token(),
                "refresh_token": "refresh-token",
                "expires_in": 1200,
                "scope": "scope-a scope-b",
            },
            purpose="corp",
        )

        self.assertEqual(token.purpose, "corp")
        self.assertNotEqual(token.refresh_token_enc, "refresh-token")
        self.assertEqual(decrypt_refresh_token(token.refresh_token_enc), "refresh-token")
        self.assertEqual(token.owner_character.eve_character_id, 90000001)
        self.assertEqual(token.scopes, "scope-a scope-b")

    @override_settings(EVE_CLIENT_ID="client", EVE_CLIENT_SECRET="secret")
    def test_refresh_access_token_updates_token(self) -> None:
        client = MagicMock()
        client.post.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=MagicMock(
                return_value={
                    "access_token": _build_access_token(name="Updated"),
                    "refresh_token": "new-refresh-token",
                    "expires_in": 3600,
                    "scope": "scope-a",
                }
            ),
        )
        service = EsiTokenService(client=client)
        token = service.upsert_token_response(
            {
                "access_token": _build_access_token(),
                "refresh_token": "refresh-token",
                "expires_in": 60,
            },
            purpose="full",
        )
        token.expires_at = timezone.now() - timedelta(seconds=1)
        token.save(update_fields=["expires_at"])

        refreshed = service.refresh_access_token(token)

        self.assertEqual(decrypt_refresh_token(refreshed.refresh_token_enc), "new-refresh-token")
        self.assertEqual(refreshed.scopes, "scope-a")
        self.assertGreater(refreshed.expires_at, timezone.now())

    @override_settings(EVE_CLIENT_ID="client", EVE_CLIENT_SECRET="secret")
    def test_refresh_access_token_records_failure(self) -> None:
        response = MagicMock()
        response.status_code = 400
        response.text = "bad request"
        request = httpx.Request("POST", "https://login.eveonline.com/v2/oauth/token")
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "boom",
            request=request,
            response=httpx.Response(400, request=request),
        )
        client = MagicMock()
        client.post.return_value = response
        service = EsiTokenService(client=client)
        token = service.upsert_token_response(
            {
                "access_token": _build_access_token(),
                "refresh_token": "refresh-token",
                "expires_in": 60,
            }
        )

        with self.assertRaises(TokenRefreshError):
            service.refresh_access_token(token)

        token.refresh_from_db()
        self.assertEqual(token.last_refresh_error, "bad request")

    def test_validate_bearer_token_rejects_invalid_header(self) -> None:
        service = EsiTokenService(client=MagicMock())

        with self.assertRaises(TokenValidationError):
            service.validate_bearer_token("Token abc")

    def test_validate_bearer_token_updates_character_membership(self) -> None:
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"corporation_id": 123, "alliance_id": 456}
        client = MagicMock()
        client.get.return_value = response
        service = EsiTokenService(client=client)
        access_token = _build_access_token(character_id=90000002, name="Director")

        parsed = service.validate_bearer_token(f"Bearer {access_token}")

        self.assertEqual(parsed.character_id, 90000002)
        token_character = EsiToken.objects.none()
        self.assertEqual(token_character.count(), 0)
        from apps.accounts.models import Character

        character = Character.objects.get(eve_character_id=90000002)
        self.assertEqual(character.corporation_id, 123)
        self.assertEqual(character.alliance_id, 456)
