from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("", views.day_view, name="day"),
    path("upload/", views.upload, name="upload"),
    path("run/", views.run_engine, name="run-engine"),
    path("close/", views.close_view, name="close-day"),
    path("reopen/", views.reopen_view, name="reopen-day"),
    path("data/", views.data_admin, name="data-admin"),
    path("data/purge-day/", views.purge_day_view, name="purge-day"),
    path("data/purge-all/", views.purge_all_view, name="purge-all"),
    path("discrepancy/<int:pk>/resolve/", views.resolve_view, name="resolve"),
    path("login/", auth_views.LoginView.as_view(template_name="dashboard/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="login"), name="logout"),
]
