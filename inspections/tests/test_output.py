import datetime as dt

from django.test import TestCase
from django.urls import reverse

from inspections.models import Facility, Inspection, PriorityLevel, Violation, fingerprint
from inspections.output import build_export


def make_facility(name, **kwargs):
    return Facility.objects.create(
        name=name,
        fingerprint=fingerprint(name, kwargs.get("street", ""), kwargs.get("zip_code", "")),
        county=kwargs.pop("county", "Pulaski"),
        street=kwargs.pop("street", "1 Main St"),
        city=kwargs.pop("city", "Little Rock"),
        zip_code=kwargs.pop("zip_code", "72201"),
        **kwargs,
    )


def cite(inspection, level, comment, ordinal=0):
    return Violation.objects.create(
        inspection=inspection,
        ordinal=ordinal,
        priority_level=level,
        inspector_comments=comment,
    )


class ExportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.zebra = make_facility("ZEBRA CAFE", street="2 Oak St")
        cls.apple = make_facility("APPLE GRILL", street="3 Elm St")
        cls.clean = make_facility("CLEAN PLATE", street="4 Pine St")

        cls.first = Inspection.objects.create(
            facility=cls.zebra, date=dt.date(2026, 8, 3), inspection_type="Routine",
            report_source_url="https://example.gov/report/1.pdf",
        )
        cite(cls.first, PriorityLevel.CORE, "Floor tiles cracked.", 0)
        cite(cls.first, PriorityLevel.PRIORITY, "Milk held above 41 degrees.", 1)

        cls.second = Inspection.objects.create(
            facility=cls.apple, date=dt.date(2026, 8, 3), inspection_type="Follow-up"
        )
        cite(cls.second, PriorityLevel.PRIORITY_FOUNDATION, "No thermometer available.")

        cls.later = Inspection.objects.create(
            facility=cls.zebra, date=dt.date(2026, 8, 5), inspection_type="Routine"
        )
        cite(cls.later, PriorityLevel.PRIORITY, "Employee did not wash hands.")

        # No violations at all: must not appear.
        Inspection.objects.create(
            facility=cls.clean, date=dt.date(2026, 8, 3), inspection_type="Routine"
        )

    def export(self, **kwargs):
        return build_export(
            kwargs.pop("county", "Pulaski"),
            kwargs.pop("date_from", dt.date(2026, 8, 1)),
            kwargs.pop("date_to", dt.date(2026, 8, 31)),
            **kwargs,
        )

    def test_boilerplate_leads_the_output(self):
        html = self.export().html
        self.assertTrue(html.startswith("<p>Violations marked as priority contribute directly"))
        self.assertIn("Only categories in which an establishment was cited are listed.", html)

    def test_days_are_headed_and_ordered(self):
        html = self.export().html
        self.assertLess(html.index("<h2>August 3, 2026</h2>"), html.index("<h2>August 5, 2026</h2>"))

    def test_establishments_are_alphabetical_within_a_day(self):
        html = self.export().html
        self.assertLess(html.index("APPLE GRILL"), html.index("ZEBRA CAFE"))

    def test_address_and_type_accompany_the_name(self):
        html = self.export().html
        self.assertIn("2 Oak St, Little Rock, AR 72201<br>\nRoutine", html)

    def test_categories_run_most_serious_first_and_omit_uncited(self):
        html = self.export().html
        block = html[html.index("ZEBRA CAFE") : html.index("August 5")]
        self.assertLess(block.index("<strong>Priority</strong>"), block.index("<strong>Core</strong>"))
        self.assertNotIn("Priority Foundation", block)
        self.assertIn("<li>Milk held above 41 degrees.</li>", block)

    def test_clean_inspection_is_left_out(self):
        self.assertNotIn("CLEAN PLATE", self.export().html)

    def test_report_link_is_included(self):
        self.assertIn('<a href="https://example.gov/report/1.pdf">', self.export().html)

    def test_uncategorised_violations_are_excluded_but_counted(self):
        loner = make_facility("OVERLAY ONLY", street="9 Ash St")
        inspection = Inspection.objects.create(
            facility=loner, date=dt.date(2026, 8, 4), inspection_type="Routine"
        )
        cite(inspection, "", "Seen on the website only.")

        export = self.export()
        self.assertNotIn("OVERLAY ONLY", export.html)
        self.assertEqual(export.uncategorised, 1)

    def test_counts_reflect_what_was_rendered(self):
        export = self.export()
        self.assertEqual(export.establishments, 3)
        self.assertEqual(export.days, 2)

    def test_date_range_and_county_bound_the_output(self):
        self.assertNotIn("ZEBRA CAFE", self.export(date_to=dt.date(2026, 7, 31)).html)
        self.assertNotIn("ZEBRA CAFE", self.export(county="Clark").html)

    def test_markup_is_escaped(self):
        rowdy = make_facility("MOM & POP'S <b>DINER</b>", street="5 Ash St")
        inspection = Inspection.objects.create(
            facility=rowdy, date=dt.date(2026, 8, 6), inspection_type="Routine"
        )
        cite(inspection, PriorityLevel.PRIORITY, 'Sign read "closed" & the door was <open>.')

        html = self.export().html
        self.assertIn("MOM &amp; POP&#x27;S &lt;b&gt;DINER&lt;/b&gt;", html)
        self.assertIn("&lt;open&gt;", html)


class OutputViewTests(TestCase):
    def setUp(self):
        facility = make_facility("TEST DINER")
        inspection = Inspection.objects.create(
            facility=facility, date=dt.date(2026, 8, 3), inspection_type="Routine"
        )
        cite(inspection, PriorityLevel.PRIORITY, "Milk held above 41 degrees.")

    def test_form_renders_without_running(self):
        r = self.client.get(reverse("output-data"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Pulaski")
        self.assertNotContains(r, "Milk held above 41 degrees.")

    def test_output_is_generated_from_the_query_string(self):
        r = self.client.get(
            reverse("output-data"),
            {"county": "Pulaski", "date_from": "2026-08-01", "date_to": "2026-08-31"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Milk held above 41 degrees.")

    def test_reversed_date_range_is_rejected(self):
        r = self.client.get(
            reverse("output-data"),
            {"county": "Pulaski", "date_from": "2026-08-31", "date_to": "2026-08-01"},
        )
        self.assertContains(r, "start date must come before")
        self.assertNotContains(r, "Milk held above 41 degrees.")

    def test_empty_range_says_so(self):
        r = self.client.get(
            reverse("output-data"),
            {"county": "Clark", "date_from": "2026-08-01", "date_to": "2026-08-31"},
        )
        self.assertContains(r, "No cited establishments")

    def test_nav_links_to_the_page(self):
        r = self.client.get(reverse("facility-list"))
        self.assertContains(r, "Output data")
