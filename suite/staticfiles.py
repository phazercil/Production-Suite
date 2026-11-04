"""Static file addresses that change with every build.

The screens' style sheets and scripts are served at fixed addresses (/static/hmi/kiosk.css), and browsers keep a
copy of them for a while. After an update a screen could then show the new page with the old styles. Adding the
build's revision to every address (kiosk.css?v=11) makes browsers fetch the new files right after an update and
keep using their copy in between.
"""
from django.conf import settings
from django.contrib.staticfiles.storage import StaticFilesStorage


class VersionedStaticFilesStorage(StaticFilesStorage):
    def url(self, name):
        u = super().url(name)
        rev = settings.SUITE.get("REVISION")
        return f"{u}{'&' if '?' in u else '?'}v={rev}" if rev else u
