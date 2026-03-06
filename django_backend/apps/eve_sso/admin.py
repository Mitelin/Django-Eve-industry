from django.contrib import admin

from apps.eve_sso.models import EsiToken


@admin.register(EsiToken)
class EsiTokenAdmin(admin.ModelAdmin):
	list_display = ("id", "purpose", "owner_character", "expires_at", "updated_at")
	list_filter = ("purpose",)
	search_fields = ("owner_character__name", "owner_character__eve_character_id")
