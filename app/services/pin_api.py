"""India Post PIN lookup client (api.postalpincode.in).

This is the application's serviceability check: a PIN the service cannot resolve
is not treated as deliverable. The client never raises on a network problem --
it returns a status the rule engine and the agent can talk about, so an outage
degrades the conversation instead of breaking it.
"""

import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# PIN -> result. PIN data is effectively immutable, so caching is safe and
# keeps a multi-turn conversation from re-querying the same PIN repeatedly.
_cache: dict[str, dict] = {}

STATUS_OK = "ok"
STATUS_NOT_SERVICEABLE = "not_serviceable"
STATUS_UNAVAILABLE = "unavailable"
STATUS_INVALID = "invalid"


def clear_cache() -> None:
    """Used by tests, and by anything that needs a genuinely fresh lookup."""
    _cache.clear()


def lookup_pin(pin: str) -> dict:
    """Resolve an Indian PIN code to its district/state.

    Returns a dict with `status` in {ok, not_serviceable, unavailable, invalid}.
    Never raises.
    """
    pin = str(pin or "").strip()

    if not (pin.isdigit() and len(pin) == 6):
        return {
            "status": STATUS_INVALID,
            "pin": pin,
            "message": "An Indian PIN code must be exactly six digits.",
        }

    if pin in _cache:
        return _cache[pin]

    settings = get_settings()
    url = f"{settings.pin_api_base_url}/pincode/{pin}"

    try:
        response = httpx.get(url, timeout=settings.pin_api_timeout_seconds)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        # Deliberately not cached: the service may recover on the next turn.
        logger.warning("PIN lookup failed for %s: %s", pin, exc)
        return {
            "status": STATUS_UNAVAILABLE,
            "pin": pin,
            "message": (
                "The PIN lookup service is not reachable at the moment, so this "
                "PIN code could not be verified."
            ),
            "error": str(exc),
        }

    record = payload[0] if isinstance(payload, list) and payload else {}
    offices = record.get("PostOffice") or []

    if record.get("Status") != "Success" or not offices:
        result = {
            "status": STATUS_NOT_SERVICEABLE,
            "pin": pin,
            "message": f"PIN code {pin} was not found in the India Post directory.",
        }
        _cache[pin] = result
        return result

    first = offices[0]
    result = {
        "status": STATUS_OK,
        "pin": pin,
        "city": first.get("District"),
        "district": first.get("District"),
        "state": first.get("State"),
        "region": first.get("Region"),
        # Useful when the user is unsure of the locality name for their address.
        "post_offices": [o.get("Name") for o in offices[:8] if o.get("Name")],
    }
    _cache[pin] = result
    return result


# A city and its administrative district frequently carry different names in
# India, and many cities were renamed while India Post data still uses the older
# form. Treating those as conflicts would block legitimate bookings, so known
# equivalents are grouped here.
CITY_ALIAS_GROUPS = [
    {"bengaluru", "bangalore"},
    {"kochi", "cochin", "ernakulam"},
    {"chennai", "madras"},
    {"mumbai", "bombay"},
    {"kolkata", "calcutta"},
    {"pune", "poona"},
    {"thiruvananthapuram", "trivandrum"},
    {"puducherry", "pondicherry"},
    {"vadodara", "baroda"},
    {"prayagraj", "allahabad"},
    {"gurugram", "gurgaon"},
    {"mysuru", "mysore"},
    {"varanasi", "banaras", "benares"},
    {"visakhapatnam", "vizag"},
    {"tiruchirappalli", "trichy"},
    {"noida", "gautam buddha nagar", "gautam buddh nagar"},
    {"hyderabad", "rangareddy", "ranga reddy", "medchal malkajgiri"},
]

# Administrative suffixes that carry no identifying meaning: "Bengaluru Urban"
# and "Kanpur Nagar" are the same place as "Bengaluru" and "Kanpur".
_DISTRICT_SUFFIXES = {"urban", "rural", "city", "district", "dist", "nagar", "metropolitan"}


def _normalise_place(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in name.lower())
    tokens = cleaned.split()
    while len(tokens) > 1 and tokens[-1] in _DISTRICT_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _same_place(given: str, api: str) -> bool:
    given, api = _normalise_place(given), _normalise_place(api)
    if not given or not api:
        return False
    if given == api or given in api or api in given:
        return True
    return any(
        given in group and api in group for group in CITY_ALIAS_GROUPS
    )


def compare_city(pin_result: dict, given_city: str | None) -> dict:
    """Compare a user-stated city against the PIN's real district.

    Documented policy: the PIN lookup is the source of truth. A genuine
    disagreement is surfaced to the user for resolution -- never silently
    corrected, never silently accepted.
    """
    if pin_result.get("status") != STATUS_OK:
        return pin_result

    api_city = (pin_result.get("city") or "").strip()
    api_state = (pin_result.get("state") or "").strip()

    if not given_city or not api_city:
        return pin_result

    # A match against the district, the state, or any post office name in that
    # PIN all count as agreement -- users name their locality, not their district.
    candidates = [api_city, api_state, *(pin_result.get("post_offices") or [])]
    if any(_same_place(given_city, candidate) for candidate in candidates if candidate):
        return pin_result

    return {
        **pin_result,
        "status": "mismatch",
        "given_city": given_city,
        "api_city": api_city,
        "api_state": api_state,
        "message": (
            f"PIN {pin_result['pin']} belongs to {api_city}, {api_state}, "
            f"but the address given says {given_city}."
        ),
    }
