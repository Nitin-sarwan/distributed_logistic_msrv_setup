"""Address text <-> coordinates.

The one place in the backend that talks to a geocoding provider. Everything
above it — the routes, and the frontend behind them — sees `Place`, never a
provider's JSON, so swapping Nominatim for Google/Mapbox/Photon is a change to
this file and to nothing else.

Why the frontend does not call the provider itself:

* **Policy.** Nominatim requires an identifying User-Agent and at most one
  request per second. A browser cannot be made to honour either — every tab is
  its own rate limiter, and the UA is the browser's.
* **Caching.** "Indiranagar, Bengaluru" resolves to the same point for every
  user who types it. One cache here answers all of them; a cache per browser
  answers none of them twice.
* **Topology.** The frontend knows the gateway and nothing else. Letting it
  reach a third-party host directly would be the first exception to that, and
  the one that makes swapping providers a frontend release.

Coordinates are returned as plain floats. Six decimal places (~10cm) is what the
`address` table stores, and float64 represents that exactly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from src.services.userServices.config import settings

logger = logging.getLogger(__name__)


class GeocoderUnavailableError(Exception):
    """The provider could not be reached, or answered with something unusable.

    Deliberately one error rather than several. A caller cannot do anything
    different about a timeout than about a 502 from Nominatim: the answer to the
    user is "search is not working, place the pin by hand", and the map still
    works either way.
    """

    def __init__(self, message: str = "Address lookup is unavailable right now."):
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Place:
    """One resolved location.

    The fields are the ones the `address` table needs, not the ones Nominatim
    happens to return — this is the shape the address form fills itself from, so
    a lookup result can be saved without the frontend re-deriving anything.

    `label` is the human sentence to show in a suggestion list. It is separate
    from `address_line1` on purpose: the list wants "Toit, 100 Feet Road,
    Indiranagar, Bengaluru", the form field wants "100 Feet Road".
    """

    latitude: float
    longitude: float
    label: str
    address_line1: str
    address_line2: str | None
    city: str
    pin_code: str | None
    # Stable per result, so the frontend has a React key that does not shift
    # when the same query is re-run.
    place_id: str


# Nominatim keys that can carry the settlement name, most specific first. A
# village has no `city` key at all, and taking `state` for one would put
# "Karnataka" in a field the driver reads as the town.
_CITY_KEYS = (
    "city",
    "town",
    "municipality",
    "village",
    "hamlet",
    "suburb",
    "county",
    "state_district",
    "state",
)

# The area *inside* a settlement — what a person writes on line 2.
_AREA_KEYS = ("neighbourhood", "quarter", "suburb", "city_district", "village")

# Keys that name the thing itself rather than the street it sits on, used when
# there is no road: a warehouse, a mall, a railway station.
_NAME_KEYS = (
    "amenity",
    "building",
    "shop",
    "office",
    "industrial",
    "commercial",
    "leisure",
    "tourism",
    "railway",
    "aeroway",
)


def _first(address: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = address.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _line1(raw: dict, address: dict) -> str:
    """The street line: house number + road, or the name of the place itself."""
    road = address.get("road")
    house_number = address.get("house_number")

    if road:
        return f"{house_number}, {road}" if house_number else str(road)

    named = raw.get("name") or _first(address, _NAME_KEYS)
    if named:
        return str(named)

    # Last resort: the first comma-separated chunk of the display name, which is
    # the most specific part of it.
    display = str(raw.get("display_name", "")).split(",")[0].strip()
    return display or "Unnamed location"


def _pin_code(address: dict) -> str | None:
    """The postcode, if it is one this system can store.

    `address.pin_code` is VARCHAR(6) and validated as six digits on the way in,
    so a UK or US postcode has to come back as None rather than as a value the
    form would submit and the API would reject. The map still gives a usable
    pin; only this one field is left for the user to fill.
    """
    postcode = address.get("postcode")
    if not isinstance(postcode, str):
        return None

    digits = "".join(character for character in postcode if character.isdigit())
    return digits if len(digits) == 6 else None


def _to_place(raw: dict) -> Place | None:
    """Map one Nominatim result. None when it carries no usable coordinate."""
    try:
        latitude = float(raw["lat"])
        longitude = float(raw["lon"])
    except (KeyError, TypeError, ValueError):
        # A result without a parseable position is not a location. Dropping it
        # is right: the alternative is a suggestion that puts a pin at (0, 0).
        return None

    address = raw.get("address") or {}
    line1 = _line1(raw, address)
    area = _first(address, _AREA_KEYS)

    return Place(
        latitude=latitude,
        longitude=longitude,
        label=str(raw.get("display_name") or line1),
        address_line1=line1,
        # Never repeat line 1 on line 2 — Nominatim will happily report the
        # suburb as both when the road *is* the suburb's main road.
        address_line2=area if area and area != line1 else None,
        city=_first(address, _CITY_KEYS) or "",
        pin_code=_pin_code(address),
        place_id=str(raw.get("place_id") or f"{latitude},{longitude}"),
    )


class _TtlCache:
    """A tiny time-boxed cache.

    In-process and per-worker, which is the honest description: two uvicorn
    workers keep two caches and the hit rate halves. That is fine at this size —
    the cache exists to stay inside the provider's rate limit and to make a
    type-ahead feel instant, not to be a system of record. Redis is the answer
    if this ever needs to be shared, and it is one class to swap.

    Eviction is oldest-inserted-first rather than least-recently-used. LRU needs
    a touch on every read; for a few thousand short-lived entries the difference
    is not measurable, and this cannot get its bookkeeping wrong.
    """

    def __init__(self, ttl_seconds: int, max_entries: int):
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._entries: dict[str, tuple[float, list[Place]]] = {}

    def get(self, key: str) -> list[Place] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None

        expires_at, value = entry
        if expires_at < time.monotonic():
            self._entries.pop(key, None)
            return None

        return value

    def put(self, key: str, value: list[Place]) -> None:
        if len(self._entries) >= self._max_entries:
            # dicts iterate in insertion order, so this is the oldest key.
            oldest = next(iter(self._entries), None)
            if oldest is not None:
                self._entries.pop(oldest, None)

        self._entries[key] = (time.monotonic() + self._ttl, value)


_cache = _TtlCache(
    ttl_seconds=settings.geocoder_cache_ttl_seconds,
    max_entries=settings.geocoder_cache_max_entries,
)

# One client for the process. Created lazily rather than at import, so that
# importing this module — which Alembic and the test suite both do — never opens
# a socket.
_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()

# Enforces the provider's request rate across every caller in this process.
# `_throttle_lock` is held for the wait as well as the send, which is what makes
# the interval a real floor: releasing it before sleeping would let a hundred
# concurrent requests each measure the same "last request" and fire together.
_throttle_lock = asyncio.Lock()
_last_request_at = 0.0


def _headers() -> dict[str, str]:
    """Identify this deployment to the provider.

    Nominatim's usage policy requires a User-Agent that identifies the
    application, and blocks traffic that does not carry one.
    GEOCODER_CONTACT_EMAIL is the second half of that contract: it is how they
    reach an operator before resorting to a block.
    """
    headers = {
        "User-Agent": settings.geocoder_user_agent,
        "Accept-Language": "en",
    }
    if settings.geocoder_contact_email:
        headers["From"] = settings.geocoder_contact_email
    return headers


async def _get_client() -> httpx.AsyncClient:
    global _client

    if _client is None:
        async with _client_lock:
            # Re-check inside the lock: two coroutines can both find it None.
            if _client is None:
                _client = httpx.AsyncClient(
                    base_url=settings.geocoder_base_url.rstrip("/"),
                    timeout=settings.geocoder_timeout_seconds,
                    headers=_headers(),
                    follow_redirects=True,
                )

    return _client


async def close_geocoder() -> None:
    """Release the HTTP client. Called from the service's lifespan shutdown."""
    global _client

    if _client is not None:
        await _client.aclose()
        _client = None


async def _request(path: str, params: dict) -> list[dict]:
    """One throttled call to the provider, normalised to a list of results."""
    global _last_request_at

    client = await _get_client()

    async with _throttle_lock:
        wait = settings.geocoder_min_interval_seconds - (
            time.monotonic() - _last_request_at
        )
        if wait > 0:
            await asyncio.sleep(wait)

        try:
            response = await client.get(path, params=params)
        except httpx.HTTPError as error:
            logger.warning("Geocoder request failed: %s", error)
            raise GeocoderUnavailableError() from error
        finally:
            # Stamped even on failure: a provider that just refused a connection
            # should not be hammered at full speed by the next caller.
            _last_request_at = time.monotonic()

    if response.status_code >= 400:
        logger.warning("Geocoder answered %s for %s", response.status_code, path)
        raise GeocoderUnavailableError()

    try:
        payload = response.json()
    except ValueError as error:
        raise GeocoderUnavailableError() from error

    # /search answers with a list, /reverse with a single object. Normalising
    # here keeps both callers on one code path.
    if isinstance(payload, dict):
        return [] if payload.get("error") else [payload]
    return payload if isinstance(payload, list) else []


async def search(
    query: str,
    limit: int,
    near: tuple[float, float] | None = None,
) -> list[Place]:
    """Find places matching free text, best match first.

    `near` biases the results towards a point without excluding anywhere else —
    someone typing "MG Road" while looking at Bengaluru means the one in
    Bengaluru, but a hard filter would hide the one they actually wanted if they
    had panned the map elsewhere.
    """
    cleaned = query.strip()
    if len(cleaned) < 3:
        # Below three characters every provider returns noise. Answering with
        # nothing costs one request less and reads identically to the user.
        return []

    cache_key = f"s:{cleaned.casefold()}:{limit}:{near}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    params: dict[str, str | int] = {
        "q": cleaned,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": limit,
    }

    if settings.geocoder_country_codes:
        params["countrycodes"] = settings.geocoder_country_codes

    if near is not None:
        latitude, longitude = near
        # A half-degree box, roughly 55km. `bounded` is deliberately not set, so
        # this ranks rather than restricts.
        params["viewbox"] = (
            f"{longitude - 0.5},{latitude + 0.5},{longitude + 0.5},{latitude - 0.5}"
        )

    raw_results = await _request("/search", params)
    results = [place for place in (_to_place(raw) for raw in raw_results) if place]

    _cache.put(cache_key, results)
    return results


async def reverse(latitude: float, longitude: float) -> Place | None:
    """The address at a point, or None if the provider knows of nothing there.

    None is a normal answer — a pin dropped at sea or on unmapped land has no
    address — and the caller shows the coordinates alone rather than an error.
    The pin is still valid; a driver can be sent to it.
    """
    # Six decimals is the stored precision, so rounding the key here means a pin
    # nudged by a pixel reuses the cached answer instead of spending a request
    # on a difference nothing can represent.
    cache_key = f"r:{latitude:.6f},{longitude:.6f}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached[0] if cached else None

    results = await _request(
        "/reverse",
        {
            "lat": f"{latitude:.6f}",
            "lon": f"{longitude:.6f}",
            "format": "jsonv2",
            "addressdetails": 1,
            # 18 is building level. Zooming out to a suburb would be less typing
            # for the user and the wrong pickup point for the driver.
            "zoom": 18,
        },
    )

    place = _to_place(results[0]) if results else None
    _cache.put(cache_key, [place] if place else [])
    return place
