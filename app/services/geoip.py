"""Where a sign-in came from, as far as an IP address can honestly say.

Three things this deliberately does NOT do.

*It does not claim to locate a person.* An IP resolves to a city at best, and
often to the carrier's egress point — a teacher in Zgharta on mobile data can
resolve to Beirut. The label is evidence that a sign-in came from somewhere
unusual, never an address.

*It does not run inside the login request.* A slow or dead lookup must never
delay or fail a sign-in, so resolution happens when an admin reads the log and
the answer is written back to the row. Everything here returns a value or None;
nothing raises.

*It does not send anyone's IP anywhere by default.* GEOIP_PROVIDER is "none"
until somebody chooses otherwise, and with it off the screen shows "—" instead
of the coordinates it used to invent. "maxmind" reads a local GeoLite2 file and
never leaves the machine; "http" calls a third-party service, which means
handing that service your teachers' addresses — a deliberate decision, not a
default.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger("app.geoip")

# Resolved addresses are stored on the row, so this only absorbs repeats within
# one page view. Small and short-lived on purpose.
_CACHE: dict[str, tuple[float, "Place | None"]] = {}
_CACHE_TTL = 60 * 60  # 1 hour
_CACHE_MAX = 512


@dataclass(frozen=True)
class Place:
    label: str
    lat: float | None = None
    lng: float | None = None


LOCAL = Place("Local network")


def _private(ip: str) -> bool:
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return parsed.is_private or parsed.is_loopback or parsed.is_link_local


def _from_cache(ip: str) -> tuple[bool, "Place | None"]:
    hit = _CACHE.get(ip)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return True, hit[1]
    return False, None


def _remember(ip: str, place: Place | None) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[ip] = (time.time(), place)


def _maxmind(ip: str) -> Place | None:
    """Local GeoLite2 lookup. Nothing leaves the machine."""
    path = settings.geoip_db_path
    if not path:
        return None
    try:
        import geoip2.database  # imported lazily: the package is optional
    except ImportError:
        logger.warning("GEOIP_PROVIDER=maxmind but the geoip2 package isn't installed")
        return None
    try:
        with geoip2.database.Reader(path) as reader:
            found = reader.city(ip)
            city = found.city.name
            country = found.country.name
            label = ", ".join(part for part in (city, country) if part) or "Unknown"
            return Place(label, found.location.latitude, found.location.longitude)
    except Exception:
        return None


# Field names differ per vendor; accept the shapes the common free services use
# so switching provider doesn't mean a code change.
_CITY_KEYS = ("city", "city_name")
_COUNTRY_KEYS = ("country_name", "country", "countryName")
_LAT_KEYS = ("latitude", "lat")
_LNG_KEYS = ("longitude", "lon", "lng")


def _first(payload: dict, keys: tuple[str, ...]):
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _http(ip: str) -> Place | None:
    url = settings.geoip_api_url
    if not url:
        return None
    try:
        import httpx

        response = httpx.get(url.replace("{ip}", ip), timeout=3.0)
        if response.status_code != 200:
            return None
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        city = _first(payload, _CITY_KEYS)
        country = _first(payload, _COUNTRY_KEYS)
        label = ", ".join(str(p) for p in (city, country) if p)
        if not label:
            return None
        lat, lng = _first(payload, _LAT_KEYS), _first(payload, _LNG_KEYS)
        return Place(
            label,
            float(lat) if lat is not None else None,
            float(lng) if lng is not None else None,
        )
    except Exception:
        # A geo lookup is never worth failing a page over.
        return None


def enabled() -> bool:
    return settings.geoip_provider in ("maxmind", "http")


def locate(ip: str) -> Place | None:
    """Best-effort place for an IP. None when unknown or lookups are off."""
    ip = (ip or "").strip()
    if not ip:
        return None
    if _private(ip):
        return LOCAL
    if not enabled():
        return None

    cached, place = _from_cache(ip)
    if cached:
        return place

    place = _maxmind(ip) if settings.geoip_provider == "maxmind" else _http(ip)
    _remember(ip, place)
    return place
