"""Who can reach what.

Everything requires a staff login except what readers and newspapers' pages
load: the establishment page, the dashboard (page, loader and API) and the
embeds. Those are requested from readers' browsers on the papers' own sites,
where nobody is logged in, so a login check there would silently break every
embed.
"""

import datetime as dt

from django.test import TestCase
from django.urls import reverse

from inspections.latest_inspection import refresh
from inspections.models import Dashboard, Embed, Inspection, ScrapeRun
from inspections.tests.test_output import log_in_staff, make_facility


class AccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.facility = make_facility("ACCESS DINER")
        Inspection.objects.create(
            facility=cls.facility, date=dt.date(2026, 8, 3), inspection_type="Routine"
        )
        refresh()
        Dashboard.objects.create(slug="paper", counties=["Pulaski"])
        Embed.objects.create(slug="table", county="Pulaski")
        cls.scrape_run = ScrapeRun.objects.create(
            county="Pulaski", date_from=dt.date(2026, 8, 1), date_to=dt.date(2026, 8, 31)
        )

    def staff_urls(self):
        return [
            reverse("facility-list"),
            reverse("facility-detail", args=[self.facility.slug]),
            reverse("output-data"),
            reverse("output-summary"),
            reverse("output-render"),
            reverse("scrape-request"),
            reverse("scrape-detail", args=[self.scrape_run.pk]),
            reverse("scrape-status", args=[self.scrape_run.pk]),
        ]

    def public_urls(self):
        return [
            self.facility.get_absolute_url(),
            reverse("dashboard-page", args=["paper"]),
            reverse("dashboard-loader", args=["paper"]),
            reverse("dashboard-rows", args=["paper"]),
            reverse("dashboard-map", args=["paper"]),
            reverse("dashboard-facility", args=["paper", self.facility.pk]),
            reverse("embed-page", args=["table"]),
            reverse("embed-loader", args=["table"]),
        ]

    def test_staff_pages_send_anonymous_visitors_to_the_admin_login(self):
        for url in self.staff_urls():
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertEqual(r.status_code, 302)
                self.assertTrue(r["Location"].startswith(reverse("admin:login")), r["Location"])
                self.assertIn(f"next={url}", r["Location"])

    def test_staff_pages_open_once_logged_in(self):
        log_in_staff(self.client)
        for url in self.staff_urls():
            with self.subTest(url=url):
                # The POST-only endpoints answer GET with 405, which still shows
                # the request got past the login check.
                self.assertIn(self.client.get(url).status_code, (200, 405))

    def test_reader_facing_endpoints_need_no_login(self):
        for url in self.public_urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_the_admin_login_page_itself_is_reachable(self):
        self.assertEqual(self.client.get(reverse("admin:login")).status_code, 200)
