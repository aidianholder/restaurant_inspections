"""Give every facility a location.

Two sources, in order:

1. **The Arkansas GIS Office's statewide composite locator.** Returns real
   address points rather than interpolated street ranges, already in WGS84, and
   resolves addresses the Census data has never heard of. Matches are accepted
   only at address precision: the locator answers *something* for nearly every
   query, but a `Locality` match is just a city centroid.
2. **The Census Bureau geocoder**, as a fallback.

Both record the address they actually matched, so a bad match is visible rather
than silent.

Deliberately *not* used: the coordinates the ADH search page emits when "Display
Map" is ticked. Checked against the state locator, a third of them were more than
300m from the address and one sat 129km away, in the wrong half of the state.

A geocoding failure never fails a scrape; the facility simply keeps a null
location and can be retried later with `manage.py geocode_facilities`.
"""

import logging
import time

import requests
from django.conf import settings
from django.contrib.gis.geos import Point
from django.utils import timezone

from .models import GeocodeSource

logger = logging.getLogger(__name__)

_last_call = {}


def _wait(key, delay):
    """Space out calls to a remote geocoding service."""
    elapsed = time.monotonic() - _last_call.get(key, 0.0)
    if elapsed < delay:
        time.sleep(delay - elapsed)
    _last_call[key] = time.monotonic()


def _one_line(street, city, state, zip_code):
    parts = [p for p in [street, city, " ".join(filter(None, [state, zip_code]))] if p]
    return ", ".join(parts).strip()


def arkansas_gis_geocode(street, city, state, zip_code, session=None):
    """Arkansas GIS composite locator. Returns {lat, lon, matched_address} or None.

    Rejects anything short of address precision. The locator will happily answer
    a hopeless query with the containing city's centroid (`Addr_type: Locality`),
    which would scatter pins across town centres.
    """
    address = _one_line(street, city, state, zip_code)
    if not address or not street:
        return None

    _wait("arkansas_gis", getattr(settings, "ARKANSAS_GIS_DELAY", 0.25))
    try:
        getter = session.get if session else requests.get
        response = getter(
            settings.ARKANSAS_GIS_GEOCODER_URL,
            params={
                "SingleLine": address,
                "f": "json",
                "outFields": "Addr_type,Score,Match_addr",
                "maxLocations": 1,
                "outSR": 4326,
            },
            timeout=settings.ARKANSAS_GIS_TIMEOUT,
        )
        response.raise_for_status()
        candidates = response.json().get("candidates") or []
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Arkansas GIS geocode failed for %r: %s", address, exc)
        return None

    if not candidates:
        return None

    best = candidates[0]
    attrs = best.get("attributes") or {}
    addr_type = attrs.get("Addr_type", "")
    score = float(attrs.get("Score") or 0)

    if addr_type not in settings.ARKANSAS_GIS_ACCEPTED_TYPES:
        logger.info("Arkansas GIS returned %s (too coarse) for %r", addr_type or "?", address)
        return None
    if score < settings.ARKANSAS_GIS_MIN_SCORE:
        logger.info("Arkansas GIS score %.1f below threshold for %r", score, address)
        return None

    location = best.get("location") or {}
    if location.get("x") is None or location.get("y") is None:
        return None

    return {
        "longitude": float(location["x"]),
        "latitude": float(location["y"]),
        "matched_address": f"{attrs.get('Match_addr', '')} [{addr_type} {score:.0f}]".strip(),
    }


def census_geocode(street, city, state, zip_code, session=None):
    """One-line address lookup. Returns {lat, lon, matched_address} or None."""
    address = _one_line(street, city, state, zip_code)
    if not address or not street:
        return None

    _wait("census", getattr(settings, "CENSUS_GEOCODER_DELAY", 0.5))
    try:
        getter = session.get if session else requests.get
        response = getter(
            settings.CENSUS_GEOCODER_URL,
            params={
                "address": address,
                "benchmark": settings.CENSUS_GEOCODER_BENCHMARK,
                "format": "json",
            },
            timeout=settings.CENSUS_GEOCODER_TIMEOUT,
        )
        response.raise_for_status()
        matches = response.json().get("result", {}).get("addressMatches", [])
    except (requests.RequestException, ValueError) as exc:
        # Network trouble or a non-JSON error page: skip, don't fail the run.
        logger.warning("Census geocode failed for %r: %s", address, exc)
        return None

    if not matches:
        return None

    match = matches[0]
    coords = match.get("coordinates") or {}
    if coords.get("x") is None or coords.get("y") is None:
        return None

    return {
        "longitude": float(coords["x"]),
        "latitude": float(coords["y"]),
        "matched_address": match.get("matchedAddress", ""),
    }


def _save_location(facility, longitude, latitude, source, matched_address=""):
    facility.location = Point(float(longitude), float(latitude), srid=4326)
    facility.geocode_source = source
    facility.geocode_matched_address = matched_address
    facility.geocoded_at = timezone.now()
    facility.geocode_last_attempt = facility.geocoded_at
    facility.geocode_attempts += 1
    facility.save(
        update_fields=[
            "location", "geocode_source", "geocode_matched_address",
            "geocoded_at", "geocode_last_attempt", "geocode_attempts",
        ]
    )
    return source


def locate_facility(facility, force=False, session=None):
    """Attach a location to one facility. Returns the source used, or None.

    Called when a facility is first identified. Already-located facilities are
    left alone unless `force` is set, so re-running a scrape doesn't re-geocode
    the world. Facilities that have failed repeatedly are also skipped: roughly
    one address in seven isn't in the Census address ranges at all, and those
    should be corrected by hand rather than re-queried forever.
    """
    if not getattr(settings, "GEOCODING_ENABLED", True):
        return None
    if facility.location is not None and not force:
        return None

    max_attempts = getattr(settings, "MAX_GEOCODE_ATTEMPTS", 3)
    if facility.geocode_attempts >= max_attempts and not force:
        return None

    args = (facility.street, facility.city, facility.state, facility.zip_code)
    for geocoder, source in (
        (arkansas_gis_geocode, GeocodeSource.ARKANSAS_GIS),
        (census_geocode, GeocodeSource.CENSUS),
    ):
        result = geocoder(*args, session=session)
        if result:
            return _save_location(
                facility,
                result["longitude"],
                result["latitude"],
                source,
                result["matched_address"],
            )

    facility.geocode_attempts += 1
    facility.geocode_last_attempt = timezone.now()
    facility.save(update_fields=["geocode_attempts", "geocode_last_attempt"])
    logger.info(
        "No location found for %s (%s) — attempt %d",
        facility.name, facility.address_display, facility.geocode_attempts,
    )
    return None
