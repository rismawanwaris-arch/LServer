from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    # 1. Dashboard
    path("", views.day_view, name="day"),
    # 2. Upload Data & Preview
    path("upload/", views.upload_view, name="upload"),
    # 3. Hasil Rekonsiliasi
    path("matches/", views.matches_view, name="matches"),
    path("matches/unpair/<int:pk>/", views.unpair_match_action, name="unpair-match"),
    path("manual-match/", views.manual_match_action, name="manual-match"),
    # 4. Antrean Review Manual
    path("review-manual/", views.manual_review_view, name="manual-review"),
    path("review-manual/tag/<int:pk>/", views.manual_tag_action, name="manual-tag-action"),
    # 5. Pending Settle
    path("pending-settle/", views.pending_settle_view, name="pending-settle"),
    # 6. Riwayat & Laporan
    path("reports/", views.reports_view, name="reports"),
    path("reports/export/", views.reports_export_action, name="reports-export"),
    # Recon Engine & Day Control
    path("run/", views.run_engine, name="run-engine"),
    path("close/", views.close_view, name="close-day"),
    path("reopen/", views.reopen_view, name="reopen-day"),
    path("discrepancy/<int:pk>/resolve/", views.resolve_view, name="resolve"),
    # Data purge admin
    path("data/", views.data_admin, name="data-admin"),
    path("data/purge-day/", views.purge_day_view, name="purge-day"),
    path("data/purge-all/", views.purge_all_view, name="purge-all"),
    # Auth
    path("login/", auth_views.LoginView.as_view(template_name="dashboard/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="login"), name="logout"),
]
