"""A machine with a counter makes parts only when its counter goes up; speed alone isn't parts made."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from hmi import engine
from hmi.models import ShiftRun
from plant.demo import seed_demo
from plant.models import Machine


class CounterTests(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.sh = ShiftRun.objects.get(line="Barbara", active=True)
        self.m = Machine.objects.get(name="Barbara")
        self.t = timezone.now().replace(second=0, microsecond=0) + timedelta(minutes=5)

    def run_minutes(self, samples):
        for i, s in enumerate(samples):
            engine.tick(self.m, s, self.t + timedelta(minutes=i))
        return ShiftRun.objects.get(pk=self.sh.pk).count

    def test_counter_decides(self):
        Machine.objects.filter(pk=self.m.pk).update(feed_count_field="cut_count")
        self.m.refresh_from_db()
        start = self.sh.count
        # running at speed, counter not moving: no parts
        n = self.run_minutes([{"ppm": 190, "count": 500}, {"ppm": 190, "count": 500}, {"ppm": 190, "count": 500}])
        self.assertEqual(n, start)
        # a minute with no counter value: still no guess from speed
        n = self.run_minutes([{"ppm": 190}])
        self.assertEqual(n, start)
        # the counter goes up: those are the parts
        n = self.run_minutes([{"ppm": 190, "count": 500}, {"ppm": 190, "count": 507}])
        self.assertEqual(n, start + 7)

    def test_no_counter_uses_speed(self):
        Machine.objects.filter(pk=self.m.pk).update(feed_count_field="")
        self.m.refresh_from_db()
        start = self.sh.count
        n = self.run_minutes([{"ppm": 300}] * 4)
        self.assertGreater(n, start)
