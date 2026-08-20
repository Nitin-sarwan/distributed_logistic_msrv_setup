# Geocoding

`/api/geo` — address text to coordinates, and coordinates back to address text.

Every map in the frontend rests on these two endpoints. Nothing else in the
system does: coordinates are stored by `userServices` (the `address` table) and
by `partnerServices` (a partner's live position), and neither of them needs a
geocoder to do it. This is the layer that lets a *person* choose those numbers
without typing them.

---

## Where it lives, and why that is deliberate

```
src/services/userServices/
  api/geo_routes.py        # the two endpoints, and the per-IP quota
  utils/geocoder.py        # the provider client, the throttle, the cache
  api/schema.py            # PlaceResponse, ReverseGeocodeResponse
```

It is served by the `userServices` process but registered in the gateway under
its own prefix:

```python
SERVICE_ROUTES = {
    "/api/users": settings.user_service_url,
    "/api/partners": settings.partner_service_url,
    "/api/geo": settings.geo_service_url,     # same host today
}
```

Two facts follow from that split, and both are the point:

* It is **not** under `/api/users`. Nothing here reads or writes a row, and no
  answer depends on who is asking — a query about a place is not a query about a
  person. Nesting it under users would also have made both endpoints look like
  they need a session, which they do not.
* Standing it up as its own process is `GEO_SERVICE_URL` in `.env` and nothing
  else. No caller changes, because no caller ever knew.

---

## Endpoints

Both are **public** — see [Why public](#why-public).

### `GET /api/geo/search`

| Query | Required | Meaning |
| --- | --- | --- |
| `q` | yes | Free text: a road, a landmark, a building |
| `limit` | no | 1–10, default `GEOCODER_SEARCH_LIMIT` (5) |
| `lat`, `lng` | no | Bias results near this point. Ranking only — nothing is excluded |

```jsonc
[
  { "latitude": 12.9732913, "longitude": 77.6404672,
    "label": "Indiranagar, Bengaluru, Karnataka, 560008, India",
    "address_line1": "Indiranagar", "address_line2": null,
    "city": "Bengaluru", "pin_code": "560008",
    "place_id": "251866849" }
]
```

`[]` means "no match" — an answer, not an error. Queries under three characters
return `[]` without touching the provider, since every geocoder returns noise
below that.

### `GET /api/geo/reverse`

| Query | Required | Meaning |
| --- | --- | --- |
| `lat`, `lng` | yes | The point to look up |

```jsonc
{ "latitude": 12.9352, "longitude": 77.6245,
  "place": { "address_line1": "41, Mahayogi Vemana Road", "…": "…" } }
```

**`place` can be `null`, and that is a `200`.** A pin in a field, on a new road,
or in the middle of a lake has no address. The coordinates are the half that
matters — a driver can be sent to them — so a null address is missing text, not
a failed request.

### Status codes

| Status | When |
| --- | --- |
| `200` | Answered, possibly with `[]` or a null `place` |
| `422` | `lat`/`lng` out of range, `q` missing |
| `429` | Per-IP quota exhausted. `Retry-After: 60` |
| `503` | The provider is unreachable or answered with something unusable |

`503` rather than `500` is load-bearing: nothing here is broken, an upstream is.
The frontend reads it as "keep the map, disable the search box".

---

## The response shape

`PlaceResponse` field names match `AddressCreate`'s on purpose, so the frontend
can fill the address form straight from a result. Two fields need explaining:

* **`label` vs `address_line1`.** `label` is the full sentence for a suggestion
  list; `address_line1` is the short street line for a form field. Collapsing
  them would either put "…, Karnataka, 560008, India" into a database column or
  strip a suggestion list of the context that tells two "MG Road"s apart.
* **`pin_code` is null unless it is six digits.** The `address` table's column is
  `VARCHAR(6)` and is validated as six digits on the way in. A UK or US postcode
  therefore comes back as null rather than as a value the form would submit and
  the API would then reject.

---

## Why public

Both endpoints are in the gateway's `PUBLIC_PATHS`. The home page lets a visitor
describe a delivery before signing in — that is the whole design of the booking
widget — and a search box that demanded an account first would undo it.

What that costs is real: the gateway now relays an anonymous request to a third
party, which is an open relay unless it is bounded. Three things bound it, and
they are layered on purpose because each covers what the others cannot:

| Control | Where | Stops |
| --- | --- | --- |
| Per-IP quota, 30/min | `geo_routes.py` | One caller monopolising the endpoint |
| 1 req/s process-wide throttle | `geocoder.py` | Exceeding the provider's policy, however many callers |
| 24h cache | `geocoder.py` | The same lookup ever costing twice |

The quota is per process and in memory. Two workers give a caller two quotas, a
restart forgives everyone, and `X-Forwarded-For` is client-supplied and so
forgeable. It is a brake on casual abuse and **not** a defence against a
determined one — that belongs at the edge, in a WAF or in nginx. The throttle
and the cache are what actually protect the provider, and neither depends on the
caller being honest.

---

## The provider

OpenStreetMap's [Nominatim](https://nominatim.org/): no key, no account, free.

Its [usage policy](https://operations.osmfoundation.org/policies/nominatim/) is
the thing to respect, and two settings exist for it:

```bash
GEOCODER_USER_AGENT=distributed-logistic/0.1 (ops@example.com)
GEOCODER_CONTACT_EMAIL=ops@example.com
```

Traffic without an identifying User-Agent is blocked, and a block takes address
search down for every user at once. Set both before running anywhere real.

**Swapping provider is one file.** Everything above `utils/geocoder.py` sees the
`Place` dataclass, never a provider's JSON. Google, Mapbox, Photon, or a
self-hosted Nominatim are a change to `_to_place()` and the two request
functions; no route, no schema, and no frontend code moves.

### Settings

| Setting | Default | Notes |
| --- | --- | --- |
| `GEOCODER_BASE_URL` | `https://nominatim.openstreetmap.org` | |
| `GEOCODER_USER_AGENT` | `distributed-logistic/0.1 (local development)` | Required by policy |
| `GEOCODER_CONTACT_EMAIL` | *(empty)* | Sent as `From`. Omitted when empty |
| `GEOCODER_COUNTRY_CODES` | `in` | Comma separated. Empty searches worldwide |
| `GEOCODER_TIMEOUT_SECONDS` | `8.0` | |
| `GEOCODER_MIN_INTERVAL_SECONDS` | `1.0` | The provider's published limit |
| `GEOCODER_CACHE_TTL_SECONDS` | `86400` | Addresses do not move |
| `GEOCODER_CACHE_MAX_ENTRIES` | `2000` | Oldest-inserted evicted first |
| `GEOCODER_SEARCH_LIMIT` | `5` | Default `limit` |
| `GEO_RATE_LIMIT_PER_MINUTE` | `30` | Per client IP |

---

## The two caches, and what they are not

The TTL cache is in-process and per-worker: two uvicorn workers keep two caches
and the hit rate halves. That is acceptable at this size because the cache exists
to stay inside the provider's rate limit and to make a type-ahead feel instant —
not to be a system of record. Redis is the answer if it ever needs to be shared,
and `_TtlCache` is one class to swap.

Reverse-lookup keys are rounded to six decimal places, matching what the
`address` table stores. A pin nudged by a pixel therefore reuses the cached
answer instead of spending a request on a difference no column can represent.

---

## Coordinates elsewhere in the system

This service does not own coordinates; it only helps choose them. Where they
actually live:

| Table | Columns | Owner |
| --- | --- | --- |
| `address` | `latitude`, `longitude` `NUMERIC(9,6)`, both `NOT NULL` | userServices |
| `partners` | `current_latitude`, `current_longitude`, `location_updated_at`, all nullable | partnerServices |

`NUMERIC(9,6)` is about 10cm, which is the precision a driver's pin needs.
Values are quantised to six decimal places in the Pydantic layer of both
services rather than left to Postgres, so the value that is validated is the
value that is stored — and so a longitude with nine decimals cannot overflow the
column's nine total digits on insert.

The one place coordinates are *computed* with is
`PartnerRepository.find_available()`, which ranks partners by Haversine distance
behind a bounding box. See [PARTNER_SERVICE.md](PARTNER_SERVICE.md); nothing in
this document is involved in that query.
