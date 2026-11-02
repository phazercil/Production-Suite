from django.core.management.base import BaseCommand

from hmi.ticker import loop


class Command(BaseCommand):
    help = "Run the plant ticker: machine readings every minute, timed S2K/people/Paycom pulls and the webhook outbox."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=5, help="Seconds between checks (default 5)")

    def handle(self, *args, **opts):
        self.stdout.write("Plant ticker running. Ctrl+C to stop.")
        loop(interval=opts["interval"])
