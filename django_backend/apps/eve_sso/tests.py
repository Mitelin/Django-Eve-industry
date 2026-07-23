from __future__ import annotations

import base64
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import httpx
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Character
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


@override_settings(ESI_TOKEN_ENCRYPTION_KEY="unit-test-key", EVE_CLIENT_ID="client", EVE_CLIENT_SECRET="secret")
class EveSsoViewTests(TestCase):
    def test_login_page_renders(self) -> None:
        response = self.client.get("/auth/login/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Continue with EVE Online")

    def test_callback_creates_account_and_logs_user_in(self) -> None:
        session = self.client.session
        session["eve_sso_oauth"] = {"state": "state-1", "mode": "login", "next": "/account/"}
        session.save()

        exchange_payload = {
            "access_token": _build_access_token(character_id=90000011, name="Fresh Pilot"),
            "refresh_token": "refresh-token",
            "expires_in": 3600,
            "scope": "scope-a",
        }
        membership = MagicMock(corporation_id=123, alliance_id=456, character_id=90000011)

        with (
            self.settings(EVE_CORPORATION_ID=123),
            self.subTest("callback"),
        ):
            with patch("apps.eve_sso.views.EsiTokenService") as service_cls:
                service = service_cls.return_value
                service.exchange_authorization_code.return_value = exchange_payload
                service.get_valid_access_token.return_value = exchange_payload["access_token"]
                service.fetch_corp_membership.return_value = membership
                service.upsert_token_response.side_effect = EsiTokenService(client=MagicMock()).upsert_token_response

                response = self.client.get("/auth/callback/?state=state-1&code=auth-code")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/account/")
        character = Character.objects.get(eve_character_id=90000011)
        self.assertIsNotNone(character.user)
        self.assertTrue(character.is_main)
        account_response = self.client.get("/account/")
        self.assertEqual(account_response.status_code, 200)
        self.assertContains(account_response, "Fresh Pilot")

    def test_callback_links_additional_character_to_existing_account(self) -> None:
        user = get_user_model().objects.create_user(username="eve-90000011")
        self.client.force_login(user)
        Character.objects.create(user=user, eve_character_id=90000011, name="Main", corporation_id=123, is_main=True)
        session = self.client.session
        session["eve_sso_oauth"] = {"state": "state-2", "mode": "link", "next": "/account/"}
        session.save()

        exchange_payload = {
            "access_token": _build_access_token(character_id=90000012, name="Alt Pilot"),
            "refresh_token": "refresh-token-2",
            "expires_in": 3600,
            "scope": "scope-a",
        }
        membership = MagicMock(corporation_id=123, alliance_id=None, character_id=90000012)

        with patch("apps.eve_sso.views.EsiTokenService") as service_cls:
            service = service_cls.return_value
            service.exchange_authorization_code.return_value = exchange_payload
            service.get_valid_access_token.return_value = exchange_payload["access_token"]
            service.fetch_corp_membership.return_value = membership
            service.upsert_token_response.side_effect = EsiTokenService(client=MagicMock()).upsert_token_response

            response = self.client.get("/auth/callback/?state=state-2&code=auth-code")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/account/")
        linked = Character.objects.get(eve_character_id=90000012)
        self.assertEqual(linked.user_id, user.id)
        self.assertFalse(linked.is_main)

    def test_callback_rejects_character_owned_by_another_account(self) -> None:
        user = get_user_model().objects.create_user(username="account-a")
        owner = get_user_model().objects.create_user(username="account-b")
        self.client.force_login(user)
        Character.objects.create(user=owner, eve_character_id=90000013, name="Owned Alt", corporation_id=123, is_main=True)
        session = self.client.session
        session["eve_sso_oauth"] = {"state": "state-3", "mode": "link", "next": "/account/"}
        session.save()

        exchange_payload = {
            "access_token": _build_access_token(character_id=90000013, name="Owned Alt"),
            "refresh_token": "refresh-token-3",
            "expires_in": 3600,
            "scope": "scope-a",
        }
        membership = MagicMock(corporation_id=123, alliance_id=None, character_id=90000013)

        with patch("apps.eve_sso.views.EsiTokenService") as service_cls:
            service = service_cls.return_value
            service.exchange_authorization_code.return_value = exchange_payload
            service.get_valid_access_token.return_value = exchange_payload["access_token"]
            service.fetch_corp_membership.return_value = membership
            service.upsert_token_response.side_effect = EsiTokenService(client=MagicMock()).upsert_token_response

            response = self.client.get("/auth/callback/?state=state-3&code=auth-code")

        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "already linked to another account", status_code=409)

        def test_callback_login_reuses_existing_account_for_linked_character(self) -> None:
            owner = get_user_model().objects.create_user(username="eve-90000021")
            Character.objects.create(user=owner, eve_character_id=90000021, name="Existing Main", corporation_id=123, is_main=True)
            session = self.client.session
            session["eve_sso_oauth"] = {"state": "state-4", "mode": "login", "next": "/account/"}
            session.save()

            exchange_payload = {
                "access_token": _build_access_token(character_id=90000021, name="Existing Main"),
                "refresh_token": "refresh-token-4",
                "expires_in": 3600,
                "scope": "scope-a",
            }
            membership = MagicMock(corporation_id=123, alliance_id=None, character_id=90000021)

            with patch("apps.eve_sso.views.EsiTokenService") as service_cls:
                service = service_cls.return_value
                service.exchange_authorization_code.return_value = exchange_payload
                service.get_valid_access_token.return_value = exchange_payload["access_token"]
                service.fetch_corp_membership.return_value = membership
                service.upsert_token_response.side_effect = EsiTokenService(client=MagicMock()).upsert_token_response

                response = self.client.get("/auth/callback/?state=state-4&code=auth-code")

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response["Location"], "/account/")
            linked = Character.objects.get(eve_character_id=90000021)
            self.assertEqual(linked.user_id, owner.id)
            self.assertEqual(get_user_model().objects.filter(username="eve-90000021").count(), 1)
            self.assertEqual(int(self.client.session.get("_auth_user_id")), owner.id)

        def test_character_cannot_be_created_twice_for_two_accounts(self) -> None:
            first = get_user_model().objects.create_user(username="first-account")
            second = get_user_model().objects.create_user(username="second-account")
            Character.objects.create(user=first, eve_character_id=90000031, name="Unique Pilot", corporation_id=123, is_main=True)

            with self.assertRaises(IntegrityError):
                Character.objects.create(
                    user=second,
                    eve_character_id=90000031,
                    name="Unique Pilot Clone",
                    corporation_id=123,
                    is_main=False,
                )

    def test_account_only_user_cannot_load_operational_pages(self) -> None:
        user = get_user_model().objects.create_user(username="account-only")
        self.client.force_login(user)

        self.assertRedirects(self.client.get("/"), "/account/")
        self.assertRedirects(self.client.get("/director/"), "/account/")
        self.assertRedirects(self.client.get("/director/jobs/"), "/account/")
        self.assertRedirects(self.client.get("/jobs/"), "/account/")
        self.assertRedirects(self.client.get("/worker/"), "/account/")

    def test_worker_can_open_worker_page(self) -> None:
        user = get_user_model().objects.create_user(username="worker-user")
        worker_group, _ = Group.objects.get_or_create(name="worker")
        user.groups.add(worker_group)
        self.client.force_login(user)

        response = self.client.get("/worker/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Worker Command")

    def test_director_can_open_production_jobs_page(self) -> None:
        user = get_user_model().objects.create_user(username="director-user")
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        self.client.force_login(user)

        response = self.client.get("/director/jobs/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Production job entry")

    def test_director_can_open_jobs_board_page(self) -> None:
        user = get_user_model().objects.create_user(username="director-jobs-board-user")
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        self.client.force_login(user)

        response = self.client.get("/jobs/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "JOBS")
