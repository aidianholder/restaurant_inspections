"""Extract violations from report PDFs already on disk.

    ./manage.py parse_reports              # only reports not yet parsed
    ./manage.py parse_reports --force      # re-parse everything
    ./manage.py parse_reports --county Pulaski

The website's observations overlay under-reports violations, so this recovers the
full set from the report itself. Newly scraped inspections are parsed during the
scrape; this is for reports collected before that existed, or after a parser fix.
"""

from django.core.management.base import BaseCommand
from django.db.models import Count

from inspections.ingest import parse_report_pdf
from inspections.models import Inspection


class Command(BaseCommand):
    help = "Parse stored inspection report PDFs and replace violations with their contents."

    def add_arguments(self, parser):
        parser.add_argument("--county", help="Limit to one county")
        parser.add_argument("--limit", type=int)
        parser.add_argument(
            "--force", action="store_true", help="Re-parse reports already processed"
        )

    def handle(self, *args, **options):
        inspections = (
            Inspection.objects.exclude(report_pdf="")
            .select_related("facility")
            .order_by("-date")
        )
        if not options["force"]:
            inspections = inspections.filter(report_parsed_at__isnull=True)
        if options["county"]:
            inspections = inspections.filter(facility__county=options["county"])
        if options["limit"]:
            inspections = inspections[: options["limit"]]

        total = inspections.count()
        if not total:
            self.stdout.write("No reports to parse.")
            return

        self.stdout.write(f"Parsing {total} report{'s' if total != 1 else ''}…")
        parsed = failed = gained = 0

        for inspection in inspections:
            before = inspection.violations.count()
            result = parse_report_pdf(inspection)
            if result is None:
                failed += 1
                self.stdout.write(self.style.WARNING(f"  {inspection.facility.name[:36]:38} unparsed"))
                continue
            parsed += 1
            gained += max(0, result - before)
            flag = self.style.SUCCESS(f"+{result - before}") if result > before else ""
            self.stdout.write(
                f"  {inspection.facility.name[:36]:38} {inspection.date}  "
                f"web={before} pdf={result} {flag}"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. parsed={parsed} failed={failed} additional violations recovered={gained}"
            )
        )
