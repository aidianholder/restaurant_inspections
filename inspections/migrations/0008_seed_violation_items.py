"""Seed the 57 form items and link existing violations to them.

The seed is written to be re-runnable: it updates the state's official wording but
never touches `plain_description`, which is ours.
"""

from django.db import migrations

from inspections.violation_items import CATCH_ALL_NUMBER, VIOLATION_ITEMS

# The catch-all's official wording ("Other violations: Code Number must be noted
# on following page.") is an instruction to inspectors, not something to show a
# reader, so it ships with our own wording from the start.
CATCH_ALL_PLAIN = "Other violations"


def seed(apps, schema_editor):
    ViolationItem = apps.get_model("inspections", "ViolationItem")
    Violation = apps.get_model("inspections", "Violation")

    for number, section, subsection, description in VIOLATION_ITEMS:
        item, created = ViolationItem.objects.update_or_create(
            number=number,
            defaults={
                "section": section,
                "subsection": subsection,
                "official_description": description,
            },
        )
        if created and number == CATCH_ALL_NUMBER:
            item.plain_description = CATCH_ALL_PLAIN
            item.save(update_fields=["plain_description"])

    # Link violations already imported, matching on the number printed in the report.
    linked = 0
    for violation in Violation.objects.filter(item__isnull=True).exclude(item_number=""):
        if violation.item_number.isdigit():
            number = int(violation.item_number)
            if ViolationItem.objects.filter(number=number).exists():
                violation.item_id = number
                violation.save(update_fields=["item"])
                linked += 1
    if linked:
        print(f"  linked {linked} existing violations to form items")


def unseed(apps, schema_editor):
    Violation = apps.get_model("inspections", "Violation")
    Violation.objects.update(item=None)
    apps.get_model("inspections", "ViolationItem").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [("inspections", "0007_violation_item_lookup")]

    operations = [migrations.RunPython(seed, unseed)]
