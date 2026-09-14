from django.urls import path

from . import views

urlpatterns = [
    path("upload/otomax", views.upload_otomax, name="api-upload-otomax"),
    path("upload/mutasi/<str:bank>", views.upload_mutasi, name="api-upload-mutasi"),
    path("upload/preview", views.upload_preview, name="api-upload-preview"),
    path("reconcile/run", views.reconcile_run, name="api-reconcile-run"),
    path("reconcile/summary", views.reconcile_summary, name="api-reconcile-summary"),
    path("reconcile/unmatched", views.reconcile_unmatched, name="api-reconcile-unmatched"),
    path("reconcile/pending-settle", views.reconcile_pending_settle, name="api-reconcile-pending-settle"),
    path("reconcile/manual-tag", views.reconcile_manual_tag, name="api-reconcile-manual-tag"),
    path("reports/daily", views.reports_daily, name="api-reports-daily"),
    path("reports/export", views.reports_export, name="api-reports-export"),
]
