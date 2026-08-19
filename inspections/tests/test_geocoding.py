"""Geocoding tests. Both services are mocked: these assert our decision logic —
source ordering, precision filtering, and retry limits — not theirs."""

from unittest.mock import patch

from django.test import TestCase, override_settings

from inspections.geocoding import arkansas_gis_geocode, census_geocode, locate_facility
from inspections.models import Facility, GeocodeSource, fingerprint


def arkgis(addr_type="PointAddress", score=100, x=-93.058088, y=34.129376):
    return {
        "candidates": [
            {
                "location": {"x": x, "y": y},
                "attributes": {
                    "Addr_type": addr_type,
                    "Score": score,
                    "Match_addr": "1033 Huddleston St, Arkadelphia, AR, 71923",
                },
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


def responder(arkgis_payload, census_payload):
    """Dispatch a mocked GET to the right fake payload by URL."""

    def _get(url, **kwargs):
        return FakeResponse(arkgis_payload if "gis.arkansas.gov" in url else census_payload)

    return _get


def make_facility(name="TEST CAFE", street="1033 Huddleston Street"):
    return Facility.objects.create(
        name=name, street=street, city="Arkadelphia", state="AR", zip_code="71923",
        fingerprint=fingerprint(name, street, "71923"),
    )


@override_settings(ARKANSAS_GIS_DELAY=0, CENSUS_GEOCODER_DELAY=0)
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


@override_settings(ARKANSAS_GIS_DELAY=0, CENSUS_GEOCODER_DELAY=0)
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


@override_settings(ARKANSAS_GIS_DELAY=0, CENSUS_GEOCODER_DELAY=0)
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

    @override_settings(MAX_GEOCODE_ATTEMPTS=2, ARKANSAS_GIS_DELAY=0, CENSUS_GEOCODER_DELAY=0)
    def test_gives_up_after_repeated_failures(self):
        facility = make_facility()
        with patch("inspections.geocoding.requests.get",
                   side_effect=responder(ARKGIS_EMPTY, CENSUS_MISS)) as m:
            for _ in range(4):
                locate_facility(facility)
            self.assertEqual(m.call_count, 4)  # 2 attempts x 2 services, then capped
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


@override_settings(ARKANSAS_GIS_DELAY=0, CENSUS_GEOCODER_DELAY=0)
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
