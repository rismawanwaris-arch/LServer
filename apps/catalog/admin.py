from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import ExclusionRule, MerchantMap, Reseller, ResellerAlias


@admin.register(ExclusionRule)
class ExclusionRuleAdmin(SimpleHistoryAdmin):
    list_display = ["name", "keywords", "target", "channel", "category", "active"]
    list_filter = ["active", "target", "channel", "category"]
    search_fields = ["name", "keywords", "notes"]


class AliasInline(admin.TabularInline):
    model = ResellerAlias
    extra = 0
    fields = ["alias_raw", "alias_norm"]
    readonly_fields = ["alias_norm"]


@admin.register(Reseller)
class ResellerAdmin(SimpleHistoryAdmin):
    list_display = ["code", "name", "active"]
    list_filter = ["active"]
    search_fields = ["code", "name"]
    inlines = [AliasInline]


@admin.register(ResellerAlias)
class ResellerAliasAdmin(SimpleHistoryAdmin):
    list_display = ["alias_raw", "alias_norm", "reseller"]
    search_fields = ["alias_raw", "alias_norm"]
    autocomplete_fields = ["reseller"]


@admin.register(MerchantMap)
class MerchantMapAdmin(SimpleHistoryAdmin):
    list_display = ["merchant_id", "merchant_name", "reseller", "active"]
    list_filter = ["active"]
    search_fields = ["merchant_id", "merchant_name"]
    autocomplete_fields = ["reseller"]
