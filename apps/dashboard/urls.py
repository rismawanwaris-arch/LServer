from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    # 1. Dashboard
    path("", views.day_view, name="day"),
    # 2. Upload Data & Preview
    path("upload/", views.upload_view, name="upload"),
    path("upload/delete-batch/<int:pk>/", views.delete_batch_action, name="delete-batch"),
    # 3. Hasil Rekonsiliasi
    path("matches/", views.matches_view, name="matches"),
    path("matches/unpair/<int:pk>/", views.unpair_match_action, name="unpair-match"),
    path("manual-match/", views.manual_match_action, name="manual-match"),
    path("manual-match/bulk/", views.bulk_manual_match_action, name="manual-match-bulk"),
    # 4. Antrean Review Manual
    path("review-manual/", views.manual_review_view, name="manual-review"),
    path("review-manual/tag/<int:pk>/", views.manual_tag_action, name="manual-tag-action"),
    # 5. Pending Settle
    path("pending-settle/", views.pending_settle_view, name="pending-settle"),
    path("pending-settle/tag/<int:pk>/", views.manual_tag_otomax_action, name="manual-tag-otomax-action"),
    # 6. Riwayat & Laporan
    path("reports/", views.reports_view, name="reports"),
    path("reports/export/", views.reports_export_action, name="reports-export"),
    # 7. Aturan Filter Pemisahan Non-Engine
    path("rules/", views.exclusion_rules_view, name="exclusion-rules"),
    path("rules/add/", views.add_exclusion_rule_action, name="add-exclusion-rule"),
    path("rules/toggle/<int:pk>/", views.toggle_exclusion_rule_action, name="toggle-exclusion-rule"),
    path("rules/delete/<int:pk>/", views.delete_exclusion_rule_action, name="delete-exclusion-rule"),
    path("rules/apply/", views.apply_exclusion_rules_action, name="apply-exclusion-rules"),
    # Recon Engine & Day Control
    path("run/", views.run_engine, name="run-engine"),
    path("close/", views.close_view, name="close-day"),
    path("reopen/", views.reopen_view, name="reopen-day"),
    path("discrepancy/<int:pk>/resolve/", views.resolve_view, name="resolve"),
    # Data purge admin
    path("data/", views.data_admin, name="data-admin"),
    path("data/purge-day/", views.purge_day_view, name="purge-day"),
    path("data/purge-all/", views.purge_all_view, name="purge-all"),
    # Pengaturan Aplikasi
    path("pengaturan/", views.app_settings_view, name="app-settings"),
    path("pengaturan/simpan/", views.update_app_settings_action, name="update-app-settings"),
    # Auth
    path("login/", auth_views.LoginView.as_view(template_name="dashboard/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="login"), name="logout"),
]
