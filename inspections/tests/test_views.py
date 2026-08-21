import datetime as dt

from django.test import TestCase
from django.urls import reverse

from inspections.models import Facility, Inspection, ScrapeRun, Violation, fingerprint


class ViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.facility = Facility.objects.create(
            name="TEST DINER",
            fingerprint=fingerprint("TEST DINER", "1 Main St", "72201"),
            source_key="999",
            street="1 Main St",
            city="Little Rock",
            zip_code="72201",
            county="Pulaski",
            phone="501-555-0100",
        )
        cls.inspection = Inspection.objects.create(
            facility=cls.facility,
            date=dt.date(2026, 8, 1),
            inspection_type="Routine",
            observation_count=1,
            details_scraped_at=dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc),
        )
        Violation.objects.create(
            inspection=cls.inspection,
            ordinal=0,
            code="20 CAR 192-501 (f)",
            code_explanation="Cold Holding",
            inspector_comments="Milk held above 41 degrees.",
        )

    def test_facility_list_shows_retrieved_data(self):
        r = self.client.get(reverse("facility-list"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "TEST DINER")

    def test_facility_list_filters_by_county(self):
        r = self.client.get(reverse("facility-list"), {"county": "Clark"})
        self.assertNotContains(r, "TEST DINER")

    def test_facility_detail_shows_violation_text(self):
        r = self.client.get(self.facility.get_absolute_url())
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Milk held above 41 degrees.")
        self.assertContains(r, "20 CAR 192-501 (f)")

    def test_scrape_form_renders(self):
        r = self.client.get(reverse("scrape-request"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Pulaski")

    def test_reversed_date_range_is_rejected(self):
        r = self.client.post(
            reverse("scrape-request"),
            {"county": "Clark", "date_from": "2026-08-17", "date_to": "2026-08-10"},
        )
        self.assertContains(r, "must come before")
        self.assertEqual(ScrapeRun.objects.count(), 0)

    def test_oversized_range_is_rejected(self):
        r = self.client.post(
            reverse("scrape-request"),
            {"county": "Clark", "date_from": "2020-01-01", "date_to": "2026-08-10"},
        )
        self.assertContains(r, "days or fewer")
        self.assertEqual(ScrapeRun.objects.count(), 0)

    def test_status_endpoint_reports_progress(self):
        run = ScrapeRun.objects.create(
            county="Clark", date_from=dt.date(2026, 8, 1), date_to=dt.date(2026, 8, 10),
            status=ScrapeRun.Status.RUNNING, progress_note="Page 1 of 3", pages_fetched=1,
        )
        r = self.client.get(reverse("scrape-status", args=[run.pk]))
        data = r.json()
        self.assertTrue(data["active"])
        self.assertEqual(data["note"], "Page 1 of 3")


class FacilityModelTests(TestCase):
    def test_slug_is_unique_across_same_named_facilities(self):
        a = Facility.objects.create(
            name="WAFFLE HOUSE", city="Little Rock",
            fingerprint=fingerprint("WAFFLE HOUSE", "1 A St", "72201"),
        )
        b = Facility.objects.create(
            name="WAFFLE HOUSE", city="Little Rock",
            fingerprint=fingerprint("WAFFLE HOUSE", "2 B St", "72202"),
        )
        self.assertNotEqual(a.slug, b.slug)

    def test_same_inspection_cannot_be_stored_twice(self):
        from django.db.utils import IntegrityError

        f = Facility.objects.create(name="X", fingerprint=fingerprint("X", "", ""))
        Inspection.objects.create(facility=f, date=dt.date(2026, 1, 1), inspection_type="Routine")
        with self.assertRaises(IntegrityError):
            Inspection.objects.create(
                facility=f, date=dt.date(2026, 1, 1), inspection_type="Routine"
            )


class BrowseDateFilterTests(TestCase):
    """The browse date filter targets each facility's most recent inspection."""

    @classmethod
    def setUpTestData(cls):
        def facility(name, *dates):
            f = Facility.objects.create(
                name=name, city="Little Rock", county="Pulaski",
                fingerprint=fingerprint(name, name, "72201"),
            )
            for d in dates:
                Inspection.objects.create(facility=f, date=d, inspection_type="Routine")
            return f

        # OLD's latest is in 2024 even though it also has an old 2020 inspection.
        cls.old = facility("OLD DINER", dt.date(2020, 5, 1), dt.date(2024, 3, 10))
        cls.mid = facility("MID DINER", dt.date(2026, 3, 15))
        cls.recent = facility("RECENT DINER", dt.date(2026, 8, 10))

    def names(self, response):
        body = response.content.decode()
        return {n for n in ("OLD DINER", "MID DINER", "RECENT DINER") if n in body}

    def test_no_dates_returns_everything(self):
        r = self.client.get(reverse("facility-list"))
        self.assertEqual(self.names(r), {"OLD DINER", "MID DINER", "RECENT DINER"})

    def test_start_date_only(self):
        r = self.client.get(reverse("facility-list"), {"date_from": "2026-01-01"})
        self.assertEqual(self.names(r), {"MID DINER", "RECENT DINER"})

    def test_end_date_only(self):
        r = self.client.get(reverse("facility-list"), {"date_to": "2026-04-01"})
        self.assertEqual(self.names(r), {"OLD DINER", "MID DINER"})

    def test_both_dates(self):
        r = self.client.get(
            reverse("facility-list"), {"date_from": "2026-01-01", "date_to": "2026-04-01"}
        )
        self.assertEqual(self.names(r), {"MID DINER"})

    def test_bounds_are_inclusive(self):
        r = self.client.get(
            reverse("facility-list"), {"date_from": "2026-03-15", "date_to": "2026-03-15"}
        )
        self.assertEqual(self.names(r), {"MID DINER"})

    def test_matches_latest_inspection_not_any_inspection(self):
        """OLD DINER has a 2020 inspection, but its latest is 2024 — so a 2020
        window must not match it."""
        r = self.client.get(
            reverse("facility-list"), {"date_from": "2020-01-01", "date_to": "2020-12-31"}
        )
        self.assertEqual(self.names(r), set())

    def test_reversed_range_is_rejected(self):
        r = self.client.get(
            reverse("facility-list"), {"date_from": "2026-08-01", "date_to": "2026-01-01"}
        )
        self.assertContains(r, "must come before")

    def test_dates_combine_with_the_other_filters(self):
        r = self.client.get(
            reverse("facility-list"), {"date_from": "2026-01-01", "q": "RECENT"}
        )
        self.assertEqual(self.names(r), {"RECENT DINER"})
        r = self.client.get(
            reverse("facility-list"), {"date_from": "2026-01-01", "county": "Clark"}
        )
        self.assertEqual(self.names(r), set())

    def test_filter_values_persist_in_the_form(self):
        r = self.client.get(reverse("facility-list"), {"date_from": "2026-01-01"})
        self.assertContains(r, 'value="2026-01-01"')

    def test_invalid_date_does_not_error(self):
        r = self.client.get(reverse("facility-list"), {"date_from": "not-a-date"})
        self.assertEqual(r.status_code, 200)


class BrowseLatestInspectionTests(TestCase):
    """The browse row describes the *latest* inspection, not the facility overall."""

    @classmethod
    def setUpTestData(cls):
        cls.facility = Facility.objects.create(
            name="TWO VISITS", city="Little Rock", county="Pulaski",
            fingerprint=fingerprint("TWO VISITS", "1 A St", "72201"),
        )
        # An older Routine with violations…
        old = Inspection.objects.create(
            facility=cls.facility, date=dt.date(2026, 1, 5), inspection_type="Routine"
        )
        for i, level in enumerate(["P", "P", "C"]):
            Violation.objects.create(inspection=old, ordinal=i, priority_level=level, code=f"old-{i}")
        # …then a more recent Follow-up with a different mix.
        cls.latest = Inspection.objects.create(
            facility=cls.facility, date=dt.date(2026, 6, 20), inspection_type="Follow-up"
        )
        for i, level in enumerate(["P", "PF", "PF", "C"]):
            Violation.objects.create(
                inspection=cls.latest, ordinal=i, priority_level=level, code=f"new-{i}"
            )

    def row(self):
        response = self.client.get(reverse("facility-list"))
        return response, response.context["facilities"][0]

    def test_shows_the_latest_inspection_type(self):
        response, facility = self.row()
        self.assertEqual(facility.latest_inspection_type, "Follow-up")
        self.assertContains(response, "Follow-up")

    def test_counts_come_from_the_latest_inspection_only(self):
        """The older visit had 2 Priority violations; the latest had 1."""
        _, facility = self.row()
        self.assertEqual(facility.latest_priority, 1)
        self.assertEqual(facility.latest_priority_foundation, 2)
        self.assertEqual(facility.latest_core, 1)
        self.assertEqual(facility.latest_violation_total, 4)

    def test_all_inspections_still_counted_separately(self):
        _, facility = self.row()
        self.assertEqual(facility.inspection_count, 2)

    def test_facility_with_a_clean_latest_inspection_shows_none(self):
        clean = Facility.objects.create(
            name="SPOTLESS", city="Little Rock", county="Pulaski",
            fingerprint=fingerprint("SPOTLESS", "2 B St", "72201"),
        )
        Inspection.objects.create(
            facility=clean, date=dt.date(2026, 7, 1), inspection_type="Routine"
        )
        response = self.client.get(reverse("facility-list"))
        row = next(f for f in response.context["facilities"] if f.name == "SPOTLESS")
        self.assertEqual(row.latest_violation_total, 0)
        self.assertContains(response, "None")

    def test_violation_without_a_priority_level_still_counts_in_the_total(self):
        Violation.objects.create(
            inspection=self.latest, ordinal=99, priority_level="", code="unknown"
        )
        _, facility = self.row()
        self.assertEqual(facility.latest_violation_total, 5)
        self.assertEqual(
            facility.latest_priority + facility.latest_priority_foundation + facility.latest_core, 4
        )

    def test_same_day_inspections_resolve_deterministically(self):
        """Two inspections share a date; the later sequence must win, every time."""
        same_day = Inspection.objects.create(
            facility=self.facility, date=dt.date(2026, 6, 20),
            inspection_type="Complaint", sequence_within_day=1,
        )
        for _ in range(3):
            _, facility = self.row()
            self.assertEqual(facility.latest_inspection_type, "Complaint")
            self.assertEqual(facility.latest_inspection_id, same_day.pk)

    def test_opening_type_is_displayed(self):
        Inspection.objects.create(
            facility=self.facility, date=dt.date(2026, 8, 1), inspection_type="Opening"
        )
        response, facility = self.row()
        self.assertEqual(facility.latest_inspection_type, "Opening")
