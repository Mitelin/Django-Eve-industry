from django.conf import settings
from django.db import models

from apps.common.models import TimeStampedModel


class Character(TimeStampedModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="characters",
        null=True,
        blank=True,
    )
    eve_character_id = models.BigIntegerField(unique=True, db_index=True)
    name = models.CharField(max_length=255)
    corporation_id = models.BigIntegerField(db_index=True)
    alliance_id = models.BigIntegerField(null=True, blank=True)
    is_main = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.eve_character_id})"
