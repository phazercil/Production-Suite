from django.core.management.base import BaseCommand

from plant.demo import ensure_basics
from plant.models import Machine, Person, Shift


class Command(BaseCommand):
    help = "First-time setup for a live plant: loads the built-in cost sheet and creates a shift lead login if there is none."

    def add_arguments(self, parser):
        parser.add_argument("--lead-pin", default="9999", help="PIN for the first shift lead (change it afterwards)")

    def handle(self, *args, **opts):
        ensure_basics()
        if not Shift.objects.exists():
            Shift.objects.create(name="1st shift", start="06:00", end="14:30", days=[1, 2, 3, 4, 5])
        if not Person.objects.filter(role="admin", active=True).exists():
            p = Person.objects.create(name="Shift Lead", pin=opts["lead_pin"], role="admin")
            p.trained.set(Machine.objects.all())
            self.stdout.write(f"Created 'Shift Lead' with PIN {opts['lead_pin']}. Change it on the Team page.")
        self.stdout.write(self.style.SUCCESS("Plant ready. Add machines in Scheduler › Lines & Shifts."))
