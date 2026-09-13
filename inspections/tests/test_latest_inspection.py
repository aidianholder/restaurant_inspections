"""The denormalised latest-inspection columns on Facility.

The load-bearing rule: the violation counts describe the **latest inspection
alone**, never the facility's history. A restaurant cited repeatedly in the past
and clean at its most recent visit must read zero.
"""

import datetime as dt

from django.test import TestCase

from inspections.latest_inspection import refresh
from inspections.models import Facility, Inspection, PriorityLevel, Violation, fingerprint
from inspections.tests.test_output import cite, make_facility


def inspect(facility, date, kind="Routine", violations=(), sequence=0):
    inspection = Inspection.objects.create(
        facility=facility, date=date, inspection_type=kind, sequence_within_day=sequence
    )
    for n, level in enumerate(violations):
        cite(inspection, level, f"Observation {n}.", n)
    return inspection


class LatestOnlyTests(TestCase):
    """Counts come from the newest inspection, not the whole history."""

    def test_a_facility_clean_today_reads_zero_however_bad_its_past(self):
        facility = make_facility("REFORMED DINER")
        inspect(facility, dt.date(2025, 3, 1),
                violations=[PriorityLevel.PRIORITY] * 5 + [PriorityLevel.CORE] * 4)
        inspect(facility, dt.date(2026, 8, 1), violations=[])
        refresh()

        facility.refresh_from_db()
        self.assertEqual(facility.latest_violation_total, 0)
        self.assertEqual(facility.latest_priority, 0)
        self.assertEqual(facility.latest_core, 0)
        # ...while the history is still there, and still counted as inspections.
        self.assertEqual(facility.inspection_count, 2)
        self.assertEqual(Violation.objects.filter(inspection__facility=facility).count(), 9)

    def test_counts_are_not_cumulative(self):
        facility = make_facility("STEADY CAFE")
        inspect(facility, dt.date(2025, 1, 1), violations=[PriorityLevel.PRIORITY] * 3)
        inspect(facility, dt.date(2026, 1, 1), violations=[PriorityLevel.PRIORITY] * 2)
        refresh()

        facility.refresh_from_db()
        self.assertEqual(facility.latest_violation_total, 2, "summed the history instead")
        self.assertEqual(facility.latest_priority, 2)

    def test_a_newly_cited_facility_reads_the_new_inspection(self):
        facility = make_facility("SLIPPED CAFE")
        inspect(facility, dt.date(2025, 1, 1), violations=[])
        refresh()
        facility.refresh_from_db()
        self.assertEqual(facility.latest_violation_total, 0)

        inspect(facility, dt.date(2026, 6, 1),
                violations=[PriorityLevel.PRIORITY, PriorityLevel.CORE])
        refresh()
        facility.refresh_from_db()
        self.assertEqual(facility.latest_violation_total, 2)
        self.assertEqual(facility.latest_inspection_date, dt.date(2026, 6, 1))


class ValuesTests(TestCase):
    def test_every_column_is_populated(self):
        facility = make_facility("FULL HOUSE")
        inspection = inspect(
            facility, dt.date(2026, 5, 4), kind="Follow-up",
            violations=[PriorityLevel.PRIORITY, PriorityLevel.PRIORITY,
                        PriorityLevel.PRIORITY_FOUNDATION, PriorityLevel.CORE],
        )
        refresh()

        facility.refresh_from_db()
        self.assertEqual(facility.latest_inspection_id, inspection.pk)
        self.assertEqual(facility.latest_inspection_date, dt.date(2026, 5, 4))
        self.assertEqual(facility.latest_inspection_type, "Follow-up")
        self.assertEqual(facility.latest_violation_total, 4)
        self.assertEqual(facility.latest_priority, 2)
        self.assertEqual(facility.latest_priority_foundation, 1)
        self.assertEqual(facility.latest_core, 1)
        self.assertEqual(facility.inspection_count, 1)

    def test_the_breakdown_never_exceeds_the_total(self):
        facility = make_facility("UNCATEGORISED")
        inspect(facility, dt.date(2026, 5, 4),
                violations=[PriorityLevel.PRIORITY, "", ""])
        refresh()

        facility.refresh_from_db()
        self.assertEqual(facility.latest_violation_total, 3)
        self.assertEqual(facility.latest_priority, 1)
        # The two uncategorised ones count toward the total and no category.
        self.assertEqual(
            facility.latest_priority + facility.latest_priority_foundation + facility.latest_core, 1
        )

    def test_same_day_inspections_break_the_tie_the_way_the_rest_of_the_app_does(self):
        facility = make_facility("TWICE IN A DAY")
        inspect(facility, dt.date(2026, 5, 4), kind="Routine",
                violations=[PriorityLevel.PRIORITY], sequence=0)
        second = inspect(facility, dt.date(2026, 5, 4), kind="Follow-up",
                         violations=[PriorityLevel.CORE, PriorityLevel.CORE], sequence=1)
        refresh()

        facility.refresh_from_db()
        self.assertEqual(facility.latest_inspection_id, second.pk)
        self.assertEqual(facility.latest_violation_total, 2)

    def test_a_facility_with_no_inspections_stays_empty(self):
        facility = make_facility("NEVER VISITED")
        refresh()
        facility.refresh_from_db()
        self.assertIsNone(facility.latest_inspection_id)
        self.assertIsNone(facility.latest_inspection_date)
        self.assertEqual(facility.latest_violation_total, 0)
        self.assertEqual(facility.inspection_count, 0)


class RefreshBehaviourTests(TestCase):
    def test_it_is_idempotent(self):
        facility = make_facility("STABLE")
        inspect(facility, dt.date(2026, 5, 4), violations=[PriorityLevel.PRIORITY])
        self.assertEqual(refresh(), 1)
        self.assertEqual(refresh(), 0, "rewrote rows that were already correct")

    def test_it_can_be_scoped_to_particular_facilities(self):
        a = make_facility("SCOPED A", street="1 A St")
        b = make_facility("SCOPED B", street="2 B St")
        inspect(a, dt.date(2026, 5, 4), violations=[PriorityLevel.PRIORITY])
        inspect(b, dt.date(2026, 5, 4), violations=[PriorityLevel.CORE])

        self.assertEqual(refresh([a.pk]), 1)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(a.latest_violation_total, 1)
        self.assertEqual(b.latest_violation_total, 0, "refreshed a facility outside the scope")

    def test_an_empty_scope_does_nothing(self):
        facility = make_facility("UNTOUCHED")
        inspect(facility, dt.date(2026, 5, 4), violations=[PriorityLevel.PRIORITY])
        self.assertEqual(refresh([]), 0)
        facility.refresh_from_db()
        self.assertEqual(facility.latest_violation_total, 0)

    def test_deleting_the_last_inspection_resets_the_columns(self):
        # The reason the UPDATE self-joins rather than filtering: a facility
        # whose inspections all vanished has to be cleared, not skipped.
        facility = make_facility("EMPTIED")
        inspection = inspect(facility, dt.date(2026, 5, 4), violations=[PriorityLevel.PRIORITY])
        refresh()
        inspection.delete()
        refresh()

        facility.refresh_from_db()
        self.assertIsNone(facility.latest_inspection_id)
        self.assertIsNone(facility.latest_inspection_date)
        self.assertEqual(facility.latest_violation_total, 0)
        self.assertEqual(facility.inspection_count, 0)

    def test_removing_violations_lowers_the_count(self):
        facility = make_facility("CORRECTED")
        inspection = inspect(facility, dt.date(2026, 5, 4),
                             violations=[PriorityLevel.PRIORITY, PriorityLevel.CORE])
        refresh()
        inspection.violations.filter(priority_level=PriorityLevel.CORE).delete()
        refresh()

        facility.refresh_from_db()
        self.assertEqual(facility.latest_violation_total, 1)
        self.assertEqual(facility.latest_core, 0)


class ManagementCommandTests(TestCase):
    def test_check_reports_without_writing(self):
        from io import StringIO
        from django.core.management import call_command

        facility = make_facility("DRIFTED")
        inspect(facility, dt.date(2026, 5, 4), violations=[PriorityLevel.PRIORITY])

        out = StringIO()
        call_command("rebuild_latest_inspection", "--check", stdout=out)
        self.assertIn("out of date", out.getvalue())

        facility.refresh_from_db()
        self.assertEqual(facility.latest_violation_total, 0, "--check wrote to the database")

    def test_it_rebuilds(self):
        from io import StringIO
        from django.core.management import call_command

        facility = make_facility("REBUILT")
        inspect(facility, dt.date(2026, 5, 4), violations=[PriorityLevel.PRIORITY])
        call_command("rebuild_latest_inspection", stdout=StringIO())

        facility.refresh_from_db()
        self.assertEqual(facility.latest_violation_total, 1)

    def test_it_can_be_scoped_to_a_county(self):
        from io import StringIO
        from django.core.management import call_command

        here = make_facility("IN SCOPE", street="1 A St")
        elsewhere = Facility.objects.create(
            name="OUT OF SCOPE", fingerprint=fingerprint("OUT", "2 B St", "71923"),
            street="2 B St", city="Arkadelphia", state="AR", zip_code="71923", county="Clark",
        )
        inspect(here, dt.date(2026, 5, 4), violations=[PriorityLevel.PRIORITY])
        inspect(elsewhere, dt.date(2026, 5, 4), violations=[PriorityLevel.PRIORITY])

        call_command("rebuild_latest_inspection", "--county", "Pulaski", stdout=StringIO())
        here.refresh_from_db(); elsewhere.refresh_from_db()
        self.assertEqual(here.latest_violation_total, 1)
        self.assertEqual(elsewhere.latest_violation_total, 0)


class AdminEditTests(TestCase):
    """Hand-editing in the admin is the one realistic way these columns go stale.

    Scrape runs rebuild what they touch; nothing else does, so the paths a person
    can reach without a run are hooked directly.
    """

    def setUp(self):
        from django.contrib.auth.models import User

        self.client.force_login(User.objects.create_superuser("ed", "e@x.com", "pw"))
        self.facility = make_facility("ADMIN EDITED")
        self.inspection = inspect(
            self.facility, dt.date(2026, 5, 4), violations=[PriorityLevel.PRIORITY]
        )
        refresh()

    def change_url(self):
        from django.urls import reverse

        return reverse("admin:inspections_inspection_change", args=[self.inspection.pk])

    def form_data(self, violations):
        """The inspection change form, with its violation inline formset."""
        data = {
            "facility": str(self.facility.pk),
            "date": "2026-05-04",
            "inspection_type": "Routine",
            "sequence_within_day": "0",
            "observation_count": "0",
            "report_source_url": "",
            "source_inspection_id": "",
            "violations_source": "",
            "violations-TOTAL_FORMS": str(len(violations)),
            "violations-INITIAL_FORMS": str(self.inspection.violations.count()),
            "violations-MIN_NUM_FORMS": "0",
            "violations-MAX_NUM_FORMS": "1000",
        }
        for i, (pk, level, delete) in enumerate(violations):
            data[f"violations-{i}-id"] = str(pk) if pk else ""
            data[f"violations-{i}-inspection"] = str(self.inspection.pk)
            data[f"violations-{i}-ordinal"] = str(i)
            data[f"violations-{i}-item_number"] = ""
            data[f"violations-{i}-code"] = ""
            data[f"violations-{i}-priority_level"] = level
            data[f"violations-{i}-inspector_comments"] = "x"
            data[f"violations-{i}-correct_by"] = ""
            data[f"violations-{i}-source"] = "pdf"
            if delete:
                data[f"violations-{i}-DELETE"] = "on"
        return data

    def test_adding_a_violation_inline_updates_the_counts(self):
        existing = self.inspection.violations.first()
        self.client.post(self.change_url(), self.form_data([
            (existing.pk, PriorityLevel.PRIORITY, False),
            (None, PriorityLevel.CORE, False),
        ]))
        self.facility.refresh_from_db()
        self.assertEqual(self.facility.latest_violation_total, 2)
        self.assertEqual(self.facility.latest_core, 1)

    def test_deleting_a_violation_inline_updates_the_counts(self):
        existing = self.inspection.violations.first()
        self.client.post(self.change_url(), self.form_data([
            (existing.pk, PriorityLevel.PRIORITY, True),
        ]))
        self.facility.refresh_from_db()
        self.assertEqual(self.facility.latest_violation_total, 0)
        self.assertEqual(self.facility.latest_priority, 0)

    def test_deleting_an_inspection_falls_back_to_the_previous_one(self):
        from django.urls import reverse

        older = inspect(self.facility, dt.date(2025, 1, 1),
                        violations=[PriorityLevel.CORE, PriorityLevel.CORE])
        refresh()
        self.facility.refresh_from_db()
        self.assertEqual(self.facility.latest_inspection_id, self.inspection.pk)

        self.client.post(
            reverse("admin:inspections_inspection_delete", args=[self.inspection.pk]),
            {"post": "yes"},
        )
        self.facility.refresh_from_db()
        self.assertEqual(self.facility.latest_inspection_id, older.pk)
        self.assertEqual(self.facility.latest_violation_total, 2)
        self.assertEqual(self.facility.latest_core, 2)
