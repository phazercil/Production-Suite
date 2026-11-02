"""Show or change the plant network (the addresses screens may connect from).

    python manage.py network                       show the setting
    python manage.py network --plain               just the ranges, one per line (used by the installer)
    python manage.py network --set 192.168.100.0/24 [10.20.0.0/16 ...]
    python manage.py network --sync-firewall       re-apply the setting to Windows Firewall

Run it on the server itself if the setting ever locks every screen out; the server is always allowed.
"""
from django.core.management.base import BaseCommand, CommandError

from plant import netsafety


class Command(BaseCommand):
    help = "Show or change the plant network that screens may connect from."

    def add_arguments(self, parser):
        parser.add_argument("--set", nargs="+", metavar="RANGE", help="New plant network ranges, e.g. 192.168.100.0/24")
        parser.add_argument("--plain", action="store_true", help="Print only the ranges, one per line")
        parser.add_argument("--sync-firewall", action="store_true", help="Re-apply the setting to Windows Firewall")

    def handle(self, *args, **o):
        if o["set"]:
            try:
                nets = netsafety.parse_networks(o["set"])
            except netsafety.BadNetwork as e:
                raise CommandError(str(e))
            ok, msg = netsafety.save(nets, by="server command line")
            self.stdout.write(self.style.SUCCESS("Plant network saved: " + ", ".join(map(str, nets))))
            self._fw(ok, msg)
            return
        netsafety.invalidate()
        nets, source = netsafety.current(with_source=True)
        if o["plain"]:
            for n in nets:
                self.stdout.write(str(n))
            return
        if o["sync_firewall"]:
            self._fw(*netsafety.sync_and_record(nets))
            return
        where = {"settings": "set in Operator Kiosk > Admin > Network", "installer": "set by the installer (PLANT_NETWORKS in .env)",
                 "default": "the standard setting"}[source]
        self.stdout.write(f"Plant network ({where}):")
        for n in nets:
            self.stdout.write(f"  {n}   {netsafety.describe(n)}")
        self.stdout.write("  This server itself is always allowed.")

    def _fw(self, ok, msg):
        if ok is False:
            self.stdout.write(self.style.WARNING(msg))
        else:
            self.stdout.write(msg)
