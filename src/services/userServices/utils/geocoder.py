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
import re
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


# Mainland India plus the Andaman, Nicobar and Lakshadweep islands, as
# min_lon, min_lat, max_lon, max_lat. A box is generous at the edges (it takes in
# slices of Pakistan, Nepal, Bangladesh, Sri Lanka...), so it narrows the search
# and the country-code check below makes the actual decision.
INDIA_BBOX = (68.1, 6.5, 97.4, 35.7)

# Photon results that are technically places but never what someone booking a
# pickup means: street furniture, utilities, and the individual stop poles and
# platforms that otherwise crowd out the station they belong to.
_PHOTON_NOISE: dict[str, set[str] | None] = {
    "man_made": None,
    "power": None,
    "waterway": None,
    "barrier": None,
    "highway": {"bus_stop", "platform", "street_lamp", "traffic_signals", "crossing"},
    "public_transport": {"platform", "stop_position"},
    "railway": {"platform", "stop", "subway_entrance", "switch", "level_crossing"},
    "amenity": {"bench", "waste_basket", "toilets", "drinking_water", "parking_entrance"},
}


def _allowed_country(code: str | None) -> bool:
    """Whether a result's country is one this deployment serves.

    Empty GEOCODER_COUNTRY_CODES means everywhere. A result that carries no
    country at all is refused when a restriction is set: an unknown country is
    not evidence of being in India, and PIN codes, phone numbers and tariffs all
    assume it.
    """
    allowed = {
        part.strip().lower()
        for part in settings.geocoder_country_codes.split(",")
        if part.strip()
    }
    if not allowed:
        return True
    return bool(code) and code.lower() in allowed


def _compose_label(*parts: str | None) -> str:
    """Join address parts into one readable line, dropping blanks and repeats.

    Providers happily report the same name at several levels — "Andheri West,
    Andheri West, Mumbai" — and Nominatim adds administrative units no person
    writes on a parcel ("Bengaluru Central City Corporation", "K/W Ward").
    Building the label from the structured fields instead of the provider's
    display string is what keeps a suggestion to one line.
    """
    seen: set[str] = set()
    kept: list[str] = []
    for part in parts:
        if not part:
            continue
        cleaned = part.strip()
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        kept.append(cleaned)
    return ", ".join(kept)


# Administrative suffixes OSM carries on Indian place names. "Jaipur Municipal
# Corporation" and "Sanganer Tehsil" are correct boundaries, but a person writes
# "Jaipur" and "Sanganer" — and the city line is what the driver reads.
_ADMIN_SUFFIX = re.compile(
    r"\s+(?:(?:Central|North|South|East|West|Greater)\s+)?"
    r"(?:Municipal Corporation|City Corporation|Municipal Council|Nagar Nigam"
    r"|Nagar Palika|Cantonment Board|Metropolitan City|Tehsil|Taluka|Taluk|taluku"
    r"|Subdistrict|Sub-District|Mandal|Block)$",
    re.IGNORECASE,
)


def _clean_admin(name: str | None) -> str | None:
    if not name:
        return name
    cleaned = _ADMIN_SUFFIX.sub("", name.strip()).strip()
    return cleaned or name.strip()


def _first(address: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = address.get(key)
        if isinstance(value, str) and value.strip():
            return _clean_admin(value.strip())
    return None


def _line1(raw: dict, address: dict) -> str:
    """The street line: house number + road, or the name of the place itself."""
    road = address.get("road")
    house_number = address.get("house_number")
    name = raw.get("name")

    # A named place (a mall, a café, a station) is what someone searched for
    # and what a driver looks for on arrival; the street it is on is detail.
    # Roads and boundaries are excluded — their "name" is the road or the
    # administrative unit itself.
    if name and raw.get("category") not in {"highway", "boundary", "place"} and name != road:
        return str(name)

    if road:
        return f"{house_number}, {road}" if house_number else str(road)

    named = raw.get("name") or _first(address, _NAME_KEYS)
    if named and raw.get("category") != "boundary":
        return str(named)

    # No road and no named place: an unnamed lane, which is most residential
    # streets in Indian OSM data. The display name's first chunk is then an
    # administrative unit ("Jaipur Municipal Corporation"), which no driver can
    # navigate to, so prefer the neighbourhood. `reverse()` replaces this with
    # the nearest named landmark when it can find one.
    area = _first(address, _AREA_KEYS)
    return area or "Unnamed road"


def _is_vague(raw: dict) -> bool:
    """A reverse result that names neither a road nor a place — only an area."""
    address = raw.get("address") or {}
    if address.get("road"):
        return False
    if raw.get("category") == "boundary":
        return True
    return not (raw.get("name") or _first(address, _NAME_KEYS))


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
    if not _allowed_country(address.get("country_code")):
        return None

    line1 = _line1(raw, address)
    area = _first(address, _AREA_KEYS)
    city = _first(address, _CITY_KEYS) or ""
    pin_code = _pin_code(address)
    # Never repeat line 1 on line 2 — Nominatim will happily report the suburb
    # as both when the road *is* the suburb's main road.
    line2 = area if area and area.casefold() != line1.casefold() else None

    return Place(
        latitude=latitude,
        longitude=longitude,
        label=_compose_label(line1, line2, city, address.get("state"), pin_code)
        or line1,
        address_line1=line1,
        address_line2=line2,
        city=city,
        pin_code=pin_code,
        place_id=str(raw.get("place_id") or f"{latitude},{longitude}"),
    )


def _photon_is_noise(properties: dict) -> bool:
    key = properties.get("osm_key")
    if key not in _PHOTON_NOISE:
        return False
    values = _PHOTON_NOISE[key]
    return values is None or properties.get("osm_value") in values


def _photon_to_place(feature: dict) -> Place | None:
    """Map one Photon GeoJSON feature. None for anything outside the served
    countries, without a usable position, or in the noise list above."""
    properties = feature.get("properties") or {}
    if not _allowed_country(properties.get("countrycode")):
        return None
    if _photon_is_noise(properties):
        return None

    try:
        longitude, latitude = (float(value) for value in feature["geometry"]["coordinates"][:2])
    except (KeyError, TypeError, ValueError):
        return None

    name = properties.get("name")
    street = properties.get("street")
    house_number = properties.get("housenumber")

    if house_number and street:
        line1 = f"{house_number}, {street}"
    elif name:
        line1 = str(name)
    elif street:
        line1 = str(street)
    else:
        return None

    # Photon's `city` is often missing for Indian suburbs, which it files
    # straight under the state — so fall back through the wider units rather
    # than leave the driver without a town.
    city = _clean_admin(
        properties.get("city")
        or properties.get("county")
        or properties.get("district")
        or properties.get("state")
        or ""
    ) or ""
    area_candidates = (
        _clean_admin(properties.get("locality")),
        _clean_admin(properties.get("district")),
        street if name and street else None,
    )
    line2 = next(
        (
            str(part)
            for part in area_candidates
            if part and str(part).casefold() not in {line1.casefold(), str(city).casefold()}
        ),
        None,
    )
    pin_code = _pin_code({"postcode": properties.get("postcode")})

    return Place(
        latitude=latitude,
        longitude=longitude,
        label=_compose_label(line1, line2, city, properties.get("state"), pin_code),
        address_line1=line1,
        address_line2=line2,
        city=str(city),
        pin_code=pin_code,
        place_id=f"{properties.get('osm_type', '')}{properties.get('osm_id', '')}"
        or f"{latitude},{longitude}",
    )


def _dedupe(places: list[Place], limit: int) -> list[Place]:
    """Drop repeats, keeping the first (best-ranked) of each.

    Two kinds show up constantly: the same label from two OSM objects (a
    station's node and its area), and the same spot under two names (a metro
    stop and its entrance). Matching on label, or on line 1 within ~100 m,
    catches both without merging genuinely different places that share a name
    across town.
    """
    kept: list[Place] = []
    labels: set[str] = set()
    spots: set[tuple[str, float, float]] = set()
    for place in places:
        label_key = place.label.casefold()
        spot_key = (
            place.address_line1.casefold(),
            round(place.latitude, 3),
            round(place.longitude, 3),
        )
        if label_key in labels or spot_key in spots:
            continue
        labels.add(label_key)
        spots.add(spot_key)
        kept.append(place)
        if len(kept) == limit:
            break
    return kept


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


# Photon gets its own client and throttle: it is a different host with a
# different policy, and sharing Nominatim's 1 s floor would make every keystroke
# queue behind every other user's.
_photon_client: httpx.AsyncClient | None = None
_photon_throttle_lock = asyncio.Lock()
_photon_last_request_at = 0.0


async def _get_photon_client() -> httpx.AsyncClient:
    global _photon_client

    if _photon_client is None:
        async with _client_lock:
            if _photon_client is None:
                _photon_client = httpx.AsyncClient(
                    base_url=settings.geocoder_autocomplete_url.rstrip("/"),
                    timeout=settings.geocoder_timeout_seconds,
                    headers=_headers(),
                    follow_redirects=True,
                )

    return _photon_client


async def close_geocoder() -> None:
    """Release the HTTP clients. Called from the service's lifespan shutdown."""
    global _client, _photon_client

    if _client is not None:
        await _client.aclose()
        _client = None
    if _photon_client is not None:
        await _photon_client.aclose()
        _photon_client = None


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


async def _photon_search(
    query: str,
    limit: int,
    near: tuple[float, float] | None,
) -> list[Place]:
    """Type-ahead search on Photon, restricted to the served countries."""
    global _photon_last_request_at

    client = await _get_photon_client()

    params: list[tuple[str, str | int | float]] = [
        ("q", query),
        # A few more than needed, for the filters to drop — but only a few.
        # Photon re-ranks a larger candidate set differently: asked for 18,
        # "MG Road" near Bengaluru led with roads in Kerala and Tamil Nadu;
        # asked for 6 it led with Bengaluru's.
        ("limit", limit + 3),
        ("lang", "en"),
        # Excluded at the source as well as by _PHOTON_NOISE: a busy road has a
        # bus stop named after it every few hundred metres, and left in they
        # fill the result window and push the road itself out of it.
        ("osm_tag", "!highway:bus_stop"),
        ("osm_tag", "!public_transport:platform"),
        ("osm_tag", "!public_transport:stop_position"),
    ]
    if settings.geocoder_country_codes.strip().lower() == "in":
        min_lon, min_lat, max_lon, max_lat = INDIA_BBOX
        params.append(("bbox", f"{min_lon},{min_lat},{max_lon},{max_lat}"))
    if near is not None:
        # City-scale bias: "MG Road" near Gurugram ranks Gurugram's first, but
        # Bengaluru's is still offered if that is what is typed in full.
        params += [("lat", near[0]), ("lon", near[1]), ("zoom", 12), ("location_bias_scale", 0.2)]

    async with _photon_throttle_lock:
        wait = settings.geocoder_autocomplete_min_interval_seconds - (
            time.monotonic() - _photon_last_request_at
        )
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            response = await client.get("/api/", params=params)
        except httpx.HTTPError as error:
            logger.warning("Autocomplete request failed: %s", error)
            raise GeocoderUnavailableError() from error
        finally:
            _photon_last_request_at = time.monotonic()

    if response.status_code >= 400:
        logger.warning("Autocomplete answered %s", response.status_code)
        raise GeocoderUnavailableError()

    try:
        features = response.json().get("features") or []
    except (ValueError, AttributeError) as error:
        raise GeocoderUnavailableError() from error

    places = [place for place in (_photon_to_place(f) for f in features) if place]
    return _dedupe(places, limit)


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

    Photon answers first because it understands half-typed words. Nominatim is
    the fallback, both when Photon is unreachable and when it finds nothing:
    Nominatim is stricter but better at full street addresses with house
    numbers, which is exactly the query Photon is most likely to miss.
    """
    cleaned = query.strip()
    if len(cleaned) < 3:
        # Below three characters every provider returns noise. Answering with
        # nothing costs one request less and reads identically to the user.
        return []

    # Round the bias so a pin nudged by a few metres still hits the cache.
    near_key = (round(near[0], 2), round(near[1], 2)) if near else None
    cache_key = f"s:{cleaned.casefold()}:{limit}:{near_key}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    results: list[Place] = []
    if settings.geocoder_autocomplete_url:
        try:
            results = await _photon_search(cleaned, limit, near)
        except GeocoderUnavailableError:
            results = []

    if not results:
        results = await _nominatim_search(cleaned, limit, near)

    results = _prefer_named_area(cleaned, results)
    _cache.put(cache_key, results)
    return results


def _prefer_named_area(query: str, places: list[Place]) -> list[Place]:
    """Put results in the area the user typed after a comma first.

    "Celebal Technologies, jaipur" is a request for Jaipur, but providers rank
    an exact name match anywhere in the country above a partial one in the
    named city. A stable sort keeps each group's own ranking.
    """
    if "," not in query:
        return places
    area = query.rsplit(",", 1)[1].strip().casefold()
    if len(area) < 3:
        return places
    return sorted(places, key=lambda place: area not in place.label.casefold())


async def _nominatim_search(
    cleaned: str,
    limit: int,
    near: tuple[float, float] | None,
) -> list[Place]:
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
    places = [place for place in (_to_place(raw) for raw in raw_results) if place]
    return _dedupe(places, limit)


@dataclass(frozen=True)
class ReverseResult:
    """What is at a point, and whether this deployment delivers there.

    The two are separate because "no address" is not "not served": an unmapped
    field inside India has no place but is a valid pin, while Kathmandu has a
    perfectly good place and is outside the service area. Only the provider's
    country code can tell them apart — a bounding box around India also
    contains Nepal, Bhutan and Bangladesh.
    """

    place: Place | None
    in_service_area: bool


async def reverse(latitude: float, longitude: float) -> ReverseResult:
    """The address at a point, with whether the point is somewhere served.

    `place=None` is a normal answer — a pin dropped at sea or on unmapped land
    has no address — and the caller shows the coordinates alone rather than an
    error. The pin is still valid; a driver can be sent to it.
    """
    # Six decimals is the stored precision, so rounding the key here means a pin
    # nudged by a pixel reuses the cached answer instead of spending a request
    # on a difference nothing can represent.
    cache_key = f"r:{latitude:.6f},{longitude:.6f}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached[0]  # type: ignore[return-value]

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
    # No result at all (open water, unmapped land) is given the benefit of the
    # doubt; only a result that names another country is refused.
    in_service_area = (
        _allowed_country((results[0].get("address") or {}).get("country_code"))
        if results
        else True
    )

    # An unnamed lane is where most people actually live, and Nominatim can only
    # describe it by its neighbourhood. Name it after the nearest landmark
    # instead — "Near Cube Lotus, Muhana Mandi Road" is something a driver can
    # find. The pin itself does not move: the coordinates stay the requested
    # ones, only the words change.
    if place is not None and results and _is_vague(results[0]):
        landmark = await _photon_nearest(latitude, longitude)
        if landmark is not None:
            # Always "Near": even a numbered house is a neighbour's, and
            # presenting "57, Amrit Nagar" as this address would send a driver
            # to the wrong door.
            line1 = f"Near {landmark.address_line1}"
            line2 = landmark.address_line2 or place.address_line2
            pin_code = place.pin_code or landmark.pin_code
            city = place.city or landmark.city
            place = Place(
                latitude=place.latitude,
                longitude=place.longitude,
                label=_compose_label(line1, line2, city, pin_code),
                address_line1=line1,
                address_line2=line2,
                city=city,
                pin_code=pin_code,
                place_id=place.place_id,
            )

    result = ReverseResult(place=place, in_service_area=in_service_area)
    _cache.put(cache_key, [result])  # type: ignore[list-item]
    return result


async def _photon_nearest(latitude: float, longitude: float) -> Place | None:
    """The closest named place or street within ~300 m, from Photon's reverse.

    Best effort: any failure answers None and the caller keeps Nominatim's
    neighbourhood-level answer, which is vaguer but still correct.
    """
    global _photon_last_request_at

    if not settings.geocoder_autocomplete_url:
        return None

    client = await _get_photon_client()
    params = {
        "lat": latitude,
        "lon": longitude,
        "limit": 6,
        "radius": 0.3,
        "lang": "en",
    }

    async with _photon_throttle_lock:
        wait = settings.geocoder_autocomplete_min_interval_seconds - (
            time.monotonic() - _photon_last_request_at
        )
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            response = await client.get("/reverse", params=params)
        except httpx.HTTPError as error:
            logger.warning("Landmark lookup failed: %s", error)
            return None
        finally:
            _photon_last_request_at = time.monotonic()

    if response.status_code >= 400:
        return None
    try:
        features = response.json().get("features") or []
    except (ValueError, AttributeError):
        return None

    for feature in features:
        candidate = _photon_to_place(feature)
        # Parks and similar areas are fine landmarks; a nameless house is not.
        if candidate is not None and candidate.address_line1:
            return candidate
    return None
