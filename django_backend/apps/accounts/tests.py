import json

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase

from apps.accounts.models import Character


class AccountPageTests(TestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(username="account-user", password="x")
        self.client.force_login(self.user)
        self.main = Character.objects.create(
            user=self.user,
            eve_character_id=90001001,
            name="Main One",
            corporation_id=123,
            is_main=True,
        )
        self.alt = Character.objects.create(
            user=self.user,
            eve_character_id=90001002,
            name="Alt Two",
            corporation_id=123,
            is_main=False,
        )

    def test_account_page_renders_linked_characters(self) -> None:
        response = self.client.get("/account/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Main One")
        self.assertContains(response, "Alt Two")
        self.assertContains(response, "Set as main")

    def test_account_page_can_switch_main_character(self) -> None:
        response = self.client.post(
            "/account/",
            data={"action": "set_main_character", "characterId": self.alt.id},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/account/")
        self.main.refresh_from_db()
        self.alt.refresh_from_db()
        self.assertFalse(self.main.is_main)
        self.assertTrue(self.alt.is_main)

    def test_account_page_can_remove_non_main_character(self) -> None:
        response = self.client.post(
            "/account/",
            data={"action": "remove_character", "characterId": self.alt.id},
        )

        self.assertEqual(response.status_code, 302)
        self.alt.refresh_from_db()
        self.main.refresh_from_db()
        self.assertIsNone(self.alt.user_id)
        self.assertFalse(self.alt.is_main)
        self.assertTrue(self.main.is_main)

    def test_account_page_promotes_new_main_when_removing_current_main(self) -> None:
        response = self.client.post(
            "/account/",
            data={"action": "remove_character", "characterId": self.main.id},
        )

        self.assertEqual(response.status_code, 302)
        self.main.refresh_from_db()
        self.alt.refresh_from_db()
        self.assertIsNone(self.main.user_id)
        self.assertFalse(self.main.is_main)
        self.assertTrue(self.alt.is_main)

    def test_account_page_blocks_removing_last_character(self) -> None:
        self.alt.user = None
        self.alt.is_main = False
        self.alt.save(update_fields=["user", "is_main", "updated_at"])

        response = self.client.post(
            "/account/",
            data={"action": "remove_character", "characterId": self.main.id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "At least one linked character must remain on the account.")
        self.main.refresh_from_db()
        self.assertEqual(self.main.user_id, self.user.id)


class AccountDirectorAccessTests(TestCase):
    def setUp(self) -> None:
        self.director = get_user_model().objects.create_user(username="director-account", password="x", first_name="Director")
        self.director.is_staff = True
        self.director.save(update_fields=["is_staff"])
        self.worker_group, _ = Group.objects.get_or_create(name="worker")
        self.worker = get_user_model().objects.create_user(username="worker-account", password="x", first_name="Worker")
        self.target = get_user_model().objects.create_user(username="target-account", password="x", first_name="Target")
        Character.objects.create(
            user=self.target,
            eve_character_id=90002001,
            name="Target Main",
            corporation_id=456,
            is_main=True,
        )
        Character.objects.create(
            user=self.target,
            eve_character_id=90002002,
            name="Target Alt",
            corporation_id=456,
            is_main=False,
        )
        self.client.force_login(self.director)

    def test_director_can_list_account_access(self) -> None:
        response = self.client.get("/api/accounts/access")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        target_entry = next(item for item in payload["accounts"] if item["id"] == self.target.id)
        self.assertEqual(target_entry["mainCharacterName"], "Target Main")
        self.assertEqual(target_entry["linkedCharacterCount"], 2)
        self.assertFalse(target_entry["isWorker"])

    def test_director_can_update_account_roles(self) -> None:
        response = self.client.post(
            f"/api/accounts/{self.target.id}/roles",
            data=json.dumps({"worker": True, "director": True}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_staff)
        self.assertTrue(self.target.groups.filter(name="worker").exists())
        self.assertTrue(response.json()["account"]["isDirector"])
        self.assertTrue(response.json()["account"]["hasWorkerGroup"])

    def test_non_director_cannot_update_account_roles(self) -> None:
        self.client.force_login(self.worker)

        response = self.client.post(
            f"/api/accounts/{self.target.id}/roles",
            data=json.dumps({"worker": True}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "Director access is required")

    def test_director_cannot_remove_own_director_access(self) -> None:
        response = self.client.post(
            f"/api/accounts/{self.director.id}/roles",
            data=json.dumps({"director": False}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "You cannot remove your own director access.")