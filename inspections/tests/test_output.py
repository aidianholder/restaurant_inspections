import datetime as dt

from django.test import TestCase
from django.urls import reverse

from inspections.markdown_render import markdown_to_html
from inspections.models import Facility, Inspection, PriorityLevel, Violation, fingerprint
from inspections.output import build_export, establishment_names


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
        )

    def markdown(self, **kwargs):
        return self.export(**kwargs).markdown

    def test_the_heading_names_the_county_and_the_range(self):
        md = self.markdown(date_from=dt.date(2026, 9, 4), date_to=dt.date(2026, 9, 11))
        self.assertTrue(
            md.startswith("# Pulaski County health inspections 9/04/26 - 9/11/26"), md[:120]
        )

    def test_the_heading_uses_the_requested_range_not_the_inspection_dates(self):
        # The reader is told what period was searched, not which days happened
        # to produce a citation.
        md = self.markdown()
        self.assertIn("8/01/26 - 8/31/26", md)
        self.assertNotIn("8/03/26", md)

    def test_boilerplate_follows_the_heading(self):
        md = self.markdown()
        self.assertLess(
            md.index("# Pulaski County"),
            md.index("Violations marked as priority contribute directly"),
        )
        self.assertIn("Only categories in which an establishment was cited are listed.", md)

    def test_establishments_are_alphabetical_across_the_whole_range(self):
        self.assertEqual(
            establishment_names(self.markdown()),
            ["APPLE GRILL", "ZEBRA CAFE", "ZEBRA CAFE"],
        )

    def test_no_dates_appear_in_the_body(self):
        md = self.markdown()
        body = md[md.index("APPLE GRILL"):]
        for fragment in ("August", "2026-08", "8/03", "8/05"):
            self.assertNotIn(fragment, body)

    def test_one_facility_inspected_twice_appears_twice_in_date_order(self):
        md = self.markdown()
        first = md.index("ZEBRA CAFE")
        self.assertLess(
            md.index("Floor tiles cracked.", first),
            md.index("Employee did not wash hands.", first),
            "the earlier inspection should come first",
        )

    def test_address_and_type_are_separate_paragraphs(self):
        # They shared one paragraph with a <br> until Markdown made that a
        # trailing-whitespace trap.
        md = self.markdown()
        self.assertIn("2 Oak St, Little Rock, AR 72201\n\nRoutine", md)
        self.assertNotIn("<br>", md)

    def test_categories_run_most_serious_first_and_omit_uncited(self):
        md = self.markdown()
        start = md.index("ZEBRA CAFE")
        block = md[start : md.index("ZEBRA CAFE", start + 1)]
        self.assertLess(block.index("**Priority**"), block.index("**Core**"))
        self.assertNotIn("Priority Foundation", block)
        self.assertIn("- Milk held above 41 degrees.", block)

    def test_clean_inspection_is_left_out(self):
        self.assertNotIn("CLEAN PLATE", self.markdown())

    def test_report_links_are_left_out(self):
        md = self.markdown()
        self.assertNotIn("example.gov/report/1.pdf", md)
        self.assertNotIn("](", md)

    def test_uncategorised_violations_are_excluded_but_counted(self):
        loner = make_facility("OVERLAY ONLY", street="9 Ash St")
        inspection = Inspection.objects.create(
            facility=loner, date=dt.date(2026, 8, 4), inspection_type="Routine"
        )
        cite(inspection, "", "Seen on the website only.")

        export = self.export()
        self.assertNotIn("OVERLAY ONLY", export.markdown)
        self.assertEqual(export.uncategorised, 1)

    def test_counts_reflect_what_was_rendered(self):
        export = self.export()
        self.assertEqual(export.establishments, 3)
        self.assertEqual(export.days, 2)

    def test_date_range_and_county_bound_the_output(self):
        self.assertNotIn("ZEBRA CAFE", self.markdown(date_to=dt.date(2026, 7, 31)))
        self.assertNotIn("ZEBRA CAFE", self.markdown(county="Clark"))

    def test_the_preamble_is_held_apart_from_the_blocks(self):
        # The summariser sends only the blocks, so the split has to be real.
        export = self.export()
        self.assertIn("Only categories", export.preamble)
        self.assertEqual(len(export.blocks), 3)
        for block in export.blocks:
            self.assertTrue(block.startswith("### "))
            self.assertNotIn("Only categories", block)
        self.assertEqual(export.markdown, "\n\n".join([export.preamble, *export.blocks]) + "\n")


class MarkdownEscapingTests(TestCase):
    """Inspector prose is full of characters CommonMark reads as markup."""

    def block_for(self, name, comment, street="1 Main St", kind="Routine"):
        facility = make_facility(name, street=street)
        inspection = Inspection.objects.create(
            facility=facility, date=dt.date(2026, 8, 3), inspection_type=kind
        )
        cite(inspection, PriorityLevel.PRIORITY, comment)
        return build_export("Pulaski", dt.date(2026, 8, 1), dt.date(2026, 8, 31)).markdown

    def test_temperature_comparisons_survive(self):
        # The one that would actually bite: "<41F" is the house style for a
        # cold-holding violation, and raw it is an HTML tag.
        md = self.block_for("COLD ONE", "Milk held at >45F, not <41F as required.")
        self.assertIn(r"\>45F", md)
        self.assertIn(r"\<41F", md)
        self.assertIn("&gt;45F", markdown_to_html(md))
        self.assertIn("&lt;41F", markdown_to_html(md))

    def test_asterisks_do_not_become_emphasis(self):
        md = self.block_for("STAR CAFE", "Sign read *see manager* and nothing else.")
        self.assertNotIn("<em>", markdown_to_html(md))
        self.assertIn("*see manager*", markdown_to_html(md))

    def test_an_address_starting_with_a_hash_is_not_a_heading(self):
        md = self.block_for("HASH HOUSE", "Floor dirty.", street="#5 Highway 65")
        html = markdown_to_html(md)
        self.assertIn("#5 Highway 65", html)
        self.assertNotIn("<h1>5 Highway 65", html)

    def test_a_newline_inside_a_comment_does_not_split_the_bullet(self):
        md = self.block_for("MULTILINE", "First sentence.\nSecond sentence.")
        self.assertIn("- First sentence. Second sentence.", md)

    def test_trailing_spaces_do_not_become_a_hard_break(self):
        md = self.block_for("TRAILING", "Ends with spaces.   ")
        self.assertNotIn("  \n", md)

    def test_ampersands_and_quotes_reach_the_html_intact(self):
        md = self.block_for("MOM & POP'S DINER", 'Sign read "closed" & the door was open.')
        html = markdown_to_html(md)
        self.assertIn("MOM &amp; POP'S DINER", html)
        self.assertIn("&quot;closed&quot; &amp; the door was open.", html)


class MarkdownRenderTests(TestCase):
    def test_the_document_shape_survives_the_round_trip(self):
        markdown = (
            "# Pulaski County health inspections 8/01/26 - 8/31/26\n\n"
            "Only categories in which an establishment was cited are listed.\n\n"
            "### TEST DINER\n\n1 Main St\n\nRoutine\n\n**Priority**\n\n- Milk too warm.\n"
        )
        html = markdown_to_html(markdown)
        self.assertIn("<h1>Pulaski County health inspections 8/01/26 - 8/31/26</h1>", html)
        self.assertIn("<h3>TEST DINER</h3>", html)
        self.assertIn("<p>1 Main St</p>", html)
        self.assertIn("<p>Routine</p>", html)
        self.assertIn("<p><strong>Priority</strong></p>", html)
        self.assertIn("<li>Milk too warm.</li>", html)

    def test_raw_html_is_shown_rather_than_run(self):
        # A person edits this text before it is published; a tag pasted in from
        # somewhere should reach the story as visible characters.
        html = markdown_to_html("Text with <script>alert(1)</script> in it.")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_empty_input_renders_to_nothing(self):
        self.assertEqual(markdown_to_html(""), "")


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
        self.assertContains(r, "# Pulaski County health inspections")

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


class RenderEndpointTests(TestCase):
    def test_markdown_is_rendered_to_html(self):
        r = self.client.post(reverse("output-render"), {"markdown": "### TEST\n\n- one\n"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["html"], "<h3>TEST</h3>\n<ul>\n<li>one</li>\n</ul>")

    def test_it_matches_what_the_preview_would_show(self):
        # Same endpoint backs the preview and the Copy HTML button, which is the
        # point — a preview that can drift from the copy is worse than none.
        markdown = "# Heading\n\n**Priority**\n\n- Something.\n"
        r = self.client.post(reverse("output-render"), {"markdown": markdown})
        self.assertEqual(r.json()["html"], markdown_to_html(markdown))

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(reverse("output-render")).status_code, 405)

    def test_missing_markdown_renders_to_nothing(self):
        r = self.client.post(reverse("output-render"), {})
        self.assertEqual(r.json()["html"], "")

    def test_something_enormous_is_refused(self):
        r = self.client.post(reverse("output-render"), {"markdown": "x" * 2_000_001})
        self.assertEqual(r.status_code, 400)
