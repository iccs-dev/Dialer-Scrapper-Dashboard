"""URL routes for the monitoring app."""

from django.urls import path
from django.views.generic import RedirectView

from . import api, views

urlpatterns = [
    # Pages
    # The kiosk browser and anyone typing the bare server address land on "/",
    # so it has to lead somewhere rather than 404. Permanent would be cached
    # by the browser and make the root impossible to repoint later.
    path("", RedirectView.as_view(pattern_name="dashboard", permanent=False),
         name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("api/dashboard/run/", views.run_scraper, name="dashboard-run"),
    path("api/dashboard/run-status/", views.run_status, name="dashboard-run-status"),

    # Scraper callbacks - token authenticated (requirement 12).
    path("api/monitor/start/", api.monitor_start, name="monitor-start"),
    path("api/monitor/log/", api.monitor_log, name="monitor-log"),
    path("api/monitor/heartbeat/", api.monitor_heartbeat, name="monitor-heartbeat"),
    path("api/monitor/success/", api.monitor_success, name="monitor-success"),
    path("api/monitor/warning/", api.monitor_warning, name="monitor-warning"),
    path("api/monitor/failed/", api.monitor_failed, name="monitor-failed"),

    # Dashboard reads - session authenticated (requirement 16).
    path("api/dashboard/state/", api.dashboard_state, name="dashboard-state"),
    path("api/dashboard/logs/", api.dashboard_logs, name="dashboard-logs"),
    path("api/dashboard/matrix/", api.dashboard_matrix, name="dashboard-matrix"),
    path("api/dashboard/dataset/", api.dashboard_dataset, name="dashboard-dataset"),
]
