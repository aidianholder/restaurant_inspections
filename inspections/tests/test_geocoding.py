"""Geocoding tests. Both services are mocked: these assert our decision logic —
source ordering, precision filtering, and retry limits — not theirs."""

from unittest.mock import patch

from django.test import TestCase, override_settings

from inspections.geocoding import (
    arkansas_gis_geocode, arkansas_ng911_geocode, census_geocode, locate_facility,
)
from inspections.models import Facility, GeocodeSource, fingerprint


def arkgis(addr_type="PointAddress", score=100, x=-93.058088, y=34.129376,
           match="1033 Huddleston St, Arkadelphia, AR, 71923"):
    return {
        "candidates": [
            {
                "location": {"x": x, "y": y},
                "attributes": {"Addr_type": addr_type, "Score": score, "Match_addr": match},
            }
        ]
    }


ARKGIS_EMPTY = {"candidates": []}
CENSUS_HIT = {
    "result": {
        "addressMatches": [
            {
                "matchedAddress": "1033 HUDDLESTON ST, ARKADELPHIA, AR, 71923",
                "coordinates": {"x": -93.05, "y": 34.12},
            }
        ]
    }
}
CENSUS_MISS = {"result": {"addressMatches": []}}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def responder(arkgis_payload, census_payload, ng911_payload=ARKGIS_EMPTY):
    """Dispatch a mocked GET to the right fake payload by URL.

    Both Arkansas locators live on gis.arkansas.gov, so they are told apart by
    the locator name in the path — matching on the host alone would quietly feed
    one service's payload to the other.
    """

    def _get(url, **kwargs):
        if "NG911" in url:
            return FakeResponse(ng911_payload)
        if "gis.arkansas.gov" in url:
            return FakeResponse(arkgis_payload)
        return FakeResponse(census_payload)

    return _get


def make_facility(name="TEST CAFE", street="1033 Huddleston Street"):
    return Facility.objects.create(
        name=name, street=street, city="Arkadelphia", state="AR", zip_code="71923",
        fingerprint=fingerprint(name, street, "71923"),
    )


@override_settings(ARKANSAS_GIS_DELAY=0, ARKANSAS_NG911_DELAY=0, CENSUS_GEOCODER_DELAY=0)
class SourceOrderingTests(TestCase):
    def test_arkansas_gis_wins_and_census_is_not_called(self):
        facility = make_facility()
        with patch("inspections.geocoding.requests.get", side_effect=responder(arkgis(), CENSUS_HIT)) as m:
            source = locate_facility(facility)
        self.assertEqual(source, GeocodeSource.ARKANSAS_GIS)
        self.assertEqual(m.call_count, 1)  # Census never queried
        facility.refresh_from_db()
        self.assertAlmostEqual(facility.latitude, 34.129376, places=5)

    def test_falls_back_to_census_when_arkansas_gis_has_nothing(self):
        facility = make_facility()
        with patch("inspections.geocoding.requests.get", side_effect=responder(ARKGIS_EMPTY, CENSUS_HIT)):
            source = locate_facility(facility)
        self.assertEqual(source, GeocodeSource.CENSUS)
        facility.refresh_from_db()
        self.assertAlmostEqual(facility.latitude, 34.12, places=5)

    def test_records_the_match_type_for_review(self):
        facility = make_facility()
        with patch("inspections.geocoding.requests.get", side_effect=responder(arkgis(), CENSUS_MISS)):
            locate_facility(facility)
        facility.refresh_from_db()
        self.assertIn("PointAddress", facility.geocode_matched_address)


@override_settings(ARKANSAS_GIS_DELAY=0, ARKANSAS_NG911_DELAY=0, CENSUS_GEOCODER_DELAY=0)
class PrecisionFilterTests(TestCase):
    def test_locality_is_rejected_as_a_city_centroid(self):
        """The locator answers nearly every query; a Locality hit is a town centre."""
        result = None
        with patch("inspections.geocoding.requests.get",
                   side_effect=responder(arkgis(addr_type="Locality", score=75), CENSUS_MISS)):
            result = arkansas_gis_geocode("1 Nowhere St", "Hampton", "AR", "71744")
        self.assertIsNone(result)

    def test_street_name_only_is_rejected(self):
        with patch("inspections.geocoding.requests.get",
                   side_effect=responder(arkgis(addr_type="StreetName", score=98), CENSUS_MISS)):
            self.assertIsNone(arkansas_gis_geocode("Highway 107", "Sherwood", "AR", "72120"))

    def test_low_score_is_rejected_even_at_a_good_type(self):
        with patch("inspections.geocoding.requests.get",
                   side_effect=responder(arkgis(addr_type="PointAddress", score=40), CENSUS_MISS)):
            self.assertIsNone(arkansas_gis_geocode("1 Main St", "Hampton", "AR", "71744"))

    def test_interpolated_street_address_is_accepted(self):
        with patch("inspections.geocoding.requests.get",
                   side_effect=responder(arkgis(addr_type="StreetAddressExt", score=98), CENSUS_MISS)):
            self.assertIsNotNone(arkansas_gis_geocode("1102 Country Club Pkwy", "Maumelle", "AR", "72113"))

    def test_a_rejected_arkgis_match_still_falls_through_to_census(self):
        facility = make_facility()
        with patch("inspections.geocoding.requests.get",
                   side_effect=responder(arkgis(addr_type="Locality", score=75), CENSUS_HIT)):
            self.assertEqual(locate_facility(facility), GeocodeSource.CENSUS)


@override_settings(ARKANSAS_GIS_DELAY=0, ARKANSAS_NG911_DELAY=0, CENSUS_GEOCODER_DELAY=0)
class RetryAndFailureTests(TestCase):
    def test_already_located_facility_is_left_alone(self):
        facility = make_facility()
        with patch("inspections.geocoding.requests.get", side_effect=responder(arkgis(), CENSUS_MISS)):
            locate_facility(facility)
        with patch("inspections.geocoding.requests.get") as m:
            self.assertIsNone(locate_facility(facility))
        m.assert_not_called()

    def test_total_miss_records_the_attempt(self):
        facility = make_facility()
        with patch("inspections.geocoding.requests.get", side_effect=responder(ARKGIS_EMPTY, CENSUS_MISS)):
            self.assertIsNone(locate_facility(facility))
        facility.refresh_from_db()
        self.assertIsNone(facility.location)
        self.assertEqual(facility.geocode_attempts, 1)

    @override_settings(MAX_GEOCODE_ATTEMPTS=2, ARKANSAS_GIS_DELAY=0,
                       ARKANSAS_NG911_DELAY=0, CENSUS_GEOCODER_DELAY=0)
    def test_gives_up_after_repeated_failures(self):
        facility = make_facility()
        with patch("inspections.geocoding.requests.get",
                   side_effect=responder(ARKGIS_EMPTY, CENSUS_MISS)) as m:
            for _ in range(4):
                locate_facility(facility)
            # 2 attempts x 3 services (composite, NG911, Census), then capped.
            self.assertEqual(m.call_count, 6)
        with patch("inspections.geocoding.requests.get", side_effect=responder(arkgis(), CENSUS_MISS)):
            self.assertEqual(locate_facility(facility, force=True), GeocodeSource.ARKANSAS_GIS)

    def test_network_failure_does_not_raise(self):
        import requests as req

        facility = make_facility()
        with patch("inspections.geocoding.requests.get", side_effect=req.Timeout("boom")):
            self.assertIsNone(locate_facility(facility))
        facility.refresh_from_db()
        self.assertEqual(facility.geocode_attempts, 1)

    @override_settings(GEOCODING_ENABLED=False)
    def test_can_be_switched_off(self):
        facility = make_facility()
        with patch("inspections.geocoding.requests.get") as m:
            self.assertIsNone(locate_facility(facility))
        m.assert_not_called()


@override_settings(ARKANSAS_GIS_DELAY=0, ARKANSAS_NG911_DELAY=0, CENSUS_GEOCODER_DELAY=0)
class InputGuardTests(TestCase):
    def test_no_street_means_no_lookup(self):
        with patch("inspections.geocoding.requests.get") as m:
            self.assertIsNone(arkansas_gis_geocode("", "Little Rock", "AR", "72201"))
            self.assertIsNone(census_geocode("", "Little Rock", "AR", "72201"))
        m.assert_not_called()

    def test_handles_malformed_json(self):
        with patch("inspections.geocoding.requests.get", side_effect=responder({}, {})):
            self.assertIsNone(arkansas_gis_geocode("1 Main St", "Little Rock", "AR", "72201"))
            self.assertIsNone(census_geocode("1 Main St", "Little Rock", "AR", "72201"))


@override_settings(ARKANSAS_GIS_DELAY=0, ARKANSAS_NG911_DELAY=0, CENSUS_GEOCODER_DELAY=0)
class NG911TierTests(TestCase):
    """NG911 sits between the composite locator and Census.

    It supplements rather than replaces: on real failures the two agree almost
    everywhere, and NG911's value is recovering from garbled input.
    """

    def test_it_is_tried_when_the_composite_locator_misses(self):
        facility = make_facility()
        with patch("inspections.geocoding.requests.get",
                   side_effect=responder(ARKGIS_EMPTY, CENSUS_MISS, arkgis(score=95))):
            source = locate_facility(facility)

        self.assertEqual(source, GeocodeSource.ARKANSAS_NG911)
        facility.refresh_from_db()
        self.assertIsNotNone(facility.location)
        self.assertEqual(facility.geocode_source, GeocodeSource.ARKANSAS_NG911)

    def test_it_is_not_called_when_the_composite_locator_succeeds(self):
        facility = make_facility()
        seen = []

        def _get(url, **kwargs):
            seen.append(url)
            return FakeResponse(arkgis())

        with patch("inspections.geocoding.requests.get", side_effect=_get):
            source = locate_facility(facility)

        self.assertEqual(source, GeocodeSource.ARKANSAS_GIS)
        self.assertFalse([u for u in seen if "NG911" in u])

    def test_it_runs_before_census(self):
        facility = make_facility()
        with patch("inspections.geocoding.requests.get",
                   side_effect=responder(ARKGIS_EMPTY, CENSUS_HIT, arkgis(score=95))):
            self.assertEqual(locate_facility(facility), GeocodeSource.ARKANSAS_NG911)

    def test_census_still_catches_what_both_locators_miss(self):
        facility = make_facility()
        with patch("inspections.geocoding.requests.get",
                   side_effect=responder(ARKGIS_EMPTY, CENSUS_HIT, ARKGIS_EMPTY)):
            self.assertEqual(locate_facility(facility), GeocodeSource.CENSUS)

    def test_it_is_held_to_a_higher_score_than_the_composite_locator(self):
        # 85 clears the composite locator's threshold of 80 but not NG911's 90.
        self.assertIsNone(
            arkansas_ng911_geocode("1033 Huddleston Street", "Arkadelphia", "AR", "71923",
                                   session=_session(arkgis(score=85)))
        )
        self.assertIsNotNone(
            arkansas_gis_geocode("1033 Huddleston Street", "Arkadelphia", "AR", "71923",
                                 session=_session(arkgis(score=85)))
        )

    def test_a_po_box_match_is_refused_however_confident(self):
        """Observed for real: "ROUTE 2, BOX 8" came back as a street called
        "PO BOX" at score 82. A PO box is a mail destination, not a place."""
        payload = arkgis(addr_type="StreetAddress", score=99,
                         match="2 PO BOX, CENTER RIDGE, AR, 72027")
        for geocoder in (arkansas_ng911_geocode, arkansas_gis_geocode):
            self.assertIsNone(
                geocoder("ROUTE 2, BOX 8", "Center Ridge", "AR", "72027",
                         session=_session(payload)),
                geocoder.__name__,
            )


class _session:
    """Minimal stand-in for a requests.Session that always answers the same."""

    def __init__(self, payload):
        self._payload = payload

    def get(self, url, **kwargs):
        return FakeResponse(self._payload)


@override_settings(ARKANSAS_GIS_DELAY=0, ARKANSAS_NG911_DELAY=0, CENSUS_GEOCODER_DELAY=0,
                   MAX_GEOCODE_ATTEMPTS=3)
class ReviewFlagTests(TestCase):
    """Exhausted retries put the address in front of a person.

    Most addresses that get this far are not geocoder problems — a renamed
    street, a business route the locators know by another name, a rural-route
    box with no physical point. Retrying never fixes those.
    """

    def miss(self, facility):
        with patch("inspections.geocoding.requests.get",
                   side_effect=responder(ARKGIS_EMPTY, CENSUS_MISS, ARKGIS_EMPTY)):
            return locate_facility(facility)

    def test_not_flagged_while_attempts_remain(self):
        facility = make_facility()
        self.miss(facility)
        facility.refresh_from_db()
        self.assertEqual(facility.geocode_attempts, 1)
        self.assertFalse(facility.address_needs_review)

    def test_flagged_once_the_attempts_are_spent(self):
        facility = make_facility()
        for _ in range(3):
            self.miss(facility)
        facility.refresh_from_db()
        self.assertEqual(facility.geocode_attempts, 3)
        self.assertTrue(facility.address_needs_review)

    def test_a_success_never_raises_the_flag(self):
        facility = make_facility()
        with patch("inspections.geocoding.requests.get",
                   side_effect=responder(arkgis(), CENSUS_MISS)):
            locate_facility(facility)
        facility.refresh_from_db()
        self.assertFalse(facility.address_needs_review)

    def test_an_existing_flag_is_left_alone(self):
        # The address parser raises this for its own reasons; geocoding must not
        # clear it, and a human unticks it once they have fixed the address.
        facility = make_facility()
        facility.address_needs_review = True
        facility.save(update_fields=["address_needs_review"])
        for _ in range(3):
            self.miss(facility)
        facility.refresh_from_db()
        self.assertTrue(facility.address_needs_review)
