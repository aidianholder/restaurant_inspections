"""Parser tests against real HTML captured from the ADH site.

The fixtures are live responses, so these tests are the early-warning system for
the state changing its markup: they fail loudly instead of silently importing
empty rows.
"""

import datetime as dt
from pathlib import Path

from django.test import SimpleTestCase

from inspections.scraper.parsers import (
    find_report_url,
    parse_results_page,
    parse_violations,
    split_address,
)

FIXTURES = Path(__file__).parent / "fixtures"


class SplitAddressTests(SimpleTestCase):
    def test_single_space_before_city(self):
        r = split_address("10815 Colonel Glenn Rd STE 450 Little Rock, AR 72204")
        self.assertEqual(r["street"], "10815 Colonel Glenn Rd STE 450")
        self.assertEqual(r["city"], "Little Rock")
        self.assertEqual(r["zip_code"], "72204")
        self.assertFalse(r["needs_review"])

    def test_padded_whitespace_before_city(self):
        r = split_address("7001 Colonel Glenn Rd    Little Rock, AR 72204")
        self.assertEqual(r["street"], "7001 Colonel Glenn Rd")
        self.assertEqual(r["city"], "Little Rock")

    def test_longest_place_name_wins(self):
        r = split_address("13101 Crystal Hill Rd Ste E   STE E North Little Rock, AR 72113")
        self.assertEqual(r["city"], "North Little Rock")
        self.assertEqual(r["street"], "13101 Crystal Hill Rd Ste E STE E")

    def test_unknown_city_is_flagged_not_guessed(self):
        r = split_address("100 Main St Nowheresville, AR 71111")
        self.assertTrue(r["needs_review"])
        self.assertEqual(r["city"], "")
        self.assertEqual(r["zip_code"], "71111")

    def test_unparseable_blob_is_preserved(self):
        r = split_address("no address here")
        self.assertTrue(r["needs_review"])
        self.assertEqual(r["raw"], "no address here")


class ResultsPageTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.page = parse_results_page((FIXTURES / "results_page.html").read_text())

    def test_finds_every_facility_and_pagination(self):
        self.assertEqual(len(self.page["facilities"]), 15)
        self.assertEqual(self.page["page"], 1)
        self.assertEqual(self.page["total_pages"], 3)

    def test_every_row_has_a_name_and_parsed_address(self):
        for f in self.page["facilities"]:
            self.assertTrue(f["name"], f"missing name in {f}")
            self.assertFalse(f["address"]["needs_review"], f"unparsed address in {f['name']}")

    def test_name_address_and_phone_are_separated(self):
        f = self.page["facilities"][0]
        self.assertEqual(f["name"], "HENDERSON STATE CAFETERIA")
        self.assertEqual(f["address"]["city"], "Arkadelphia")
        self.assertEqual(f["address"]["zip_code"], "71923")
        self.assertEqual(f["phone"], "870-626-4100")
        self.assertEqual(f["source_key"], "364346")

    def test_history_is_captured_without_extra_requests(self):
        """The nested past-inspections grid ships inline; we must read all of it."""
        f = self.page["facilities"][0]
        self.assertEqual(len(f["inspections"]), 15)
        latest = f["inspections"][0]
        self.assertEqual(latest["date"], dt.date(2026, 8, 14))
        self.assertEqual(latest["inspection_type"], "Routine")
        self.assertEqual(latest["observation_count"], 2)
        self.assertTrue(latest["violations_target"])
        self.assertTrue(latest["report_target"])

    def test_inspections_without_observations_have_no_violation_target(self):
        for f in self.page["facilities"]:
            for insp in f["inspections"]:
                if insp["observation_count"] == 0:
                    self.assertEqual(insp["violations_target"], "")

    def test_same_day_duplicates_get_distinct_sequence_numbers(self):
        for f in self.page["facilities"]:
            keys = [
                (i["date"], i["inspection_type"], i["sequence_within_day"])
                for i in f["inspections"]
            ]
            self.assertEqual(len(keys), len(set(keys)), f"duplicate natural key in {f['name']}")

    def test_map_coordinates_are_extracted(self):
        self.assertTrue(self.page["coordinates"])
        pt = self.page["coordinates"][0]
        self.assertIn("name", pt)
        self.assertTrue(float(pt["latitude"]))


class ViolationsPageTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.html = (FIXTURES / "violations_page.html").read_text()

    def test_parses_code_explanation_and_comments(self):
        violations = parse_violations(self.html)
        self.assertEqual(len(violations), 2)
        first = violations[0]
        self.assertEqual(first["code"], "20 CAR 192-501 (f)")
        self.assertEqual(first["code_explanation"], "(a) (2) and (B) PHF/ (TCS FOOD) Cold Holding")
        self.assertIn("Jelly labeled", first["inspector_comments"])
        # The header labels must be stripped, not stored as content.
        self.assertNotIn("Code Explanation", first["code_explanation"])
        self.assertNotIn("Inspector Comments", first["inspector_comments"])

    def test_ordinals_are_sequential(self):
        self.assertEqual([v["ordinal"] for v in parse_violations(self.html)], [0, 1])


class ReportUrlTests(SimpleTestCase):
    def test_extracts_popup_url(self):
        html = """<script>window.open('/Web/Common/ExternalFileViewer.aspx?ID=abc&Key=def','_blank');</script>"""
        self.assertEqual(
            find_report_url(html), "/Web/Common/ExternalFileViewer.aspx?ID=abc&Key=def"
        )

    def test_returns_empty_when_absent(self):
        self.assertEqual(find_report_url("<html>nothing</html>"), "")


class ReportPdfTests(SimpleTestCase):
    """Parsed against a real report PDF captured from the ADH site.

    The report is the authoritative source of violations — the website overlay
    omitted one of these three — so a parser regression here silently loses data.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from inspections.scraper.report_pdf import parse_report

        cls.report = parse_report(FIXTURES / "inspection_report.pdf")

    def test_extracts_the_states_inspection_id(self):
        """This ID exists nowhere in the search results — only in the PDF."""
        self.assertEqual(self.report["inspection_id"], "422557")

    def test_finds_every_violation_including_the_one_the_website_hides(self):
        self.assertEqual(len(self.report["violations"]), 3)
        items = [v["item_number"] for v in self.report["violations"]]
        self.assertEqual(items, ["3", "5", "39"])

    def test_rejoins_codes_wrapped_across_lines(self):
        """The cell holds "20 CAR 191-\\n201 (a)(1),(2),\\n(3)&(5)"."""
        self.assertEqual(self.report["violations"][0]["code"], "20 CAR 191-201 (a)(1),(2),(3)&(5)")
        self.assertEqual(self.report["violations"][2]["code"], "20 CAR 192-305 (a)")

    def test_captures_priority_levels(self):
        levels = [v["priority_level"] for v in self.report["violations"]]
        self.assertEqual(levels, ["PF", "PF", "C"])

    def test_captures_correct_by_dates(self):
        import datetime as dt

        for violation in self.report["violations"]:
            self.assertEqual(violation["correct_by"], dt.date(2026, 8, 17))

    def test_comment_keeps_the_full_narrative(self):
        comment = self.report["violations"][2]["comment"]
        self.assertIn("Observed food being stored on facility floor", comment)
        self.assertIn("at least 6 inches above the floor", comment)
        self.assertNotIn("\n", comment)

    def test_reads_the_header_violation_counts(self):
        self.assertEqual(self.report["risk_violation_count"], 3)
        self.assertEqual(self.report["repeat_violation_count"], 0)
