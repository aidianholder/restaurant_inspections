"""The reader-facing establishment page.

A separate view and template from the staff `facility_detail`, not one template
with conditionals — so these tests assert on what the markup *cannot* contain,
which is the point of the split.
"""

import datetime as dt

from django.contrib.gis.geos import Point
from django.test import TestCase
from django.urls import reverse

from inspections.models import Facility, GeocodeSource, Inspection, PriorityLevel, Violation, fingerprint
from inspections.tests.test_output import cite, make_facility


def inspect(facility, date, *, kind="Routine", observations=0,
            scraped=True, parsed=False, levels=(), comments=None):
    inspection = Inspection.objects.create(
        facility=facility, date=date, inspection_type=kind,
        observation_count=observations,
        details_scraped_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) if scraped else None,
        report_parsed_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) if parsed else None,
    )
    for n, level in enumerate(levels):
        cite(inspection, level, (comments or [])[n] if comments else f"Observation {n}.", n)
    return inspection


class EstablishmentPageTests(TestCase):
    def setUp(self):
        self.facility = make_facility("TEST DINER")
        self.facility.location = Point(-92.3, 34.7, srid=4326)
        self.facility.geocode_source = GeocodeSource.ARKANSAS_GIS
        self.facility.geocode_matched_address = "1 Main St [PointAddress 98]"
        self.facility.save()
        self.inspection = inspect(
            self.facility, dt.date(2026, 8, 1), observations=2,
            levels=[PriorityLevel.PRIORITY, PriorityLevel.CORE],
            comments=["Milk held above 41 degrees.", "Floor tiles cracked."],
        )

    def get(self):
        return self.client.get(self.facility.get_absolute_url())

    def test_it_is_reachable_at_the_canonical_url(self):
        self.assertEqual(self.facility.get_absolute_url(), "/establishment/test-diner-little-rock/")
        r = self.get()
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "TEST DINER")

    def test_it_shows_the_inspectors_own_wording(self):
        r = self.get()
        self.assertContains(r, "Milk held above 41 degrees.")
        self.assertContains(r, "Floor tiles cracked.")

    def test_violations_are_grouped_by_category(self):
        r = self.get()
        body = r.content.decode()
        self.assertIn("Priority", body)
        self.assertIn("Core", body)
        self.assertLess(body.index("Milk held above"), body.index("Floor tiles cracked"))

    def test_it_explains_what_the_categories_mean(self):
        self.assertContains(self.get(), "Only categories in which an establishment was cited")

    # ---- what it must NOT contain ----

    def test_no_staff_navigation(self):
        r = self.get()
        for link in ("Retrieve data", "Output data", "/admin/", "/retrieve/", "/output/"):
            self.assertNotContains(r, link)

    def test_no_coordinates_or_geocoder(self):
        r = self.get()
        body = r.content.decode()
        for fragment in ("34.7", "-92.3", "Arkansas GIS", "geocode", "PointAddress",
                         "google.com/maps"):
            self.assertNotIn(fragment, body, fragment)

    def test_no_state_site_undercount_note(self):
        # The PDF is a superset of the website overlay, so a parsed report often
        # exceeds the state's own count. That discrepancy is ours to reconcile,
        # not something to explain to a reader.
        self.inspection.violations_source = "pdf"
        self.inspection.observation_count = 1
        self.inspection.save()
        Violation.objects.create(
            inspection=self.inspection, ordinal=9, priority_level="C",
            inspector_comments="A third thing.",
        )
        r = self.get()
        self.assertTrue(self.inspection.website_undercounted)
        self.assertNotContains(r, "state site shows")

    def test_it_does_not_extend_the_staff_base_template(self):
        r = self.get()
        self.assertNotContains(r, "Arkansas Health Inspections</a>")


class UnretrievedInspectionTests(TestCase):
    """Inspections whose details were never fetched are left out entirely.

    Publishing "1 violation" under a named business when the state recorded five
    and we simply have not fetched them is worse than saying nothing.
    """

    def setUp(self):
        self.facility = make_facility("PARTIAL DINER")

    def get(self):
        return self.client.get(self.facility.get_absolute_url())

    def test_an_inspection_with_unfetched_observations_is_hidden(self):
        inspect(self.facility, dt.date(2026, 7, 1), observations=5, scraped=False)
        inspect(self.facility, dt.date(2026, 8, 1), observations=1, scraped=True,
                levels=[PriorityLevel.PRIORITY], comments=["Retrieved observation."])
        r = self.get()
        self.assertContains(r, "August 1, 2026")
        self.assertNotContains(r, "July 1, 2026")

    def test_a_clean_inspection_is_kept_even_if_never_scraped(self):
        # observation_count 0 comes from the state's own results grid: it means
        # nothing was cited, not that we failed to look.
        inspect(self.facility, dt.date(2026, 6, 1), observations=0, scraped=False)
        r = self.get()
        self.assertContains(r, "June 1, 2026")
        self.assertContains(r, "No violations")

    def test_an_inspection_parsed_only_from_the_report_is_kept(self):
        inspect(self.facility, dt.date(2026, 5, 1), observations=3, scraped=False, parsed=True,
                levels=[PriorityLevel.CORE], comments=["From the PDF."])
        self.assertContains(self.get(), "From the PDF.")

    def test_a_facility_with_nothing_showable_says_so_rather_than_being_blank(self):
        inspect(self.facility, dt.date(2026, 7, 1), observations=5, scraped=False)
        r = self.get()
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "No inspection details are available")

    def test_never_retrieved_inspections_are_hidden_from_the_reader_only(self):
        hidden = inspect(self.facility, dt.date(2026, 7, 1), observations=5, scraped=False)
        staff = self.client.get(reverse("facility-detail", args=[self.facility.slug]))
        self.assertContains(staff, "July 1, 2026")
        self.assertNotContains(self.get(), "July 1, 2026")


class StaffPageStillIntactTests(TestCase):
    """The staff view keeps everything the reader page drops."""

    def setUp(self):
        self.facility = make_facility("STAFF VIEW")
        self.facility.location = Point(-92.3, 34.7, srid=4326)
        self.facility.geocode_source = GeocodeSource.ARKANSAS_GIS
        self.facility.save()
        inspect(self.facility, dt.date(2026, 8, 1), observations=1,
                levels=[PriorityLevel.PRIORITY], comments=["Something."])

    def test_it_keeps_coordinates_and_the_geocoder_used(self):
        r = self.client.get(reverse("facility-detail", args=[self.facility.slug]))
        self.assertContains(r, "34.70000")
        self.assertContains(r, "Arkansas GIS composite locator")

    def test_it_keeps_the_site_navigation(self):
        r = self.client.get(reverse("facility-detail", args=[self.facility.slug]))
        self.assertContains(r, "Retrieve data")

    def test_the_browse_list_links_to_the_staff_view_not_the_reader_page(self):
        from inspections.latest_inspection import refresh

        refresh()
        r = self.client.get(reverse("facility-list"))
        self.assertContains(r, reverse("facility-detail", args=[self.facility.slug]))
        self.assertNotContains(r, self.facility.get_absolute_url())


class DashboardLinkTests(TestCase):
    def test_the_dashboard_api_links_readers_to_the_public_page(self):
        from inspections.latest_inspection import refresh
        from inspections.models import Dashboard
        import json

        facility = make_facility("LINKED DINER")
        inspect(facility, dt.date(2026, 8, 1), observations=1,
                levels=[PriorityLevel.PRIORITY], comments=["x"])
        refresh()
        Dashboard.objects.create(slug="d", counties=["Pulaski"])

        data = json.loads(self.client.get(reverse("dashboard-rows", args=["d"])).content)
        self.assertEqual(data["rows"][0]["url"], "/establishment/linked-diner-little-rock/")
        self.assertNotIn("/facility/", data["rows"][0]["url"])
