from django.core.management.base import BaseCommand

from plant.demo import seed_demo


class Command(BaseCommand):
    help = "Replace everything with the demo plant (two pleaters, a running shift, a small MO backlog). For training only."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")

    def handle(self, *args, **opts):
        if not opts["yes"] and input("This deletes all MOs, records and people. Type DEMO to continue: ").strip() != "DEMO":
            self.stdout.write("Nothing changed.")
            return
        seed_demo(reset=True)
        self.stdout.write(self.style.SUCCESS("Demo plant loaded. PINs: J. Ortiz 1111 · M. Lee 2222 · K. Patel 3333 · Shift Lead 9999"))
