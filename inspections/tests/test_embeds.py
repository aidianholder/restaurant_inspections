"""Embed endpoints: what a newspaper actually receives."""

import datetime as dt

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from inspections.models import (
    Embed, Facility, Inspection, Violation, ViolationItem, fingerprint,
)


def make_inspection(name, date, county="Pulaski", violations=0, itype="Routine"):
    facility, _ = Facility.objects.get_or_create(
        name=name,
        defaults={
            "fingerprint": fingerprint(name, name, "72201"),
            "street": f"{name} Road", "city": "Little Rock", "county": county,
        },
    )
    inspection = Inspection.objects.create(
        facility=facility, date=date, inspection_type=itype,
        sequence_within_day=Inspection.objects.filter(facility=facility, date=date).count(),
    )
    for i in range(violations):
        Violation.objects.create(
            inspection=inspection, ordinal=i, code=f"20 CAR {i}",
            item=ViolationItem.objects.get(number=22),
            priority_level="P", inspector_comments=f"Finding number {i} for {name}.",
        )
    return inspection


class EmbedConfigTests(TestCase):
    def test_rolling_window_moves_with_today(self):
        embed = Embed(slug="e", county="Pulaski", window_days=30)
        start, end = embed.resolved_window(dt.date(2026, 8, 21))
        self.assertEqual(end, dt.date(2026, 8, 21))
        self.assertEqual(start, dt.date(2026, 7, 22))
        # Same embed, a month later — the window has moved, no edit needed.
        start2, _ = embed.resolved_window(dt.date(2026, 9, 21))
        self.assertEqual(start2, dt.date(2026, 8, 22))

    def test_fixed_range_is_used_verbatim(self):
        embed = Embed(
            slug="e", county="Pulaski", window_days=None,
            date_from=dt.date(2026, 1, 1), date_to=dt.date(2026, 2, 1),
        )
        self.assertEqual(embed.resolved_window(dt.date(2026, 8, 21)),
                         (dt.date(2026, 1, 1), dt.date(2026, 2, 1)))

    def test_cannot_set_both_window_kinds(self):
        embed = Embed(slug="e", county="Pulaski", window_days=30,
                      date_from=dt.date(2026, 1, 1), date_to=dt.date(2026, 2, 1))
        with self.assertRaises(ValidationError):
            embed.clean()

    def test_must_set_one_window_kind(self):
        with self.assertRaises(ValidationError):
            Embed(slug="e", county="Pulaski", window_days=None).clean()

    def test_heading_and_date_label(self):
        embed = Embed(slug="e", county="Garland", window_days=30)
        self.assertEqual(embed.heading, "Garland County Health Inspections")
        self.assertIn("2026", embed.date_range_label(dt.date(2026, 8, 21)))


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}})
class EmbedPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.embed = Embed.objects.create(
            slug="pulaski", publication="Test Paper", county="Pulaski",
            window_days=30, rows_per_page=20,
        )
        today = dt.date.today()
        cls.recent = make_inspection("IN WINDOW CAFE", today - dt.timedelta(days=3), violations=2)
        cls.old = make_inspection("TOO OLD DINER", today - dt.timedelta(days=200), violations=1)
        cls.other = make_inspection("WRONG COUNTY GRILL", today - dt.timedelta(days=3),
                                    county="Garland", violations=1)
        cls.clean = make_inspection("SPOTLESS DELI", today - dt.timedelta(days=1), violations=0)

    def get(self):
        return self.client.get(reverse("embed-page", args=[self.embed.slug]))

    def test_shows_only_this_county_inside_the_window(self):
        response = self.get()
        self.assertContains(response, "IN WINDOW CAFE")
        self.assertNotContains(response, "TOO OLD DINER")
        self.assertNotContains(response, "WRONG COUNTY GRILL")

    def test_heading_and_range_are_rendered(self):
        self.assertContains(self.get(), "Pulaski County Health Inspections")

    def test_violation_detail_ships_with_the_page(self):
        """No API call on expand: the notes are already in the HTML."""
        self.assertContains(self.get(), "Finding number 0 for IN WINDOW CAFE.")

    def test_short_description_is_the_headline(self):
        self.assertContains(self.get(), "Proper cold holding temperatures")

    def test_clean_inspection_still_expands_with_a_message(self):
        response = self.get()
        self.assertContains(response, "SPOTLESS DELI")
        self.assertContains(response, "No violations were recorded on this inspection.")

    def test_rows_carry_sort_and_search_data(self):
        response = self.get()
        self.assertContains(response, 'data-viol="2"')
        self.assertContains(response, 'data-search="in window cafe')

    def test_can_be_framed_by_anyone(self):
        """Django sends X-Frame-Options: DENY by default, which would break every embed."""
        self.assertNotIn("X-Frame-Options", self.get())

    def test_is_publicly_cacheable(self):
        self.assertIn("public", self.get()["Cache-Control"])

    def test_self_contained_no_external_requests(self):
        """A strict host page may block third-party assets, so nothing external."""
        body = self.get().content.decode()
        for pattern in ("<link ", "src=\"http", "@import", "fonts.googleapis"):
            self.assertNotIn(pattern, body, f"embed pulled in external asset: {pattern}")

    def test_inactive_embed_renders_blank_not_an_error(self):
        self.embed.is_active = False
        self.embed.save()
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "IN WINDOW CAFE")

    def test_row_limit_is_enforced(self):
        self.embed.row_limit = 1
        self.embed.save()
        self.assertEqual(self.get().content.decode().count('class="row"'), 1)

    def test_unknown_slug_is_404(self):
        self.assertEqual(self.client.get(reverse("embed-page", args=["nope"])).status_code, 404)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}})
class EmbedLoaderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.embed = Embed.objects.create(slug="pulaski", county="Pulaski", window_days=30)

    def get(self):
        return self.client.get(reverse("embed-loader", args=[self.embed.slug]))

    def test_serves_javascript(self):
        self.assertEqual(self.get()["Content-Type"], "application/javascript")

    def test_points_at_its_own_embed_and_checks_the_origin(self):
        body = self.get().content.decode()
        self.assertIn("/embed/pulaski/", body)
        self.assertIn("event.origin !== ORIGIN", body)

    def test_carries_a_fallback_height_before_the_first_measurement(self):
        self.assertIn('frame.height = "600"', self.get().content.decode())


class EmbedCacheKeyTests(TestCase):
    """The cache must not outlive the data or the template."""

    def setUp(self):
        cache.clear()
        self.embed = Embed.objects.create(slug="c", county="Pulaski", window_days=30)

    def test_new_data_changes_the_fingerprint(self):
        from inspections.embed_views import _data_fingerprint

        today = dt.date.today()
        start, end = self.embed.resolved_window(today)
        before = _data_fingerprint("Pulaski", start, end)
        make_inspection("NEW ARRIVAL", today - dt.timedelta(days=1), violations=1)
        self.assertNotEqual(before, _data_fingerprint("Pulaski", start, end))

    def test_template_version_is_stable_and_non_empty(self):
        from inspections.embed_views import _template_version

        self.assertEqual(_template_version(), _template_version())
        self.assertNotEqual(_template_version(), "0")


class AdminPresentationTests(TestCase):
    """The admin is the whole configuration UI for v1, so it has to be usable."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth.models import User

        cls.user = User.objects.create_superuser("staff", "s@example.com", "pw-for-tests-only")
        cls.embed = Embed.objects.create(slug="pulaski", county="Pulaski", window_days=30)

    def setUp(self):
        self.client.force_login(self.user)

    def test_site_is_named_for_this_project(self):
        response = self.client.get(reverse("admin:index"))
        self.assertContains(response, "AR Health Inspections")
        self.assertNotContains(response, "Django administration")

    def test_embed_snippet_pins_both_colours(self):
        """Setting only a background leaves light-on-light under the dark theme."""
        response = self.client.get(reverse("admin:inspections_embed_change", args=[self.embed.pk]))
        body = response.content.decode()
        self.assertIn("background:#fbfaf8;color:#1a1a1a", body)

    def test_embed_snippet_offers_both_tags(self):
        response = self.client.get(reverse("admin:inspections_embed_change", args=[self.embed.pk]))
        self.assertContains(response, "/embed/pulaski.js")
        self.assertContains(response, "&lt;iframe")
