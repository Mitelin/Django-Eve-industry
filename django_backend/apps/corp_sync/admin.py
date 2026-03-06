from django.contrib import admin

from apps.corp_sync.models import (
	CorpAssetSnapshot,
	CorpJobSnapshot,
	SyncRun,
	WalletJournalSnapshot,
	WalletTransactionSnapshot,
)


admin.site.register(SyncRun)
admin.site.register(CorpAssetSnapshot)
admin.site.register(CorpJobSnapshot)
admin.site.register(WalletJournalSnapshot)
admin.site.register(WalletTransactionSnapshot)
