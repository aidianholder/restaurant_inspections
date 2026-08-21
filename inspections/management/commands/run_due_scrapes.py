"""Fire any county scrapes that are due right now.

    ./manage.py run_due_scrapes
    ./manage.py run_due_scrapes --dry-run

Normally invoked by the recurring dispatcher registered with `install_scheduler`;
this is the manual entry point for testing or for recovering after downtime.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from inspections.scheduling import dispatch_due_scrapes, due_schedules


class Command(BaseCommand):
    help = "Queue scrapes for every county whose schedule is due."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Show what would be queued without queueing anything",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        due = list(due_schedules(now))

        if not due:
            self.stdout.write("Nothing due.")
            return

        if options["dry_run"]:
            self.stdout.write(f"{len(due)} schedule(s) due at {timezone.localtime(now):%Y-%m-%d %H:%M}:")
            for schedule in due:
                start, end = schedule.window_for(timezone.localtime(now).date())
                self.stdout.write(f"  {schedule.county:14} would scrape {start} to {end}")
            return

        result = dispatch_due_scrapes(now)
        for county in result["queued"]:
            self.stdout.write(self.style.SUCCESS(f"  queued   {county}"))
        for county in result["skipped"]:
            self.stdout.write(self.style.WARNING(f"  skipped  {county} (scrape already in flight)"))
        self.stdout.write(f"Done. queued={len(result['queued'])} skipped={len(result['skipped'])}")
