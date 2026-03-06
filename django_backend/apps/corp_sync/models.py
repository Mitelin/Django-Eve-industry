from django.db import models

from apps.common.models import TimeStampedModel


class SyncRun(TimeStampedModel):
    KIND_CHOICES = (
        ("assets", "assets"),
        ("jobs", "jobs"),
        ("wallet_journal", "wallet_journal"),
        ("wallet_transactions", "wallet_transactions"),
    )
    STATUS_CHOICES = (
        ("started", "started"),
        ("ok", "ok"),
        ("failed", "failed"),
    )

    kind = models.CharField(max_length=40, choices=KIND_CHOICES, db_index=True)
    corporation_id = models.BigIntegerField(db_index=True)
    wallet_division = models.IntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, db_index=True)
    rows_written = models.IntegerField(default=0)
    error_text = models.TextField(blank=True, default="")
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class CorpAssetSnapshot(TimeStampedModel):
    sync_run = models.ForeignKey(SyncRun, on_delete=models.PROTECT, related_name="assets")
    item_id = models.BigIntegerField(db_index=True)
    type_id = models.BigIntegerField(db_index=True)
    location_id = models.BigIntegerField(db_index=True)
    location_type = models.CharField(max_length=100, blank=True, default="")
    location_flag = models.CharField(max_length=100, blank=True, default="")
    quantity = models.BigIntegerField(default=0)
    is_singleton = models.BooleanField(default=False)
    is_blueprint_copy = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["location_id", "location_flag"]),
            models.Index(fields=["type_id", "location_id"]),
        ]


class CorpJobSnapshot(TimeStampedModel):
    sync_run = models.ForeignKey(SyncRun, on_delete=models.PROTECT, related_name="jobs")
    job_id = models.BigIntegerField(db_index=True)
    activity_id = models.IntegerField(db_index=True)
    blueprint_id = models.BigIntegerField(null=True, blank=True)
    blueprint_type_id = models.BigIntegerField(db_index=True)
    product_type_id = models.BigIntegerField(db_index=True)
    runs = models.IntegerField(default=0)
    status = models.CharField(max_length=30, blank=True, default="")
    installer_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    output_location_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True)
    completed_date = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "output_location_id"]),
            models.Index(fields=["installer_id", "activity_id"]),
        ]


class WalletJournalSnapshot(TimeStampedModel):
    sync_run = models.ForeignKey(SyncRun, on_delete=models.PROTECT, related_name="wallet_journal_entries")
    entry_id = models.BigIntegerField(db_index=True)
    amount = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    balance = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    context_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    context_id_type = models.CharField(max_length=100, blank=True, default="")
    entry_date = models.DateTimeField(db_index=True)
    description = models.TextField(blank=True, default="")
    first_party_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    reason = models.TextField(blank=True, default="")
    ref_type = models.CharField(max_length=100, blank=True, default="", db_index=True)
    second_party_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    tax = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    tax_receiver_id = models.BigIntegerField(null=True, blank=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["ref_type", "entry_date"]),
            models.Index(fields=["context_id", "context_id_type"]),
        ]


class WalletTransactionSnapshot(TimeStampedModel):
    sync_run = models.ForeignKey(SyncRun, on_delete=models.PROTECT, related_name="wallet_transactions")
    transaction_id = models.BigIntegerField(db_index=True)
    client_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    transaction_date = models.DateTimeField(db_index=True)
    is_buy = models.BooleanField(default=False, db_index=True)
    journal_ref_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    location_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    quantity = models.BigIntegerField(default=0)
    type_id = models.BigIntegerField(db_index=True)
    unit_price = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["type_id", "transaction_date"]),
            models.Index(fields=["is_buy", "transaction_date"]),
        ]
