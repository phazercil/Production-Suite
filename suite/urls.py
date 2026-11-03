from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

from datakiosk import views as dk
from labor import views as labor
from reports import views as reports
from hmi import views as hmi
from scheduler import views as sch
from suite import thin

urlpatterns = [
    path("", RedirectView.as_view(url="/operator/", permanent=False)),
    path("admin/", admin.site.urls),
    # Operator Kiosk
    path("operator/", hmi.kiosk, name="operator"),
    path("station/<str:machine>/", hmi.station, name="station"),
    path("api/hmi/state", hmi.state),
    path("api/hmi/login", hmi.login),
    path("api/hmi/logout", hmi.logout),
    path("api/hmi/ping", hmi.ping),
    path("api/hmi/cmd", hmi.command),
    path("api/feed/<str:machine>", hmi.feed_push),
    path("api/ingest/<str:name>", hmi.ingest),
    path("api/version", hmi.version),
    path("api/updates/upload", hmi.update_upload),
    path("api/hmi/ticket-photo", hmi.ticket_photo_upload),
    path("api/hmi/ticket-photo/<int:pk>", hmi.ticket_photo),
    # Scheduler
    path("scheduler/", sch.board, name="scheduler"),
    path("api/scheduler/state", sch.state),
    path("api/scheduler/job", sch.put_job),
    path("api/scheduler/job/delete", sch.del_job),
    path("api/scheduler/preview", sch.preview),
    path("api/scheduler/mo", sch.put_mo),
    path("api/scheduler/mo/delete", sch.del_mo),
    path("api/scheduler/config", sch.save_config),
    path("api/scheduler/import/parse", sch.import_parse),
    path("api/scheduler/import/preview", sch.import_preview),
    path("api/scheduler/import/commit", sch.import_commit),
    path("api/scheduler/catalog", sch.catalog),
    path("api/scheduler/catalog/import", sch.catalog_import),
    path("api/scheduler/catalog/reset", sch.catalog_reset),
    # Data Kiosk
    path("data/", dk.board, name="data"),
    path("api/data/state", dk.state),
    path("data/r/<str:name>", dk.region),
    path("thin/watch", thin.watch),
    # Reports
    path("reports/", reports.board, name="reports"),
    path("api/report", reports.report),
    path("api/report/grafana/panels", reports.grafana_panels),
    path("api/report/grafana/data", reports.grafana_data),
    # Labor Budget (shift leads only)
    path("labor/", labor.page, name="labor"),
    path("api/labor/state", labor.state),
    path("api/labor/save", labor.save),
    path("api/labor/history", labor.history),
    path("api/labor/restore", labor.restore),
]
