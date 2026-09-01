# Order Service

The spine of the platform. Every other service exists to answer a question this
one asks, or to react to something this one announces.

This document is the implementation plan: what it owns, what it may never own,
the exact shape of its conversation with the User service, and the rule that
decides — for each of those conversations — whether it is an HTTP call or an
event.

Read [ARCHITECTURE.md](ARCHITECTURE.md) first for how services are wired today.

---

## Contents

- [The one question: HTTP or event?](#the-one-question-http-or-event)
- [Order ↔ User, in full](#order--user-in-full)
- [Trust and identity](#trust-and-identity)
- [Data model](#data-model)
- [The state machine](#the-state-machine)
- [Events it emits](#events-it-emits)
- [Events it consumes](#events-it-consumes)
- [The outbox](#the-outbox)
- [Endpoints](#endpoints)
- [Service layout](#service-layout)
- [Build order](#build-order)
- [Open decisions](#open-decisions)

---

## What it owns

| Owns exclusively | Notes |
| --- | --- |
| `orders` | The order itself, including **snapshots** of the addresses, the price, and the partner |
| `order_status_history` | Every transition, what caused it, and when |
| `order_items` | Deferred — see the note below |
| `outbox` | Events written in the same transaction as the state change that produced them |
| `processed_events` | Consumed `event_id`s, for deduplication |

**What it must never own:** a foreign key into another service's tables, a copy
of the user's address book, a partner's live position, a payment provider's
token. If Order needs any of those it either snapshots the value at the moment
it matters or asks the owner.

> **`order_items` is deferred on purpose.** A delivery today is one package with
> a weight and a description, which is three columns on `orders`. Build the table
> when a real multi-parcel order appears — an empty table with a one-row-per-order
> invariant is worse than the columns it replaced. See "Don't build empty layers"
> in the blueprint.

---

## The one question: HTTP or event?

One rule decides every case:

> **Call over HTTP when Order cannot produce a correct record without the answer
> right now. Emit an event when Order is announcing something it already knows.**

The tell is the direction of need. "What is this address?" is a question — Order
cannot write a valid order without it, and the answer must be the authoritative
one at that instant. "This order was confirmed" is a statement — Order already
knows it, it is complete without anyone's reply, and how many services care is
none of Order's business.

Applied to every conversation Order has:

| Direction | Mechanism | Why |
| --- | --- | --- |
| Order → **User**: resolve address + contact | **HTTP**, once, at creation | The snapshot has to be right at that instant, and there is no order without it |
| Order → **Pricing**: get a quote | **HTTP**, once, at creation | The customer is waiting to see a fare; nothing can proceed without the number |
| Order → **Payment**: create an intent | **HTTP**, once | The client needs the intent reference to complete payment |
| Order → **Dispatch**: find a partner | **Event** (`order.confirmed`) | Order does not care who does it, or whether it is one service or three |
| Order → Notification / Analytics / Audit | **Event** | Order must not know these exist. Adding a fourth consumer must not touch Order |
| **Payment** → Order: authorized, captured | **Event** | The outcome arrives from a provider webhook minutes later. Nothing can block on it |
| **Dispatch** → Order: partner assigned | **Event** | Order is being *told*, and dispatch may take seconds or minutes |
| **Pricing** → Order: quote issued | **Event** *and* HTTP response | Same fact, two audiences: the response is for this request, the event is for Audit and Analytics |

**Async is the default.** Every synchronous call couples two services' uptime:
if User is down, order creation fails. Three such calls is already a lot for one
endpoint, and each one below is justified individually. Everything after
creation is events, which is why an order can complete while half the platform
is restarting.

---

## Order ↔ User, in full

This is the connection the rest of the platform copies, so it is worth being
precise about.

### Exactly one call, at creation

```
Client                Gateway              Order                 User
  │                     │                    │                     │
  │ POST /api/orders    │                    │                     │
  │ {pickup:{address_id:12}, drop:{…}}       │                     │
  ├────────────────────>│                    │                     │
  │                     │ session check      │                     │
  │                     │ + X-User-Id: 7     │                     │
  │                     ├───────────────────>│                     │
  │                     │                    │ GET /internal/users/7/addresses/12
  │                     │                    │ X-Internal-Key: …   │
  │                     │                    ├────────────────────>│
  │                     │                    │  address + contact  │
  │                     │                    │<────────────────────┤
  │                     │                    │                     │
  │                     │                    │ ── copy into the order row ──
  │                     │                    │ ── commit + outbox row ──
  │                     │  201 {order}       │                     │
  │<─────────────────────────────────────────┤                     │
  │                     │                    │                     │
  │            ⋯ everything after this point is events ⋯
  │                     │                    │  0 further calls to User, ever
```

After that call the order row holds every fact it will ever need about the
customer. A partner opening a live delivery, a support agent reading a
six-month-old order, and an invoice all read the same snapshot — none of them
touch User.

### The endpoint User has to add

User does not expose one today. It needs an internal route, following the
pattern already established in `partnerServices/api/internal_routes.py`:

```
GET /internal/users/{user_id}/addresses/{address_id}
Header: X-Internal-Key: <INTERNAL_API_KEY>
```

```jsonc
// 200
{
  "address": {
    "id": 12,
    "address_line1": "25, Lavelle Road",
    "address_line2": "D'Souza Layout",
    "city": "Bengaluru",
    "pin_code": "560001",
    "latitude": 12.969515,
    "longitude": 77.597444
  },
  "contact": {
    "name": "Nitish Sarwan",
    "phone": "9853443879"
  }
}
```

Four things about this endpoint, each deliberate:

- **`/internal`, not `/api`.** The gateway routes `/api/*` and nothing else, so
  these paths do not exist as far as the public internet is concerned. Same
  reasoning as `/internal/partners/*`.
- **`user_id` is in the path and the query is scoped by it.** Order passes the
  id the gateway authenticated. User must still filter `WHERE user_id = ? AND
  id = ?`, so a wrong id returns 404 rather than someone else's address.
- **Contact comes back in the same response.** The driver needs a name and a
  number, and a second call for two columns would double the failure surface of
  the only synchronous dependency on this path.
- **404, not 403,** when the address belongs to someone else — consistent with
  the existing address routes, and it avoids confirming that an id exists.

### Three alternatives, and why each is wrong

| Alternative | Why not |
| --- | --- |
| **Client sends the full address** in the create request | The order is the record of what was agreed. A client-supplied lat/lng cannot be checked against anything, and support disputes turn on which address the customer actually picked. A saved-address id is a claim Order can verify; a blob of text is not |
| **Order stores `address_id`** and joins/reads later | Editing a saved address would silently rewrite history — the delivery that went to the old flat would start claiming it went to the new one. This is the snapshot rule in the blueprint, and it is the single most common way an audit trail starts lying |
| **Order consumes `user.address_*` events** into a local replica | Order would mirror a table it does not own, for data it needs once per order. Backfill, staleness, and holding every customer's address book — three new problems to avoid one HTTP call on a path that already makes two |

### The escape hatch, which removes the dependency entirely

A one-off delivery to somewhere not in the address book is a real case, and the
request supports it:

```jsonc
{ "pickup": { "address_id": 12 } }          // resolved via User
{ "pickup": { "address": { "…": "…" } } }   // taken as given, no call to User
```

Inline addresses need no User call — the customer typed them, they are the
agreement, and there is nothing to look up. **If both ends are inline, order
creation has no User dependency at all.** That is worth knowing on the day User
is down: the app can fall back to the map picker (which needs only `/api/geo`)
and orders keep flowing.

### When User is unavailable

The one place Order's uptime depends on someone else's. Policy:

| Setting | Value | Why |
| --- | --- | --- |
| Timeout | **2s** | The customer is on a spinner. A slow answer is a failed one |
| Retries | **1**, on connection error only | A retried 500 is usually a second 500. A retried timeout doubles the wait |
| Failure response | **503**, `"Could not confirm your delivery address. Try again."` | Not 500: nothing here is broken, an upstream is — same distinction `/api/geo` makes |
| Never | Fall back to a cached or client-supplied address | Silently ordering to the wrong flat is worse than failing |

---

## Trust and identity

> **Implemented differently from the blueprint, deliberately.** The blueprint
> says internal services trust `X-User-Id` from the gateway. This service reads
> the shared session store instead. The reasoning is worth recording, because it
> decides how every future service here does it.

The blueprint assumes a JWT the gateway verifies with a local key. This system
has no such token: an access token is AES-encrypted with a per-subject secret
held in the owning service's private database, which is why userServices and
partnerServices each decrypt their own, and why the gateway can only consult the
session store.

Order holds no such secret and can decrypt nothing. That leaves two options:

| Option | Cost |
| --- | --- |
| Trust `X-User-Id` | One forged header away from creating orders as anyone, for anything that can reach the service port |
| Read the shared session store, as the gateway does | One Mongo lookup per request |

The second is what `api/dependencies.py` does. The session store is shared
infrastructure — the same category as Kafka — so this depends on no service, and
in particular **it is not a call to User**. That rule still holds: nothing on the
request path asks User whether a token is valid, so User's uptime never gates
anyone else's.

It also fixes something the gateway does not currently check. Sessions live in
one collection with an `app_type` discriminator (`1` = customer, `2` = partner),
and the gateway forwards `session["user"]` without looking at it. Order checks it
itself:

```python
def get_current_customer_id(request: Request) -> int:
    return _subject_id(request, SESSION_COOKIE_NAME, CUSTOMER_APP_TYPE)

def get_current_partner_id(request: Request) -> int:
    return _subject_id(request, PARTNER_SESSION_COOKIE_NAME, PARTNER_APP_TYPE)
```

Without that check, a partner who copied their own token into the `lp_session`
cookie would create orders under "customer 7" who is actually partner 7. Adding
the same assertion at the gateway is still worth doing — defence at both ends —
but it is no longer the thing standing between this service and a forged
identity.

Registered in the gateway alongside the others:

```python
SERVICE_ROUTES = {..., "/api/orders": settings.order_service_url}
ROUTE_COOKIES  = {..., "/api/orders": SESSION_COOKIE_NAME}
# No entry in PUBLIC_PATHS: every order route requires a session.
```

---

## Data model

```sql
CREATE TABLE orders (
    id              BIGSERIAL PRIMARY KEY,
    public_ref      VARCHAR(12) NOT NULL UNIQUE,       -- "LP-8F3K2Q", shown to people
    customer_id     BIGINT      NOT NULL,              -- from X-User-Id. No FK: another service owns it
    status          VARCHAR(20) NOT NULL,

    -- Pickup snapshot ------------------------------------------------------
    pickup_line1        TEXT NOT NULL,
    pickup_line2        TEXT,
    pickup_city         VARCHAR(255) NOT NULL,
    pickup_pin_code     VARCHAR(6)   NOT NULL,
    pickup_latitude     NUMERIC(9,6) NOT NULL,
    pickup_longitude    NUMERIC(9,6) NOT NULL,
    pickup_contact_name  VARCHAR(100) NOT NULL,
    pickup_contact_phone VARCHAR(10)  NOT NULL,

    -- Drop snapshot (same nine columns) ------------------------------------
    drop_line1 TEXT NOT NULL, …

    -- Package --------------------------------------------------------------
    package_weight_kg   NUMERIC(8,2) NOT NULL,
    package_description TEXT,
    vehicle_type        VARCHAR(20)  NOT NULL,

    -- Price snapshot, from Pricing -----------------------------------------
    quote_id        UUID,
    quoted_amount   NUMERIC(10,2),
    currency        CHAR(3) NOT NULL DEFAULT 'INR',

    -- Payment reference, owned by Payment ----------------------------------
    payment_intent_id VARCHAR(64),

    -- Partner snapshot, from partner.assigned ------------------------------
    partner_id      BIGINT,
    partner_name    VARCHAR(100),
    partner_phone   VARCHAR(10),
    vehicle_number  VARCHAR(20),

    idempotency_key VARCHAR(64) NOT NULL,

    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at TIMESTAMPTZ,
    picked_up_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    cancellation_reason TEXT,

    CONSTRAINT uq_orders_idempotency UNIQUE (customer_id, idempotency_key),
    CONSTRAINT ck_orders_status CHECK (status IN (
        'created','quoted','payment_pending','confirmed','searching_partner',
        'partner_assigned','picked_up','in_transit','delivered','settled','cancelled'))
);

CREATE INDEX ix_orders_customer ON orders (customer_id, created_at DESC);
CREATE INDEX ix_orders_active   ON orders (status) WHERE status NOT IN ('delivered','settled','cancelled');
```

```sql
CREATE TABLE order_status_history (
    id         BIGSERIAL PRIMARY KEY,
    order_id   BIGINT NOT NULL REFERENCES orders(id),
    from_status VARCHAR(20),
    to_status   VARCHAR(20) NOT NULL,
    -- What caused it: the consumed event, or NULL for a customer action
    caused_by_event_id UUID,
    actor       VARCHAR(20) NOT NULL,   -- customer | dispatch | payment | system
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Every `*_name`, `*_phone`, `*_line1`, `quoted_amount` column is a snapshot,**
not a cache. Nothing refreshes them, and nothing should: they record what was
true when the parties agreed. `customer_id` and `partner_id` are the only
foreign references, and they are bare integers by design — a real FK would
require a shared database, which is the thing this architecture exists to avoid.

---

## The state machine

Pure logic, no SQLAlchemy, no FastAPI — `domain/states.py`:

```python
class OrderStatus(str, Enum):
    CREATED           = "created"
    QUOTED            = "quoted"
    PAYMENT_PENDING   = "payment_pending"
    CONFIRMED         = "confirmed"
    SEARCHING_PARTNER = "searching_partner"
    PARTNER_ASSIGNED  = "partner_assigned"
    PICKED_UP         = "picked_up"
    IN_TRANSIT        = "in_transit"
    DELIVERED         = "delivered"
    SETTLED           = "settled"
    CANCELLED         = "cancelled"


# The only legal moves. Anything absent here is a bug in a caller, not a
# state to add a branch for.
TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED:           frozenset({QUOTED, CANCELLED}),
    OrderStatus.QUOTED:            frozenset({PAYMENT_PENDING, CANCELLED}),
    OrderStatus.PAYMENT_PENDING:   frozenset({CONFIRMED, CANCELLED}),
    OrderStatus.CONFIRMED:         frozenset({SEARCHING_PARTNER, CANCELLED}),
    OrderStatus.SEARCHING_PARTNER: frozenset({PARTNER_ASSIGNED, CANCELLED}),
    OrderStatus.PARTNER_ASSIGNED:  frozenset({PICKED_UP, SEARCHING_PARTNER, CANCELLED}),
    OrderStatus.PICKED_UP:         frozenset({IN_TRANSIT}),
    OrderStatus.IN_TRANSIT:        frozenset({DELIVERED}),
    OrderStatus.DELIVERED:         frozenset({SETTLED}),
    OrderStatus.SETTLED:           frozenset(),
    OrderStatus.CANCELLED:         frozenset(),
}


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    return target in TRANSITIONS[current]
```

Three properties worth stating, because each one prevents a class of bug:

- **`PARTNER_ASSIGNED → SEARCHING_PARTNER` is legal.** A partner who accepts and
  then drops the job sends the order back to the pool. Without that edge the
  order is stranded and someone rescues it with SQL.
- **Nothing returns from `PICKED_UP`.** Cancellation ends at pickup: once goods
  are in a vehicle the resolution is a return trip, which is a new order, not a
  reversed status.
- **`DELIVERED` is not the end; `SETTLED` is.** Delivered means the goods
  arrived, settled means the money moved. Conflating them means either holding
  the customer's payment hostage to a webhook or calling an order complete
  before anyone has been paid.

---

## Events it emits

Every event, without exception, is written to the outbox in the **same
transaction** as the row it describes.

```jsonc
{
  "event_id":       "9f1c…",          // consumers deduplicate on this
  "event_type":     "order.confirmed",
  "event_version":  1,
  "occurred_at":    "2026-08-21T09:12:03.412Z",
  "correlation_id": "3b7a…",          // generated at the gateway, carried everywhere
  "producer":       "order",
  "data": {
    "order_id": 1042,
    "public_ref": "LP-8F3K2Q",
    "customer_id": 7,
    "pickup":  { "latitude": 12.97, "longitude": 77.59, "city": "Bengaluru" },
    "drop":    { "latitude": 12.93, "longitude": 77.62, "city": "Bengaluru" },
    "vehicle_type": "two_wheeler",
    "package_weight_kg": 4.5,
    "amount": 149.00,
    "currency": "INR"
  }
}
```

| Event | Consumers | Carries |
| --- | --- | --- |
| `order.created` | pricing, audit, analytics | Both ends, weight, vehicle type — everything Pricing needs to quote |
| `order.confirmed` | **dispatch**, notification, audit, analytics | Pickup coordinates, vehicle type, weight — everything Dispatch needs to match |
| `order.picked_up` | notification, analytics, audit | Order id, partner id, timestamp |
| `order.delivered` | **payment**, notification, analytics, audit | Order id, amount — Payment captures on this |
| `order.cancelled` | **payment** (refund), dispatch, notification, audit | Order id, previous status, reason. Previous status is what tells Payment whether there is anything to refund |

**Partition by `order_id`.** Kafka orders messages within a partition only. Put
`order.picked_up` and `order.delivered` on different partitions and a consumer
will eventually process the delivery before the pickup.

**The payload is what consumers need, not the whole row.** `order.confirmed`
carries coordinates because Dispatch matches on them; it does not carry the
customer's phone number, because Dispatch has no business with it and an event
is read by every consumer group including Analytics and Audit.

---

## Events it consumes

| Event | Producer | What Order does | Transition |
| --- | --- | --- | --- |
| `pricing.quoted` | pricing | Snapshot `quote_id`, `quoted_amount` | `CREATED → QUOTED` |
| `payment.authorized` | payment | Record intent, confirm the order | `PAYMENT_PENDING → CONFIRMED` |
| `partner.assigned` | dispatch | Snapshot partner name, phone, vehicle | `SEARCHING_PARTNER → PARTNER_ASSIGNED` |
| `payment.captured` | payment | Close the order out | `DELIVERED → SETTLED` |

Every handler is the same six lines, and the shape is not optional:

```python
def handle(event: Envelope, db: Session) -> None:
    # 1. Have we seen this event_id before? At-least-once delivery makes
    #    duplicates certain, not hypothetical.
    if db.get(ProcessedEvent, event.event_id):
        return

    order = repository.lock_for_update(event.data["order_id"])

    # 2. Is the move legal from where the order actually is? An out-of-order
    #    delivery is a fact of life, not an error to raise.
    if not can_transition(order.status, target):
        log.warning("ignoring %s: order %s is %s", event.event_type, order.id, order.status)
        db.add(ProcessedEvent(event_id=event.event_id))   # still consumed
        db.commit()
        return

    # 3. State change, history row, processed marker, and any outbox row this
    #    produces — one transaction. Either all of it happened or none did.
    order.status = target
    db.add(OrderStatusHistory(order_id=order.id, to_status=target,
                              caused_by_event_id=event.event_id, actor=event.producer))
    db.add(ProcessedEvent(event_id=event.event_id))
    db.commit()
```

Writing `processed_events` in the same transaction as the state change is what
turns at-least-once delivery into exactly-once *effect*. A separate commit for
the marker leaves a window where the work is done and the marker is not, and the
redelivery does it twice.

---

## The outbox

Order commits to Postgres and publishes to Kafka. Two systems, no shared
transaction: if the commit succeeds and the publish fails, the order exists and
Dispatch never hears about it. Nothing retries, nothing alerts, and a customer
sits waiting for a driver who was never requested.

```sql
CREATE TABLE outbox (
    id           BIGSERIAL PRIMARY KEY,
    event_id     UUID        NOT NULL UNIQUE,
    event_type   VARCHAR(64) NOT NULL,
    aggregate_id VARCHAR(64) NOT NULL,        -- order id: the Kafka partition key
    payload      JSONB       NOT NULL,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,
    attempts     INTEGER     NOT NULL DEFAULT 0,
    last_error   TEXT
);

-- Partial index: the relay only ever asks for unpublished rows, and this keeps
-- that query the same speed on day one and after a million orders.
CREATE INDEX ix_outbox_unpublished ON outbox (id) WHERE published_at IS NULL;
```

The write side never touches Kafka:

```python
def confirm(self, order: Order, event: Envelope) -> None:
    order.status = OrderStatus.CONFIRMED
    self.db.add(OrderStatusHistory(...))
    self.db.add(Outbox(event_id=uuid4(), event_type="order.confirmed",
                       aggregate_id=str(order.id), payload=build_payload(order)))
    self.db.commit()          # the order and its announcement, atomically
```

The relay is a separate loop — a thread in the service today, its own process
later:

```python
rows = db.execute(
    select(Outbox).where(Outbox.published_at.is_(None))
    .order_by(Outbox.id).limit(100)
    .with_for_update(skip_locked=True)      # two relays never fight over a row
).scalars().all()

for row in rows:
    producer.send(topic_for(row.event_type), key=row.aggregate_id, value=row.payload)
    row.published_at = now()
db.commit()
```

**The cost, stated plainly:** a relay can publish and crash before marking the
row sent, so it republishes. Delivery is at-least-once and duplicates are
certain — which is exactly why every event carries an `event_id` and every
consumer keeps `processed_events`.

> **Build this first, before the first event is ever published.** Retrofitting an
> outbox means finding every `producer.send()` in the system and unpicking it
> from the transaction it should have been in.

---

## Endpoints

All behind the gateway at `/api/orders`, all requiring a customer session.

| Route | Does |
| --- | --- |
| `POST /v1/orders` | Create. Resolves addresses, gets a quote, creates a payment intent. Takes an `Idempotency-Key` header |
| `GET /v1/orders` | The caller's orders, newest first. Never anyone else's |
| `GET /v1/orders/{id}` | One order, scoped to `customer_id` in the WHERE clause |
| `POST /v1/orders/{id}/cancel` | Legal up to `PICKED_UP`. Emits `order.cancelled`; Payment decides what to refund |
| `GET /internal/orders/{id}` | For Dispatch and Notification. `X-Internal-Key`, not routed by the gateway |

`Idempotency-Key` is not optional. A customer on a flaky connection taps
"Confirm" twice and the second request must return the first order, not create a
second one — the unique constraint on `(customer_id, idempotency_key)` is what
makes that true even under a race.

---

## Service layout

Following the blueprint's template, with the folders this service actually needs
on day one:

```
services/order/
├── Dockerfile
├── requirements.txt
├── alembic/                     # this service's migrations, its own history
└── src/
    ├── main.py                  # app factory + lifespan (starts the relay)
    ├── config.py
    ├── api/
    │   ├── deps.py              # current_customer from X-User-Id + X-Subject-Type
    │   └── v1/{router.py, orders.py}
    ├── domain/
    │   └── states.py            # the state machine above. Pure
    ├── models/                  # orders, status history, outbox, processed_events
    ├── schemas/
    ├── repositories/
    ├── services/                # create_order orchestrates User + Pricing + Payment
    ├── database/
    ├── events/
    │   ├── envelope.py          # the shape above, built once
    │   ├── publisher.py         # writes to outbox — never to Kafka directly
    │   ├── relay.py             # outbox → Kafka
    │   └── handlers.py          # the four consumers
    └── clients/
        ├── user.py              # the one call in this document
        ├── pricing.py
        └── payment.py
```

`clients/` is what makes "what does Order depend on?" a question you answer by
listing a folder. Three files means three synchronous dependencies — if a fourth
appears, that is a design conversation, not a merge.

**In this repository today**, services live at `src/services/<name>Services/`
with a shared `venv` and one `.env`, run as separate uvicorn processes. Order can
follow that convention now (`src/services/orderServices/`) and move to
`services/order/` with its own Dockerfile when containers land — the internal
layout above is unchanged either way, and the boundary that matters (its own
database, its own migrations) is already respected by the existing services.

---

## Build order

The blueprint says to prove the spine with Order and Dispatch joined by one real
event. Concretely, in this order:

1. **`docker-compose.yml` with Kafka** (plus the Postgres and Mongo already in
   use). One broker, one topic — `order-events`, 3 partitions.
2. **`libs/contracts/`** with the envelope and `order.confirmed`. Nothing else.
3. **Order: create + read, no events.** Tables, the state machine, the User call,
   `POST /v1/orders` returning 201. Verifiable end to end with no broker running.
4. **The outbox and the relay.** Still no consumer — assert rows appear in the
   topic. This is the step that is painful to add later and cheap to add now.
5. **Dispatch consumes `order.confirmed`** and emits `partner.assigned` through
   its own outbox.
6. **Order consumes `partner.assigned`,** snapshots the partner, and moves to
   `PARTNER_ASSIGNED`.

At step 6 the loop is closed and every hard problem in the blueprint — dual
writes, idempotency, partitioning, consumer groups, contract versioning — has
been solved once, at a scale where it is still debuggable. Pricing and Payment
then slot into a proven pattern.

Gate each step on a check that would fail loudly:

| After | It works when |
| --- | --- |
| 3 | `POST /v1/orders` with a bad `address_id` returns 404, and a good one snapshots the address |
| 4 | Kill the relay mid-publish; restart it; the event arrives exactly once at the consumer, twice in the log |
| 6 | `order.confirmed` → `partner.assigned` round trip with Dispatch restarted mid-flight, and the order ends in the right state |

---

## Open decisions

Answer each before the code that depends on it, not after.

- **`SEARCHING_PARTNER`: who moves the order into it?** Order on emitting
  `order.confirmed`, or Dispatch acknowledging it. The first is simpler; the
  second is honest about whether anyone is actually searching.
- **Cancellation is a saga, not a transition.** Money may be authorized. Payment
  owns the compensating flow — Order emits `order.cancelled` and does *not* wait
  for the refund. Decide what the customer sees in the gap.
- **Quote expiry.** A quote is a price at a moment. If payment lands after the
  quote expires, does Order re-quote, honour it, or fail? Pricing needs the
  answer before it issues its first quote.
- **Does Order call Pricing, or emit and wait?** This document assumes a
  synchronous call because the customer is watching a spinner. If quoting ever
  gets slow, that assumption is the first thing to revisit.
- **Topic granularity.** One `order-events` topic, or one per event type. One
  topic keeps ordering per order id trivially correct, which is why it is the
  default here.
