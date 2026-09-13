"""Rebuild the denormalised latest-inspection columns on Facility.

    ./manage.py rebuild_latest_inspection
    ./manage.py rebuild_latest_inspection --county Pulaski
    ./manage.py rebuild_latest_inspection --check

Scrape runs refresh the facilities they touch, so this is for the cases they
don't cover: violations edited by hand in the admin, inspections deleted, or
simply proving the columns still agree with the underlying rows.
"""

from django.core.management.base import BaseCommand, CommandError

from inspections.counties import COUNTY_IDS
from inspections.latest_inspection import refresh
from inspections.models import Facility


class Command(BaseCommand):
    help = "Recompute Facility.latest_* from the stored inspections and violations."

    def add_arguments(self, parser):
        parser.add_argument("--county", help="Limit to one county.")
        parser.add_argument(
            "--check", action="store_true",
            help="Report how many rows are out of date without writing anything.",
        )

    def handle(self, *args, **options):
        county = options.get("county")
        facility_ids = None

        if county:
            if county not in COUNTY_IDS:
                raise CommandError(f"Unknown county {county!r}")
            facility_ids = list(
                Facility.objects.filter(county=county).values_list("pk", flat=True)
            )
            if not facility_ids:
                self.stdout.write(f"No facilities in {county} County.")
                return

        if options["check"]:
            # The refresh only touches rows whose values actually differ, so a
            # rolled-back run is an honest audit.
            from django.db import transaction

            with transaction.atomic():
                stale = refresh(facility_ids)
                transaction.set_rollback(True)
            if stale:
                self.stdout.write(
                    self.style.WARNING(f"{stale} facilities are out of date. Re-run without --check.")
                )
            else:
                self.stdout.write(self.style.SUCCESS("Every facility is up to date."))
            return

        changed = refresh(facility_ids)
        scope = f"{county} County" if county else "all counties"
        self.stdout.write(self.style.SUCCESS(f"Updated {changed} facilities ({scope})."))
