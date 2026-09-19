"""Views dashboard, dipecah per halaman (lihat masing-masing submodule untuk detail).

File ini cuma re-export supaya `from . import views` + `views.<nama_fungsi>` di
apps/dashboard/urls.py tidak perlu berubah sama sekali."""

from __future__ import annotations

from .data_admin import data_admin, purge_all_view, purge_day_view
from .day import day_view
from .engine_control import close_view, reopen_view, resolve_view, run_engine
from .exclusion_rules import (
    add_exclusion_rule_action,
    apply_exclusion_rules_action,
    delete_exclusion_rule_action,
    exclusion_rules_view,
    toggle_exclusion_rule_action,
)
from .manual_review import (
    bulk_manual_match_action,
    manual_match_action,
    manual_review_view,
    manual_tag_action,
    unpair_match_action,
)
from .matches import matches_view
from .pending_settle import manual_tag_otomax_action, pending_settle_view
from .reports import reports_export_action, reports_view
from .upload import delete_batch_action, upload, upload_view

__all__ = [
    "add_exclusion_rule_action",
    "apply_exclusion_rules_action",
    "bulk_manual_match_action",
    "close_view",
    "data_admin",
    "day_view",
    "delete_batch_action",
    "delete_exclusion_rule_action",
    "exclusion_rules_view",
    "manual_match_action",
    "manual_review_view",
    "manual_tag_action",
    "manual_tag_otomax_action",
    "matches_view",
    "pending_settle_view",
    "purge_all_view",
    "purge_day_view",
    "reopen_view",
    "reports_export_action",
    "reports_view",
    "resolve_view",
    "run_engine",
    "toggle_exclusion_rule_action",
    "unpair_match_action",
    "upload",
    "upload_view",
]
