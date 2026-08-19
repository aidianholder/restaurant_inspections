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
