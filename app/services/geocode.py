"""Distance between two PIN codes.

Two steps, as designed:
  1. Geocode each PIN to latitude/longitude via OpenStreetMap Nominatim (free,
     no signup, but requires a real User-Agent and at most 1 request/second).
  2. Compute the great-circle distance locally with the haversine formula --
     no service, no key, no failure mode.

If a PIN cannot be geocoded the result is `unknown`. It is never estimated,
and an unknown distance must not block a booking.
"""

import logging
import math
import threading
import time

import httpx

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# Nominatim's usage policy requires an identifying User-Agent; requests without
# one are rejected.
USER_AGENT = "IndiaShipments-Agent/0.1 (interview challenge project)"
REQUEST_TIMEOUT_SECONDS = 6.0
MIN_SECONDS_BETWEEN_REQUESTS = 1.1  # policy: max 1 request per second

EARTH_RADIUS_KM = 6371.0

_cache: dict[str, dict] = {}
_rate_lock = threading.Lock()
_last_request_at = 0.0


def clear_cache() -> None:
    _cache.clear()


def _throttle() -> None:
    """Serialise outbound calls so we stay inside Nominatim's rate limit."""
    global _last_request_at
    with _rate_lock:
        wait = MIN_SECONDS_BETWEEN_REQUESTS - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _query_nominatim(params: dict) -> list:
    _throttle()
    response = httpx.get(
        NOMINATIM_URL,
        params={**params, "format": "json", "limit": 1},
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def geocode_pin(pin: str, fallback_place: str | None = None) -> dict:
    """Resolve a PIN code to coordinates.

    `fallback_place` (e.g. "Ernakulam, Kerala" from the PIN lookup) is tried when
    the PIN itself is not tagged in OpenStreetMap. Never raises.
    """
    pin = str(pin or "").strip()
    cache_key = f"{pin}|{fallback_place or ''}"
    if cache_key in _cache:
        return _cache[cache_key]

    attempts = [({"postalcode": pin, "country": "India"}, "postalcode")]
    if fallback_place:
        attempts.append(({"q": f"{fallback_place}, India"}, "place_name"))

    for params, source in attempts:
        try:
            results = _query_nominatim(params)
        except Exception as exc:
            logger.warning("Geocoding failed for %s via %s: %s", pin, source, exc)
            return {
                "status": "unavailable",
                "pin": pin,
                "message": (
                    "The geocoding service is not reachable at the moment, so the "
                    "distance could not be calculated."
                ),
                "error": str(exc),
            }

        if results:
            hit = results[0]
            result = {
                "status": "ok",
                "pin": pin,
                "lat": float(hit["lat"]),
                "lon": float(hit["lon"]),
                "display_name": hit.get("display_name"),
                "source": source,
            }
            _cache[cache_key] = result
            return result

    result = {
        "status": "not_found",
        "pin": pin,
        "message": f"No coordinates are available for PIN code {pin}.",
    }
    _cache[cache_key] = result
    return result


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))
