from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

from datakiosk import views as dk
from labor import views as labor
from reports import views as reports
from hmi import kviews as kv
from hmi import views as hmi
from scheduler import views as sch
from quality import viewer as fviewer
from quality import views as quality
from supervisor import views as supervisor
from maintenance import views as maintenance
from suite import sections, thin

urlpatterns = [
    path("", RedirectView.as_view(url="/operator/", permanent=False)),
    path("admin/", kv.admin_page, name="admin"),                 # Admin: the suite's settings, its own section
    path("db-admin/", admin.site.urls),                          # Django's raw table editor (superusers only)
    # Operator Kiosk
    path("operator/", kv.page, name="operator"),
    path("operator/<str:page>/", kv.page),
    path("station/<str:lock>/", kv.page, name="station"),
    path("station/<str:lock>/<str:page>/", kv.page),
    # the Operator Kiosk's parts: a page's view, a region, a dialog, an action (all built on the server)
    path("kiosk/v/<str:page>", kv.view),
    path("kiosk/r/<str:name>", kv.part),
    path("kiosk/m/<str:name>", kv.dialog),
    path("kiosk/do/<str:name>", kv.act),
    path("kiosk/backup/<str:name>", kv.db_backup_file),
    path("kiosk/", RedirectView.as_view(url="/operator/", permanent=False, query_string=True)),
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
    path("api/scheduler/job/split", sch.split_job),
    path("api/scheduler/job/merge", sch.merge_job),
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
    path("data/<str:line>/", dk.board, name="data-line"),
    path("thin/watch", thin.watch),
    # Reports
    path("reports/", reports.board, name="reports"),
    path("api/report", reports.report),
    path("api/report/grafana/panels", reports.grafana_panels),
    path("api/report/grafana/data", reports.grafana_data),
    # Labor Budget (shift leads only)
    path("labor/", labor.page, name="labor"),
    path("budget/", labor.budget_only, name="budget"),
    path("supervisor/", supervisor.page, name="supervisor"),
    path("quality/", quality.page, name="quality"),
    path("quality/viewer/app/", fviewer.app, name="filter-viewer"),
    path("api/quality/fv/parts", fviewer.parts),
    path("api/quality/fv/parts/set", fviewer.part_set),
    path("api/quality/fv/parts/delete", fviewer.part_delete),
    path("api/quality/fv/parts/flat", fviewer.part_flat),
    path("api/quality/fv/running", fviewer.running),
    path("api/quality/fv/refresh", fviewer.refresh),
    path("quality/viewer/refresh-all/", fviewer.refresh_all),
    path("supervisor/log/<int:pk>/", supervisor.log, name="supervisor-log"),
    path("supervisor/edit/<str:kind>/<int:pk>/", supervisor.edit, name="supervisor-edit"),
    path("supervisor/build/", supervisor.build_list, name="supervisor-build"),
    path("supervisor/build/new/", supervisor.build_add, name="supervisor-build-add"),
    path("supervisor/build/<int:pk>/", supervisor.build_fill, name="supervisor-build-fill"),
    path("supervisor/processout/<int:pk>/", supervisor.process_out, name="supervisor-processout"),
    path("supervisor/coend/<int:pk>/", supervisor.co_force_end, name="supervisor-coend"),
    path("maintenance/", maintenance.page, name="maintenance-section"),
    path("maintenance/do/<str:name>", maintenance.do, name="maintenance-do"),
    path("packaging/", sections.soon, {"key": "packaging"}, name="packaging-section"),   # (coming later)   # the Labor Budget alone (no other sections), e.g. for the office
    path("api/labor/state", labor.state),
    path("api/labor/save", labor.save),
    path("api/labor/history", labor.history),
    path("api/labor/restore", labor.restore),
]
