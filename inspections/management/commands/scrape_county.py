"""Run a scrape from the command line, without the web trigger or a worker.

    ./manage.py scrape_county --county Pulaski --from 2026-07-01 --to 2026-08-17
"""

import datetime as dt

from django.core.management.base import BaseCommand, CommandError

from inspections.counties import COUNTY_IDS
from inspections.ingest import run_scrape
from inspections.models import ScrapeRun


def parse_date(value):
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise CommandError(f"Dates must look like YYYY-MM-DD, got {value!r}")


class Command(BaseCommand):
    help = "Retrieve inspections for one county and date range."

    def add_arguments(self, parser):
        parser.add_argument("--county", required=True)
        parser.add_argument("--from", dest="date_from", required=True)
        parser.add_argument("--to", dest="date_to", required=True)

    def handle(self, *args, **options):
        county = options["county"]
        if county not in COUNTY_IDS:
            raise CommandError(f"Unknown county {county!r}")

        run = ScrapeRun.objects.create(
            county=county,
            date_from=parse_date(options["date_from"]),
            date_to=parse_date(options["date_to"]),
        )
        self.stdout.write(f"Starting run {run.pk}: {run}")
        status = run_scrape(run.pk)
        run.refresh_from_db()

        if status == ScrapeRun.Status.SUCCESS:
            self.stdout.write(self.style.SUCCESS(
                f"Done. pages={run.pages_fetched} facilities+{run.facilities_created} "
                f"inspections+{run.inspections_created} violations+{run.violations_created} "
                f"reports+{run.reports_downloaded}"
            ))
        else:
            raise CommandError(run.error or "Scrape failed")
