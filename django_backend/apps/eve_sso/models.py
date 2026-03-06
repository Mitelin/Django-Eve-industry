from django.db import models

from apps.common.models import TimeStampedModel


class EsiToken(TimeStampedModel):
    PURPOSE_CHOICES = (
        ("full", "full"),
        ("sales", "sales"),
        ("corp", "corp"),
    )

    owner_character = models.ForeignKey(
        "accounts.Character",
        on_delete=models.CASCADE,
        related_name="tokens",
        null=True,
        blank=True,
    )
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES, db_index=True)
    refresh_token_enc = models.TextField()
    access_token = models.TextField(blank=True, default="")
    expires_at = models.DateTimeField(db_index=True)
    scopes = models.TextField(blank=True, default="")
    last_refresh_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["purpose", "owner_character_id"]

    def __str__(self) -> str:
        return f"{self.purpose}:{self.owner_character_id or 'service'}"
