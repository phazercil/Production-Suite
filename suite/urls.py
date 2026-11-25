from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

from datakiosk import views as dk
from labor import views as labor
from maintenance import views as maintenance
from plant import calview
from scheduler import stations
from reports import views as reports
from hmi import kviews as kv
from hmi import views as hmi
from instructions import views as wi
from scheduler import s2kview
from scheduler import views as sch
from quality import viewer as fviewer
from quality import views as quality
from supervisor import views as supervisor
from packkiosk import views as packkiosk
from cameras import views as cameras
from sensors import views as sensors
from suite import sections, thin

def tabbed(view, tabs, root):
    """/<section>/<tab>/: the section's page opened on that tab (the same as /<section>/?tab=<tab>, which still works;
    a ?tab= in the query wins, so a page's own "?tab=…" links keep working from a tab's address)."""
    from django.http import HttpResponseRedirect

    def page(request, tab):
        if tab not in {k for k, _ in tabs}:
            return HttpResponseRedirect(root)
        if "tab" not in request.GET:
            q = request.GET.copy()
            q["tab"] = tab
            request.GET = q
        return view(request)
    return page


urlpatterns = [
    path("", RedirectView.as_view(url="/operator/", permanent=False)),
    path("favicon.ico", RedirectView.as_view(url="/static/suite/icon.svg", permanent=False)),   # pages without their own icon link
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
    path("api/hmi/people", hmi.people),
    path("api/hmi/logout", hmi.logout),
    path("api/hmi/ping", hmi.ping),
    path("api/hmi/cmd", hmi.command),
    path("api/feed/<str:machine>", hmi.feed_push),
    path("api/ingest/<str:name>", hmi.ingest),
    path("api/version", hmi.version),
    path("api/updates/upload", hmi.update_upload),
    path("api/hmi/ticket-photo", hmi.ticket_photo_upload),
    path("api/hmi/ticket-photo/<int:pk>", hmi.ticket_photo),
    path("api/hmi/roll-photo/<int:pk>", hmi.roll_photo),
    # Scheduler
    path("scheduler/", sch.board, name="scheduler"),
    path("scheduler/s2k/", s2kview.page, name="s2k-posting"),
    path("instructions/", wi.index),
    path("instructions/<int:pk>/", wi.detail),
    path("instructions/<int:pk>/rev/<int:rev>/", wi.detail),
    path("instructions/<int:pk>/upload", wi.upload),
    path("instructions/<int:pk>/read-by.csv", wi.acks_csv),
    path("instructions/media/<int:mid>", wi.media),
    path("instructions/do/<str:name>", wi.do),
    path("scheduler/s2k/mark", s2kview.mark),
    path("scheduler/<slug:route>/", sch.board_route),
    path("api/scheduler/state", sch.state),
    path("api/scheduler/job", sch.put_job),
    path("api/scheduler/job/delete", sch.del_job),
    path("api/scheduler/job/split", sch.split_job),
    path("api/scheduler/job/merge", sch.merge_job),
    path("api/scheduler/preview", sch.preview),
    path("api/scheduler/suggest", sch.suggest),
    path("api/scheduler/suggest/apply", sch.suggest_apply),
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
    path("thin/events", thin.events),
    # Reports
    path("reports/", reports.board, name="reports"),
    path("api/report", reports.report),
    path("api/report/grafana/panels", reports.grafana_panels),
    path("api/report/grafana/data", reports.grafana_data),
    # Labor Budget (shift leads only)
    path("labor/", labor.page, name="labor"),
    path("budget/", labor.budget_only, name="budget"),
    path("supervisor/", supervisor.page, name="supervisor"),
    path("supervisor/down-batch", supervisor.down_batch),
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
    # Sensors & custom alarms
    path("cameras/", cameras.page, name="cameras"),
    path("cameras/do/<str:name>", cameras.do, name="cameras-do"),
    path("api/cameras/<int:pk>/frame", cameras.frame, name="camera-frame"),
    path("sensors/", sensors.page, name="sensors"),
    path("sensors/do/<str:name>", sensors.do, name="sensors-do"),
    path("api/sensors", sensors.api_push),
    # Packaging Kiosk: pallet tags
    path("packaging/", packkiosk.page, name="packaging-section"),
    path("stations/", stations.page, name="stations"),
    path("schedule/", calview.page, name="schedule"),
    path("schedule/do/<str:name>", calview.do),
    path("maintenance/reorder.csv", maintenance.reorder_csv),
    path("stations/do/<str:name>", stations.do),
    path("packaging/suggest", packkiosk.suggest),
    path("packaging/do/<str:name>", packkiosk.do, name="packaging-do"),
    path("packaging/tag/<int:pk>/", packkiosk.print_tag, name="packaging-tag"),
    path("api/labor/state", labor.state),
    path("api/labor/save", labor.save),
    path("api/labor/history", labor.history),
    path("api/labor/restore", labor.restore),
]

# every section's tabs at their own address: /admin/backup/, /supervisor/down/, /quality/trace/ …
from hmi.kadmin import TABS as _admin_tabs  # noqa: E402
from maintenance.views import TABS as _mt_tabs  # noqa: E402
from quality.views import TABS as _q_tabs  # noqa: E402
from sensors.views import TABS as _sn_tabs  # noqa: E402
from supervisor.views import TABS as _sv_tabs  # noqa: E402

urlpatterns += [
    path("admin/<slug:tab>/", tabbed(kv.admin_page, _admin_tabs, "/admin/")),
    path("supervisor/<slug:tab>/", tabbed(supervisor.page, _sv_tabs, "/supervisor/")),
    path("quality/<slug:tab>/", tabbed(quality.page, _q_tabs, "/quality/")),
    path("maintenance/<slug:tab>/", tabbed(maintenance.page, _mt_tabs, "/maintenance/")),
    path("sensors/<slug:tab>/", tabbed(sensors.page, _sn_tabs, "/sensors/")),
]
