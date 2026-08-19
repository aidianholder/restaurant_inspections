"""Backfill locations for facilities that don't have one.

    ./manage.py geocode_facilities
    ./manage.py geocode_facilities --county Pulaski --limit 50
    ./manage.py geocode_facilities --force        # re-geocode everything

Facilities located during a scrape need no backfill; this is for rows that were
imported before geocoding existed, or whose lookup failed at the time.
"""

import requests
from django.core.management.base import BaseCommand

from inspections.geocoding import locate_facility
from inspections.models import Facility


class Command(BaseCommand):
    help = "Look up and store locations for facilities missing one."

    def add_arguments(self, parser):
        parser.add_argument("--county", help="Limit to one county")
        parser.add_argument("--limit", type=int, help="Stop after this many facilities")
        parser.add_argument(
            "--force", action="store_true", help="Re-geocode facilities that already have a location"
        )

    def handle(self, *args, **options):
        facilities = Facility.objects.all().order_by("name")
        if not options["force"]:
            facilities = facilities.filter(location__isnull=True)
        if options["county"]:
            facilities = facilities.filter(county=options["county"])
        if options["limit"]:
            facilities = facilities[: options["limit"]]

        total = facilities.count()
        if not total:
            self.stdout.write("Nothing to geocode.")
            return

        self.stdout.write(f"Geocoding {total} facilit{'y' if total == 1 else 'ies'}…")
        counts = {"arkansas_gis": 0, "census": 0, "failed": 0}

        # One session for all lookups: connection reuse makes a real difference
        # across hundreds of addresses.
        with requests.Session() as session:
            for facility in facilities:
                source = locate_facility(facility, force=options["force"], session=session)
                if source:
                    counts[source] = counts.get(source, 0) + 1
                    self.stdout.write(
                        f"  {facility.name[:40]:42} {facility.latitude:.5f}, "
                        f"{facility.longitude:.5f}  [{source}]"
                    )
                else:
                    counts["failed"] += 1
                    self.stdout.write(
                        self.style.WARNING(f"  {facility.name[:40]:42} no match")
                    )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. arkansas_gis={counts['arkansas_gis']} census={counts['census']} "
                f"failed={counts['failed']}"
            )
        )
