"""HTTP client for the ADH public inspection search.

The site is ASP.NET WebForms: every interaction is a POST that echoes back the
page's hidden ViewState bundle. No headless browser is needed. The flow is:

    GET  search page                 -> form state
    POST county + dates + btnSearch  -> results page 1
    POST __EVENTTARGET=grid, Page$N  -> results page N
    POST __EVENTTARGET=lnkViolations -> same page, plus the observations panel
    POST __EVENTTARGET=btnInspectionReport -> same page, plus a window.open()
                                             URL pointing at the report PDF

Detail postbacks are issued against the *saved* form state of the results page
they came from, so the grid never has to be re-fetched between them.
"""

import logging
import time

import requests
from bs4 import BeautifulSoup

from ..counties import ARKANSAS_STATE_ID, COUNTY_IDS
from .parsers import find_report_url, parse_form_state, parse_results_page, parse_violations

logger = logging.getLogger(__name__)

SEARCH_URL = "https://foodserviceprod.adh.arkansas.gov/Web/inspection/publicinspectionsearch.aspx"
BASE_URL = "https://foodserviceprod.adh.arkansas.gov"

GRID = "ctl00$MainContent$gvInspections"
FIELD_STATE = "ctl00$MainContent$wucStateCountiesFS$ddlState"
FIELD_COUNTY = "ctl00$MainContent$wucStateCountiesFS$ddlCounty"
FIELD_BEGIN = "ctl00$MainContent$dteInspectionBeginDate$txtDate"
FIELD_END = "ctl00$MainContent$dteInspectionEndDate$txtDate"
FIELD_SEARCH = "ctl00$MainContent$btnSearch"
FIELD_MAP = "ctl00$MainContent$chkDisplayMap"
FIELD_REBIND = "ctl00$MainContent$hfRebindGridOnPostBack"


class ADHScrapeError(RuntimeError):
    pass


class ADHClient:
    """A polite, stateful session against the ADH search page."""

    def __init__(self, delay_seconds=1.5, user_agent=None, timeout=120):
        self.delay = delay_seconds
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent or "health-inspections-bot/0.1 (public records research)",
                "Accept": "text/html,application/xhtml+xml",
            }
        )
        self._last_request = 0.0

    # -- plumbing ---------------------------------------------------------

    def _wait(self):
        """Never hit the state's server faster than the configured delay."""
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request = time.monotonic()

    def _get(self, url):
        self._wait()
        r = self.session.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r

    def _post(self, data):
        self._wait()
        r = self.session.post(SEARCH_URL, data=data, timeout=self.timeout)
        r.raise_for_status()
        return r

    @staticmethod
    def _form_state(html_text):
        return parse_form_state(BeautifulSoup(html_text, "lxml"))

    # -- searching --------------------------------------------------------

    def search(self, county, date_from, date_to, include_map=False):
        """Run a county + date-range search. Returns (parsed_page, form_state)."""
        if county not in COUNTY_IDS:
            raise ADHScrapeError(f"Unknown county: {county!r}")

        state = self._form_state(self._get(SEARCH_URL).text)
        state.update(
            {
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
                FIELD_STATE: ARKANSAS_STATE_ID,
                FIELD_COUNTY: str(COUNTY_IDS[county]),
                FIELD_BEGIN: date_from.strftime("%m/%d/%Y"),
                FIELD_END: date_to.strftime("%m/%d/%Y"),
                FIELD_SEARCH: "Search",
            }
        )
        if include_map:
            # Makes the server emit its own coordinates alongside the grid. Off by
            # default: those coordinates proved unreliable and geocoding now goes
            # through the Arkansas GIS locator instead. Parsing is retained so the
            # payload stays available for comparison.
            state[FIELD_MAP] = "on"

        html_text = self._post(state).text
        return parse_results_page(html_text), self._form_state(html_text)

    def goto_page(self, form_state, page_number):
        """Move the results grid to a page. Returns (parsed_page, form_state)."""
        data = dict(form_state)
        data.pop(FIELD_SEARCH, None)
        data.update({"__EVENTTARGET": GRID, "__EVENTARGUMENT": f"Page${page_number}"})
        html_text = self._post(data).text
        return parse_results_page(html_text), self._form_state(html_text)

    # -- per-inspection detail --------------------------------------------

    def fetch_violations(self, form_state, target):
        """Open one inspection's observations overlay and parse it."""
        if not target:
            return []
        data = dict(form_state)
        data.pop(FIELD_SEARCH, None)
        data.update({"__EVENTTARGET": target, "__EVENTARGUMENT": "", FIELD_REBIND: "true"})
        return parse_violations(self._post(data).text)

    def fetch_report_pdf(self, form_state, target):
        """Trigger the report postback, follow the popup URL, return PDF bytes.

        Returns (content, url) or (None, "") when no report is available.
        """
        if not target:
            return None, ""
        data = dict(form_state)
        data.pop(FIELD_SEARCH, None)
        # An image button posts its coordinates rather than __EVENTTARGET.
        data.update({"__EVENTTARGET": "", "__EVENTARGUMENT": "", f"{target}.x": "8", f"{target}.y": "8"})

        url = find_report_url(self._post(data).text)
        if not url:
            return None, ""

        full_url = BASE_URL + url if url.startswith("/") else url
        r = self._get(full_url)
        if "application/pdf" not in r.headers.get("Content-Type", ""):
            logger.warning("Report URL did not return a PDF: %s", full_url)
            return None, full_url
        return r.content, full_url
