"""Register the recurring dispatcher with django-q.

    ./manage.py install_scheduler
    ./manage.py install_scheduler --every 5

Creates (or updates) a single django-q Schedule that calls the dispatcher every
few minutes. The per-county timing lives in ScrapeSchedule rows, not here — this
is just the heartbeat, so it only ever needs installing once.

The interval sets the worst-case lateness: at the default of 10 minutes, a job set
for 2:00am starts by 2:10 at the latest.
"""

from django.core.management.base import BaseCommand
from django_q.models import Schedule

TASK = "inspections.tasks.dispatch_due_scrapes_task"
NAME = "dispatch-due-scrapes"


class Command(BaseCommand):
    help = "Install the recurring scrape dispatcher."

    def add_arguments(self, parser):
        parser.add_argument("--every", type=int, default=10, help="Minutes between checks")

    def handle(self, *args, **options):
        minutes = options["every"]
        schedule, created = Schedule.objects.update_or_create(
            name=NAME,
            defaults={
                "func": TASK,
                "schedule_type": Schedule.MINUTES,
                "minutes": minutes,
                "repeats": -1,          # forever
            },
        )
        verb = "Installed" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(f"{verb} dispatcher '{NAME}' — checks every {minutes} minutes.")
        )
        self.stdout.write("The worker must be running for schedules to fire: manage.py qcluster")
