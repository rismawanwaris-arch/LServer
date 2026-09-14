from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import Adjustment, Discrepancy, Match, ReconDay


@admin.register(Match)
class MatchAdmin(SimpleHistoryAdmin):
    list_display = ["book_date", "channel", "match_type", "amount_bank", "amount_diff", "confidence", "voided_at"]
    list_filter = ["channel", "match_type", "book_date"]
    autocomplete_fields = []
    readonly_fields = ["amount_diff"]


@admin.register(Discrepancy)
class DiscrepancyAdmin(SimpleHistoryAdmin):
    list_display = ["code", "origin_book_date", "channel", "kind", "amount", "status", "resolved_book_date"]
    list_filter = ["status", "kind", "channel", "origin_book_date"]
    search_fields = ["code", "note"]
    readonly_fields = ["code", "origin_book_date"]


@admin.register(Adjustment)
class AdjustmentAdmin(admin.ModelAdmin):
    list_display = ["book_date", "discrepancy", "amount", "reason", "created_at"]
    list_filter = ["book_date"]

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ReconDay)
class ReconDayAdmin(SimpleHistoryAdmin):
    list_display = [
        "book_date",
        "status",
        "locked",
        "total_in_bank",
        "total_out_otomax",
        "selisih_initial",
        "selisih_current",
        "unmatched_count",
    ]
    list_filter = ["status", "locked"]
    readonly_fields = [f.name for f in ReconDay._meta.fields]
