"""Geo helpers — city → lat/lng cache + haversine distance.

Used by the scheduler's distance filter in poll_search. FB returns
listings as city-name strings (e.g. "Vancouver, British Columbia") and
ignores the `filter_radius_km` request parameter for small radii. We
geocode each city once via Nominatim, cache it in Postgres, and
haversine-filter listings client-side.

Design:
  - Cache hits never round-trip to Nominatim. Misses do (slow first
    time per new city, then forever fast).
  - Failures are cached as (NULL, NULL) so we don't keep retrying a
    bad string. The filter treats NULL coords as "unknown distance"
    and lets the listing through (fail-open) — better to occasionally
    show an out-of-range listing than to drop a real one because we
    couldn't geocode the city name.
  - Nominatim is rate-limited (1 req/sec). We don't enforce that
    here; the scheduler's coordinator_tick already throttles the
    scrape rate, and at 9s/poll we're well under 1 geocode/sec.
"""
from __future__ import annotations

import logging
import math

import requests

from .connection import get_conn

logger = logging.getLogger(__name__)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_USER_AGENT = "bullseye-deal-finder/0.1 (github.com/reubenlavin08/bullseye)"

# Earth radius in km for haversine.
_EARTH_KM = 6371.0


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two lat/lng points in kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlng / 2) ** 2)
    return _EARTH_KM * 2 * math.asin(math.sqrt(a))


def geocode_city(label: str) -> tuple[float, float] | None:
    """Look up a city display-name's lat/lng. Cache-first.

    Returns (lat, lng) on success, None when the geocoder couldn't find
    anything OR when geocoding has previously failed for this label.
    Never raises — best-effort.
    """
    if not label:
        return None
    norm = label.strip()
    if not norm:
        return None

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT latitude, longitude FROM city_geocache WHERE label = %s",
                    (norm,),
                )
                row = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        logger.warning("geocode_city cache lookup failed for %r: %s", norm, e)
        row = None

    if row is not None:
        # Cache hit — including negative results (NULL,NULL) so we don't retry.
        if row[0] is None or row[1] is None:
            return None
        return float(row[0]), float(row[1])

    # Miss — hit Nominatim
    coords = _query_nominatim(norm)
    try:
        with get_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO city_geocache (label, latitude, longitude)
                           VALUES (%s, %s, %s)
                           ON CONFLICT (label) DO UPDATE SET
                             latitude = EXCLUDED.latitude,
                             longitude = EXCLUDED.longitude,
                             fetched_at = NOW()""",
                        (norm, coords[0] if coords else None,
                         coords[1] if coords else None),
                    )
    except Exception as e:  # noqa: BLE001
        logger.warning("geocode_city cache write failed for %r: %s", norm, e)

    return coords


def _query_nominatim(q: str) -> tuple[float, float] | None:
    try:
        resp = requests.get(
            _NOMINATIM_URL,
            params={"q": q, "format": "json", "limit": 1, "addressdetails": 0},
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "en"},
            timeout=4.0,
        )
        resp.raise_for_status()
        items = resp.json()
        if not items:
            return None
        first = items[0]
        return float(first["lat"]), float(first["lon"])
    except Exception as e:  # noqa: BLE001 — never crash the scheduler on a geocode error
        logger.warning("nominatim query failed for %r: %s", q, e)
        return None
