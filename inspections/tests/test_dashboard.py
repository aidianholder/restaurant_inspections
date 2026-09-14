"""The per-newspaper dashboard: model, API and the two mount points.

The rule the whole design rests on: the map reflects the FILTER state, never the
sort or the page. Sorting reorders rows without changing which facilities match,
and the map shows every match rather than the current page — so `map` ignores
`sort` and `page` entirely, which is what makes reordering the table free.
"""

import datetime as dt
import json

from django.contrib.gis.geos import Point
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from inspections.latest_inspection import refresh
from inspections.models import Dashboard, Facility, Inspection, PriorityLevel, fingerprint
from inspections.tests.test_output import cite


def make(name, county, *, city="Little Rock", located=True, lon=-92.3, lat=34.7,
         date=dt.date(2026, 6, 1), kind="Routine", levels=()):
    facility = Facility.objects.create(
        name=name, fingerprint=fingerprint(name, county, city),
        street=f"{abs(hash(name)) % 900 + 1} Test St", city=city, state="AR",
        zip_code="72201", county=county,
        location=Point(lon, lat, srid=4326) if located else None,
    )
    inspection = Inspection.objects.create(
        facility=facility, date=date, inspection_type=kind
    )
    for n, level in enumerate(levels):
        cite(inspection, level, f"Observation {n} for {name}.", n)
    return facility


class DashboardModelTests(TestCase):
    def test_counties_are_deduplicated_and_sorted(self):
        d = Dashboard.objects.create(slug="d", counties=["Pulaski", "Conway", "Pulaski"])
        d.refresh_from_db()
        self.assertEqual(d.counties, ["Conway", "Pulaski"])

    def test_at_least_one_county_is_required(self):
        with self.assertRaises(ValidationError):
            Dashboard(slug="d", counties=[]).clean()

    def test_counties_must_be_real(self):
        with self.assertRaisesMessage(ValidationError, "Not Arkansas counties"):
            Dashboard(slug="d", counties=["Pulaski", "Atlantis"]).clean()

    def test_it_covers_only_its_own_counties(self):
        make("IN", "Pulaski")
        make("OUT", "Clark")
        refresh()
        d = Dashboard.objects.create(slug="d", counties=["Pulaski"])
        self.assertEqual([f.name for f in d.facilities()], ["IN"])

    def test_a_facility_never_inspected_is_not_covered(self):
        Facility.objects.create(
            name="NEVER", fingerprint=fingerprint("NEVER", "x", "y"),
            county="Pulaski", city="Little Rock",
        )
        refresh()
        d = Dashboard.objects.create(slug="d", counties=["Pulaski"])
        self.assertEqual(d.facilities().count(), 0)

    def test_heading_falls_back_sensibly(self):
        one = Dashboard.objects.create(slug="one", counties=["Pulaski"])
        many = Dashboard.objects.create(slug="many", counties=["Pulaski", "Saline"])
        titled = Dashboard.objects.create(
            slug="titled", counties=["Pulaski"], title_override="The Paper's Inspections")
        self.assertEqual(one.heading, "Pulaski County health inspections")
        self.assertEqual(many.heading, "Health inspections")
        self.assertEqual(titled.heading, "The Paper's Inspections")


class ApiTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alpha = make("ALPHA CAFE", "Pulaski", city="Little Rock",
                         date=dt.date(2026, 6, 1), levels=[PriorityLevel.PRIORITY])
        cls.bravo = make("BRAVO GRILL", "Saline", city="Benton",
                         date=dt.date(2026, 7, 15), kind="Follow-up",
                         levels=[PriorityLevel.CORE] * 4)
        cls.clean = make("CLEAN PLATE", "Pulaski", city="Little Rock",
                         date=dt.date(2026, 5, 1), levels=[])
        cls.nowhere = make("UNMAPPED DINER", "Pulaski", located=False,
                           date=dt.date(2026, 4, 1), levels=[PriorityLevel.PRIORITY] * 2)
        cls.outside = make("OUTSIDE CAFE", "Clark", city="Arkadelphia")
        refresh()
        cls.dashboard = Dashboard.objects.create(
            slug="paper", publication="The Paper", counties=["Pulaski", "Saline"],
            rows_per_page=2,
        )

    def rows(self, **params):
        r = self.client.get(reverse("dashboard-rows", args=["paper"]), params)
        self.assertEqual(r.status_code, 200)
        return json.loads(r.content)

    def map(self, **params):
        r = self.client.get(reverse("dashboard-map", args=["paper"]), params)
        self.assertEqual(r.status_code, 200)
        return json.loads(r.content)


class RowsApiTests(ApiTestCase):
    def test_it_returns_only_this_dashboards_counties(self):
        names = {r["name"] for r in self.rows(page=1)["rows"]} | \
                {r["name"] for r in self.rows(page=2)["rows"]}
        self.assertNotIn("OUTSIDE CAFE", names)
        self.assertEqual(self.rows()["total"], 4)

    def test_paging(self):
        first = self.rows(page=1)
        self.assertEqual(first["per_page"], 2)
        self.assertEqual(len(first["rows"]), 2)
        self.assertEqual(first["pages"], 2)
        second = self.rows(page=2)
        self.assertNotEqual(
            [r["id"] for r in first["rows"]], [r["id"] for r in second["rows"]]
        )

    def test_an_out_of_range_page_is_clamped(self):
        self.assertEqual(self.rows(page=99)["page"], 2)
        self.assertEqual(self.rows(page=-3)["page"], 1)
        self.assertEqual(self.rows(page="banana")["page"], 1)

    def test_default_sort_is_newest_first(self):
        data = self.rows()
        self.assertEqual(data["sort"], "-date")
        self.assertEqual(data["rows"][0]["name"], "BRAVO GRILL")

    def test_sorting_by_violation_count(self):
        """The sort the denormalised columns exist for."""
        data = self.rows(sort="-violations")
        self.assertEqual([r["name"] for r in data["rows"]], ["BRAVO GRILL", "UNMAPPED DINER"])
        self.assertEqual(data["rows"][0]["total"], 4)

    def test_sorting_by_name(self):
        self.assertEqual(self.rows(sort="name")["rows"][0]["name"], "ALPHA CAFE")
        self.assertEqual(self.rows(sort="-name")["rows"][0]["name"], "UNMAPPED DINER")

    def test_an_unknown_sort_falls_back_rather_than_erroring(self):
        self.assertEqual(self.rows(sort="; DROP TABLE")["sort"], "-date")

    def test_search_covers_name_street_and_city(self):
        self.assertEqual(self.rows(q="BRAVO")["total"], 1)
        self.assertEqual(self.rows(q="Benton")["total"], 1)
        self.assertEqual(self.rows(q="zzzz")["total"], 0)

    def test_county_filter(self):
        self.assertEqual(self.rows(county="Saline")["total"], 1)

    def test_a_county_outside_the_dashboard_is_ignored_not_honoured(self):
        # Otherwise a caller could read another publication's area.
        self.assertEqual(self.rows(county="Clark")["total"], 4)

    def test_date_range_filter(self):
        self.assertEqual(self.rows(**{"from": "2026-06-01"})["total"], 2)
        self.assertEqual(self.rows(to="2026-05-01")["total"], 2)
        self.assertEqual(self.rows(**{"from": "2026-06-01", "to": "2026-06-30"})["total"], 1)

    def test_an_unparseable_date_is_ignored_rather_than_fatal(self):
        self.assertEqual(self.rows(**{"from": "yesterday"})["total"], 4)

    def test_type_filter(self):
        self.assertEqual(self.rows(type="Follow-up")["total"], 1)

    def test_cited_only_filter(self):
        self.assertEqual(self.rows(cited="1")["total"], 3)

    def test_rows_carry_the_violations_for_the_dropdown(self):
        row = next(r for r in self.rows(q="BRAVO")["rows"])
        self.assertEqual(len(row["violations"]), 4)
        self.assertEqual(row["violations"][0]["level"], "C")
        self.assertIn("Observation 0 for BRAVO GRILL.", row["violations"][0]["text"])

    def test_a_clean_facility_reports_no_violations(self):
        row = next(r for r in self.rows(q="CLEAN")["rows"])
        self.assertEqual(row["total"], 0)
        self.assertEqual(row["violations"], [])

    def test_counts_are_the_latest_inspection_only(self):
        Inspection.objects.create(
            facility=self.alpha, date=dt.date(2026, 8, 1), inspection_type="Follow-up"
        )
        refresh()
        row = next(r for r in self.rows(q="ALPHA")["rows"])
        self.assertEqual(row["total"], 0, "counted the facility's history")
        self.assertEqual(row["date"], "2026-08-01")

    def test_it_reports_how_many_could_not_be_mapped(self):
        # A reader who notices fewer pins than the count will assume the map
        # is broken, so the difference is stated rather than hidden.
        self.assertEqual(self.rows()["unmapped"], 1)
        self.assertEqual(self.rows(q="ALPHA")["unmapped"], 0)


class MapApiTests(ApiTestCase):
    def test_it_returns_geojson_for_mapped_facilities_only(self):
        data = self.map()
        self.assertEqual(data["type"], "FeatureCollection")
        names = {f["properties"]["name"] for f in data["features"]}
        self.assertEqual(names, {"ALPHA CAFE", "BRAVO GRILL", "CLEAN PLATE"})

    def test_features_carry_what_the_labels_need(self):
        feature = self.map(q="ALPHA")["features"][0]
        self.assertEqual(feature["geometry"]["type"], "Point")
        props = feature["properties"]
        for key in ("id", "name", "address", "county", "total", "date"):
            self.assertIn(key, props)
        self.assertIn("Little Rock", props["address"])

    def test_it_honours_the_same_filters_as_the_table(self):
        self.assertEqual(len(self.map(county="Saline")["features"]), 1)
        self.assertEqual(len(self.map(q="BRAVO")["features"]), 1)
        self.assertEqual(len(self.map(cited="1")["features"]), 2)

    def test_sorting_does_not_change_which_facilities_are_returned(self):
        """Sorting reorders rows; the matching set is identical. This is why the
        component never refetches the map when a column header is clicked."""
        plain = [f["id"] for f in self.map()["features"]]
        sorted_ = [f["id"] for f in self.map(sort="-violations")["features"]]
        self.assertEqual(set(plain), set(sorted_))

    def test_paging_does_not_narrow_the_map(self):
        """The map shows every match, not the current page — a map showing only
        page one would be useless."""
        self.assertEqual(len(self.map(page=2)["features"]), 3)
        self.assertEqual(self.rows(page=2)["per_page"], 2)

    def test_it_stays_inside_the_dashboards_counties(self):
        names = {f["properties"]["name"] for f in self.map()["features"]}
        self.assertNotIn("OUTSIDE CAFE", names)


class FacilityApiTests(ApiTestCase):
    def test_it_returns_one_row(self):
        r = self.client.get(reverse("dashboard-facility", args=["paper", self.bravo.pk]))
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.content)
        self.assertEqual(data["name"], "BRAVO GRILL")
        self.assertEqual(data["total"], 4)
        self.assertEqual(len(data["violations"]), 4)

    def test_a_facility_outside_the_dashboard_is_not_reachable(self):
        r = self.client.get(reverse("dashboard-facility", args=["paper", self.outside.pk]))
        self.assertEqual(r.status_code, 404)


class DeliveryTests(ApiTestCase):
    def test_the_canonical_page_mounts_the_component(self):
        r = self.client.get(reverse("dashboard-page", args=["paper"]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "arhi-mount")
        # ManifestStaticFilesStorage hashes the filename when DEBUG is off, so
        # match the stem rather than pinning the exact name.
        self.assertRegex(r.content.decode(), r"dashboard/dashboard(\.[0-9a-f]+)?\.js")
        self.assertContains(r, 'id="arhi-config"')

    def test_the_page_can_be_framed(self):
        # Django's middleware sends DENY by default, which would silently break
        # every paper that prefers an iframe.
        r = self.client.get(reverse("dashboard-page", args=["paper"]))
        self.assertNotIn("X-Frame-Options", r.headers)

    def test_the_loader_is_javascript_carrying_its_configuration(self):
        r = self.client.get(reverse("dashboard-loader", args=["paper"]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/javascript")
        body = r.content.decode()
        self.assertIn("mountDashboard", body)
        self.assertIn('"counties": ["Pulaski", "Saline"]', body)
        self.assertIn("data-arhi-dashboard", body)

    def test_an_inactive_dashboard_is_gone_everywhere(self):
        self.dashboard.is_active = False
        self.dashboard.save()
        for name, args in (
            ("dashboard-page", ["paper"]),
            ("dashboard-loader", ["paper"]),
            ("dashboard-rows", ["paper"]),
            ("dashboard-map", ["paper"]),
        ):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 404, name)

    def test_the_config_carries_the_basemap_and_its_font_names(self):
        """Which glyphs exist belongs to the style, not the component.

        Our bucket carries Regular/Medium/Italic; OpenFreeMap carries
        Regular/Bold/Italic — so the style and the font names have to move
        together. Single names only: MapLibre joins a multi-font stack with
        commas into one glyph URL, which a static bucket has no directory for.
        """
        body = self.client.get(reverse("dashboard-loader", args=["paper"])).content.decode()
        self.assertIn("protostyle3.json", body)
        self.assertIn('"fonts"', body)
        config = json.loads(body.split("var config = ", 1)[1].split(";\n", 1)[0])
        self.assertEqual(set(config["fonts"]), {"regular", "emphasis"})
        for name in config["fonts"].values():
            self.assertNotIn(",", name, "a comma-joined stack has no glyph directory")

    def test_the_config_lists_the_inspection_types_actually_present(self):
        r = self.client.get(reverse("dashboard-loader", args=["paper"]))
        self.assertIn('"types": ["Follow-up", "Routine"]', r.content.decode())

    def test_post_is_not_allowed_on_the_read_only_endpoints(self):
        for name in ("dashboard-rows", "dashboard-map"):
            self.assertEqual(
                self.client.post(reverse(name, args=["paper"])).status_code, 405, name
            )

    def test_the_api_is_readable_from_a_newspapers_own_origin(self):
        """The component is mounted into the paper's page, so its fetches are
        cross-origin and a response without this header is discarded unread.

        The loader itself is a <script src>, which is exempt, so losing this
        header breaks the data while the snippet still appears to load.
        """
        for name, args in (
            ("dashboard-rows", ["paper"]),
            ("dashboard-map", ["paper"]),
            ("dashboard-facility", ["paper", self.alpha.pk]),
        ):
            r = self.client.get(
                reverse(name, args=args), headers={"origin": "https://www.example.com"}
            )
            self.assertEqual(r.status_code, 200, name)
            self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "*", name)
