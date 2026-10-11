# Pricing Service

What a delivery costs. Port **8006**, database **pricing_db**.

It replaced the stub that lived inside orderServices (`domain/pricing.py` and an
in-process `clients/pricing_client.py`). The stub was shaped for this: Order's
`pricing_client.get_quote()` kept its signature and its `Quote`, and now makes
an HTTP call instead of a calculation. Nothing above that file changed.

Companions: [ORDER_SERVICE.md](ORDER_SERVICE.md) for the caller,
[PLATFORM_WORKFLOW.md](PLATFORM_WORKFLOW.md) for where pricing sits in a delivery.

---

## Two ways to ask, and why they are different

| | Estimate | Quote |
| --- | --- | --- |
| Endpoint | `POST /api/pricing/estimate` | `POST /internal/pricing/quotes` |
| Who | Anyone — public, no session | Order only — internal key, not routed by the gateway |
| Answers | Every vehicle, with availability | One vehicle |
| Stored | No | Yes, with its breakdown |
| Binding | No | Yes, until `expires_at` |

Both call the same pure function, `domain/fare.py:compute_fare`, so the price a
visitor sees on the home page and the price they are charged cannot be computed
two different ways.

**Why the estimate is public.** The home page lets a visitor describe a delivery
and see the price before signing in — the same reasoning that makes `/api/geo`
public. It is safe in a way most public endpoints are not: it is arithmetic on
the caller's own coordinates plus a published rate card, writes nothing, and
calls no third party.

**Why the quote is not.** A customer who could mint quotes could price a short
trip and attach the quote to a long one.

---

## The fare

```
distance  = straight line (haversine) × ROAD_DISTANCE_FACTOR      default 1.3
fare      = base_fare
          + distance × per_km
          + max(0, weight − included_weight_kg) × per_kg
fare      = max(fare, minimum_fare)
```

Every line is rounded on its own, in `Decimal`, so the breakdown shown to the
customer adds up to the total shown to the customer.

**The road factor** exists because nothing here asks a routing engine which
roads exist. Charging the straight line would undercharge every trip by a
predictable amount. When a router is wired in, the factor becomes `1.0`.

**Capacity is refused here.** A parcel heavier than the vehicle's
`max_weight_kg` gets a 422 on a quote and an "unavailable" option on an
estimate. Left to Dispatch, it would surface as "no driver found" three minutes
after the customer paid.

---

## The rate card

`tariffs`, one row per vehicle type, **seeded by the first migration** — with no
rows every estimate is empty and every quote a 422, so it is reference data the
service cannot run without, not demo data.

| vehicle | base | per km | minimum | kg included | per extra kg | max kg |
| --- | --- | --- | --- | --- | --- | --- |
| two_wheeler | 30 | 10 | 45 | 10 | 2.00 | 30 |
| three_wheeler | 80 | 18 | 150 | 100 | 0.50 | 500 |
| mini_truck | 250 | 28 | 400 | 500 | 0.30 | 1500 |
| truck | 600 | 45 | 1000 | 2000 | 0.15 | 10000 |

The two-wheeler sits close to the stub it replaced: 10/km on the 1.3× road
distance is about 13 per straight-line km. Max weights match partnerServices'
`VEHICLE_MAX_CAPACITY_KG`.

Change it with `PATCH /internal/pricing/tariffs/{vehicle_type}` (partial).
`is_active: false` takes a vehicle off sale without deleting the row old quotes
point at. **Editing a tariff never changes an issued quote** — each quote stores
the amounts it was computed to.

---

## Endpoints

Public, through the gateway at `/api/pricing` (both in `PUBLIC_PATHS`):

| | |
| --- | --- |
| `POST /api/pricing/estimate` | `{pickup:{latitude,longitude}, drop:{…}, weight_kg?}` → every vehicle's price, cheapest first. `weight_kg` defaults to `ESTIMATE_DEFAULT_WEIGHT_KG` |
| `GET /api/pricing/tariffs` | The active rate card |

Internal, `/internal/pricing`, `X-Internal-Key`:

| | |
| --- | --- |
| `POST /internal/pricing/quotes` | `{pickup, drop, vehicle_type, weight_kg, customer_id?}` → `201` quote, or `422` with a sentence meant for the customer |
| `GET /internal/pricing/quotes/{id}` | One quote as issued — "why was I charged that?" |
| `GET /internal/pricing/tariffs` | Every tariff, including inactive |
| `PATCH /internal/pricing/tariffs/{vehicle_type}` | Change a rate card |

Money is a string in every response — Pydantic serialises `Decimal` that way,
and a float has lost the cents by the time a browser parses it.

---

## How Order uses it

```
POST /api/orders ─▶ Order ─▶ POST /internal/pricing/quotes ─▶ Pricing
                                  │
                ┌─────────────────┼────────────────────────┐
              201                422                 timeout / 5xx
   snapshot amount,       QuoteRefusedError        PricingServiceUnavailableError
   distance, expiry,      → 422 to the customer    → 503 "try again"
   and quote_id           in Pricing's words
```

The two failures are kept apart on purpose: one is the customer's to fix by
choosing again, the other is ours. Order stores `quote_id` (revision
`a8d4e61f2c90`) beside the amount it already snapshotted — the amount is what
was agreed, the id is where to look up how.

---

## Running it

```powershell
# once — ask before running; these write to Postgres
CREATE DATABASE pricing_db;
alembic -c alembic_pricing.ini upgrade head      # -> f7a2c9d1b3e5 (head)
alembic -c alembic_order.ini upgrade head        # -> a8d4e61f2c90 (adds orders.quote_id)

uvicorn src.services.pricingServices.main:app --port 8006 --reload
```

No Mongo and no Kafka: nothing here is authenticated by session, and Order needs
the answer before it can reply to the customer, so this service is synchronous
by nature.

| Setting | Default | |
| --- | --- | --- |
| `PRICING_DB_NAME` | `pricing_db` | |
| `ROAD_DISTANCE_FACTOR` | `1.3` | Detour allowance on the straight line |
| `QUOTE_TTL_MINUTES` | `15` | Moved here from orderServices |
| `ESTIMATE_DEFAULT_WEIGHT_KG` | `1` | Light enough never to trip a surcharge |
| `PRICING_SERVICE_URL` | `http://127.0.0.1:8006` | Read by the gateway and by Order |
| `PRICING_SERVICE_TIMEOUT_SECONDS` | `2` | Order's budget for a quote |

## Not built

- `pricing.quoted` on Kafka. Order gets the quote synchronously, so nothing
  needs the event yet; Audit will.
- Surge, city tariffs, night rates, coupons — each is a column or a rates
  multiplier in `compute_fare`, not a redesign.
- A per-IP quota on the public estimate. It is cheap, but it is a public POST.
- Re-quoting an expired quote on confirm. `quote_expires_at` is stored on the
  order; `confirm` does not check it yet.
