"""Geocoding — the map's half of the address story.

Mounted at `/api/geo`, outside `/users`, because it is about places rather than
about people: nothing here reads or writes a row, and no answer depends on who
is asking. It lives inside userServices only because that is the process the
gateway already routes to; moving it to its own service later is one line in the
gateway's SERVICE_ROUTES and no change to any caller.

**These two endpoints are public.** That is a deliberate decision with a cost.
The home page lets someone describe a delivery before signing in — that is the
whole point of the booking widget — and a search box that demands an account
before it will find "Indiranagar" would undo it. What public costs is that the
gateway now relays an unauthenticated request to a third party, which is an open
relay unless it is bounded. Hence:

* a per-IP quota, below, refusing the caller before the provider is touched;
* a process-wide 1 req/s throttle and a 24h cache in `utils/geocoder.py`, so
  even a stampede from many IPs cannot exceed the provider's policy;
* no request body, no state, and nothing written anywhere.

The quota is per process and in memory. Two workers give a caller two quotas,
and a restart forgives everyone. It is a brake on casual abuse, not a defence
against a determined one — that belongs at the edge (a WAF, or a rate limit in
nginx) and this is not a substitute for it.
"""

import logging
import time
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from src.services.userServices.api.schema import PlaceResponse, ReverseGeocodeResponse
from src.services.userServices.config import settings
from src.services.userServices.utils.geocoder import (
    GeocoderUnavailableError,
    Place,
    reverse,
    search,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/geo", tags=["geo"])

GEOCODER_UNAVAILABLE = HTTPException(
    # 503, not 500: nothing here is broken, an upstream is. The distinction is
    # what tells the frontend to keep the map usable and only disable search.
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    detail="Address lookup is unavailable right now. Place the pin on the map instead.",
)


class _IpRateLimiter:
    """A sliding-window request count per client.

    A window rather than a fixed bucket, because a fixed one lets a caller spend
    a full quota at 11:59.9 and another at 12:00.0 — twice the intended rate at
    exactly the moment a limiter is supposed to hold. Deques of timestamps cost
    a few hundred bytes per active caller and are exact.
    """

    # How many calls between sweeps of the whole table. Without one, the dict
    # keeps an entry for every address that has ever called, for the lifetime of
    # the process — a slow leak on a long-running server.
    _SWEEP_EVERY = 512

    def __init__(self, limit: int, window_seconds: float = 60.0):
        self._limit = limit
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._calls_since_sweep = 0

    def _sweep(self, now: float) -> None:
        stale = [
            client
            for client, hits in self._hits.items()
            if not hits or now - hits[-1] > self._window
        ]
        for client in stale:
            self._hits.pop(client, None)

    def allow(self, client: str) -> bool:
        now = time.monotonic()

        self._calls_since_sweep += 1
        if self._calls_since_sweep >= self._SWEEP_EVERY:
            self._calls_since_sweep = 0
            self._sweep(now)

        hits = self._hits.setdefault(client, deque())

        # Drop everything that has aged out of the window.
        while hits and now - hits[0] > self._window:
            hits.popleft()

        if len(hits) >= self._limit:
            return False

        hits.append(now)
        return True


_limiter = _IpRateLimiter(settings.geo_rate_limit_per_minute)


def _client_ip(request: Request) -> str:
    """The caller's address, as seen through the gateway.

    Every request here arrives from the gateway, so `request.client.host` is the
    gateway on every one of them — limiting on it would give the entire internet
    a single shared quota. The gateway appends the real address to
    X-Forwarded-For, and the *first* entry is the original client.

    This header is client-supplied and therefore forgeable, which matters: a
    caller who wants around this quota can send a new X-Forwarded-For per
    request. That is accepted here, because the throttle and cache below are
    what actually protect the provider, and this is only fairness between honest
    callers.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first

    return request.client.host if request.client else "unknown"


def enforce_rate_limit(request: Request) -> None:
    if not _limiter.allow(_client_ip(request)):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many address lookups. Wait a moment and try again.",
            # Tells a well-behaved client how long to back off, instead of
            # leaving it to guess and retry immediately.
            headers={"Retry-After": "60"},
        )


def _to_response(place: Place) -> PlaceResponse:
    return PlaceResponse(
        latitude=place.latitude,
        longitude=place.longitude,
        label=place.label,
        address_line1=place.address_line1,
        address_line2=place.address_line2,
        city=place.city,
        pin_code=place.pin_code,
        place_id=place.place_id,
    )


@router.get(
    "/search",
    response_model=list[PlaceResponse],
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(enforce_rate_limit)],
)
async def search_places(
    q: str = Query(min_length=1, max_length=200, description="Free-text address"),
    limit: int | None = Query(default=None, ge=1, le=10),
    lat: float | None = Query(
        default=None, ge=-90, le=90, description="Bias results near this point"
    ),
    lng: float | None = Query(default=None, ge=-180, le=180),
):
    """Free text in, candidate places out — the type-ahead behind every address
    field in the app.

    An empty list is a valid answer, not an error: it means "no match", and the
    user's next move is to refine the text or drop the pin themselves.

    `lat`/`lng` are optional and biasing only. They are supplied as a pair or
    not at all; one without the other is ignored rather than rejected, since a
    half-supplied bias is not worth failing a search over.
    """
    near = (lat, lng) if lat is not None and lng is not None else None

    try:
        places = await search(
            query=q,
            limit=limit or settings.geocoder_search_limit,
            near=near,
        )
    except GeocoderUnavailableError as error:
        raise GEOCODER_UNAVAILABLE from error

    return [_to_response(place) for place in places]


@router.get(
    "/reverse",
    response_model=ReverseGeocodeResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(enforce_rate_limit)],
)
async def reverse_geocode(
    lat: float = Query(ge=-90, le=90),
    lng: float = Query(ge=-180, le=180),
):
    """The address at a point — what fills the form when the pin is dragged.

    `place: null` is a 200, not a 404. The coordinates are the answer that
    matters and they are always valid; the address is a convenience the provider
    may not have for a field, a new road, or the middle of a lake.
    """
    try:
        place = await reverse(latitude=lat, longitude=lng)
    except GeocoderUnavailableError as error:
        raise GEOCODER_UNAVAILABLE from error

    return ReverseGeocodeResponse(
        latitude=lat,
        longitude=lng,
        place=_to_response(place) if place else None,
    )
