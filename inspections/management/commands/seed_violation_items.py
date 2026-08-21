"""Refresh the 57 form items from violation_items.py.

    ./manage.py seed_violation_items
    ./manage.py seed_violation_items --relink   # also re-link existing violations

The initial seed runs as a migration; this is for when the state revises the form
and the module is regenerated. Reader-facing `plain_description` text is ours and
is never overwritten.
"""

from django.core.management.base import BaseCommand

from inspections.models import Violation, ViolationItem
from inspections.violation_items import VIOLATION_ITEMS


class Command(BaseCommand):
    help = "Load or refresh the inspection form's numbered items."

    def add_arguments(self, parser):
        parser.add_argument(
            "--relink", action="store_true",
            help="Re-resolve every violation's item number after seeding",
        )

    def handle(self, *args, **options):
        created = updated = 0
        for number, section, subsection, description in VIOLATION_ITEMS:
            _, was_created = ViolationItem.objects.update_or_create(
                number=number,
                defaults={
                    "section": section,
                    "subsection": subsection,
                    "official_description": description,
                },
            )
            created += was_created
            updated += not was_created

        self.stdout.write(f"items created={created} updated={updated}")

        if options["relink"]:
            known = set(ViolationItem.objects.values_list("number", flat=True))
            linked = cleared = 0
            for violation in Violation.objects.exclude(item_number=""):
                number = int(violation.item_number) if violation.item_number.isdigit() else None
                resolved = number if number in known else None
                if violation.item_id != resolved:
                    violation.item_id = resolved
                    violation.save(update_fields=["item"])
                    linked += resolved is not None
                    cleared += resolved is None
            self.stdout.write(f"relinked={linked} cleared={cleared}")

        self.stdout.write(self.style.SUCCESS("Done."))
