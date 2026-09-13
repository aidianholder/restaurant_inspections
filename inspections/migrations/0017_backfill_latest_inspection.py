"""Populate the denormalised latest-inspection columns for existing rows.

The columns ship with defaults of zero and null, which is indistinguishable from
"this facility has never been inspected" — so every existing facility has to be
computed once before anything reads them.

Re-runnable and non-destructive: it derives everything from the inspections and
violations already stored, so running it again is a no-op.
"""

from django.db import migrations

from inspections.latest_inspection import refresh


def backfill(apps, schema_editor):
    changed = refresh()
    if changed:
        print(f"  populated latest-inspection columns for {changed} facilities")


def unbackfill(apps, schema_editor):
    # Nothing to undo: 0016 drops the columns on the way back down.
    pass


class Migration(migrations.Migration):

    dependencies = [("inspections", "0016_latest_inspection_denormalised")]

    operations = [migrations.RunPython(backfill, unbackfill)]
