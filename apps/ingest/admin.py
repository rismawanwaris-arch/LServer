from django.contrib import admin

from .models import BankMutation, DebitIgnored, ImportBatch, OtomaxEntry


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    list_display = ["channel", "book_date", "status", "row_count", "quarantined_count", "created_at"]
    list_filter = ["channel", "status", "book_date"]
    readonly_fields = [f.name for f in ImportBatch._meta.fields]


@admin.register(BankMutation)
class BankMutationAdmin(admin.ModelAdmin):
    list_display = ["book_date", "channel", "amount", "ref_normalized", "match_status", "review_flag"]
    list_filter = ["channel", "match_status", "book_date"]
    search_fields = ["ref_normalized", "ref_core", "description_raw", "external_ref"]
    readonly_fields = [f.name for f in BankMutation._meta.fields]


@admin.register(OtomaxEntry)
class OtomaxEntryAdmin(admin.ModelAdmin):
    list_display = ["book_date", "reseller_name_raw", "amount", "category", "channel_hint", "match_status"]
    list_filter = ["category", "channel_hint", "match_status", "book_date"]
    search_fields = ["reseller_name_raw", "ref_normalized", "description_raw"]
    readonly_fields = [f.name for f in OtomaxEntry._meta.fields]


@admin.register(DebitIgnored)
class DebitIgnoredAdmin(admin.ModelAdmin):
    list_display = ["book_date", "channel", "amount", "description_raw"]
    list_filter = ["channel", "book_date"]
    readonly_fields = [f.name for f in DebitIgnored._meta.fields]
