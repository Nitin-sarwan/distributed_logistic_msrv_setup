# Platform Workflow

One delivery, end to end: which service does the work at each step, how it
reaches the next one, and what state changes when.

Two orderings appear in this document and they are **not** the same:

- **The runtime flow** — what happens when a customer books a delivery. Pricing
  comes before Dispatch, payment before confirmation.
- **The build order** — what to write first. Order and Dispatch come before
  Pricing and Payment, with those two stubbed, because the spine is what proves
  every mechanism.

Building in runtime order is the most common way a distributed system like this
stalls: four services deep before anything can be demonstrated end to end.

Companions: [ORDER_SERVICE.md](ORDER_SERVICE.md) for the service at the centre
of this, [ARCHITECTURE.md](ARCHITECTURE.md) for how the current processes are
wired.

---

## Contents

- [Where the project actually is](#where-the-project-actually-is)
- [Who does what](#who-does-what)
- [The delivery, step by step](#the-delivery-step-by-step)
- [Cancellation](#cancellation)
- [The connection matrix](#the-connection-matrix)
- [Build order](#build-order)
- [Rules that apply at every step](#rules-that-apply-at-every-step)

---

## Where the project actually is

| Blueprint service | Status in this repo | Notes |
| --- | --- | --- |
| gateway | **Built** | Routes `/api/users`, `/api/partners`, `/api/geo`. Needs the app-type fix in [ORDER_SERVICE.md](ORDER_SERVICE.md#trust-and-identity) |
| user | **Built** | Auth, sessions, addresses. Needs one internal endpoint and three events |
| partner | **Built** | Auth, KYC gate, vehicles, availability, location heartbeat, **and the availability search Dispatch needs** |
| geo | **Built** | Not in the blueprint's ten — address search and reverse lookup, shared by both apps |
| order | **Missing** | The next thing to write |
| dispatch | **Missing** | Can start thin: partner already answers "who is nearby?" |
| pricing | **Missing** | Stub it first — a flat rate per km is enough to prove the spine |
| payment | **Missing** | Stub it first — auto-authorize in development |
| location | **Missing, and deferrable** | Partner positions already live in `partners.current_latitude/longitude`. Extract when GPS volume justifies a separate store |
| notification | **Missing** | Nothing is emitted yet for it to consume |
| audit / analytics | **Missing** | Cheap to add — wildcard consumers, once Kafka exists |
| Kafka / RabbitMQ | **Missing** | Not in `docker-compose.yml` yet |
| Frontend | **Built, ahead of the backend** | Customer booking widget collects both ends with coordinates; partner app has availability, vehicles, and live position. Both are waiting on an order API |

The important line in that table is **partner**: `GET /internal/partners/available`
already does the haversine search, filters on verification, active vehicle,
online status, and heartbeat freshness, and returns candidates nearest-first
with distances. Dispatch's first version does not need a read model, a Location
service, or Redis — it needs to call that endpoint.

---

## Who does what

Each service in one line: the decision it owns, and the thing it must never do.

| Service | Decides | Must never |
| --- | --- | --- |
| **gateway** | Is this caller authenticated, and where does the request go | Contain business logic, or ask a service to validate a token |
| **user** | Who a customer is, and what addresses they have saved | Know that orders exist |
| **partner** | Who may work, on what vehicle, and who is available near a point | Decide who gets a specific order |
| **order** | What was agreed, and what state the delivery is in | Choose a partner, price anything, or move money |
| **pricing** | What a delivery costs | Know whether it was paid for |
| **payment** | Whether money moved, and where it goes | Know what was delivered |
| **dispatch** | Which partner gets this order | Own the order, or the partner |
| **location** | Where a partner is right now | Interpret why anyone wants to know |
| **notification** | Which channel and template, and who has opted out | Contain the reason a message is being sent |
| **audit / analytics** | Nothing. They only read | Be called synchronously by anyone |

The split that is easiest to get wrong is **partner vs dispatch**. Partner
answers *"who could take this?"* — a fact about the fleet. Dispatch answers
*"who gets it?"* — a policy decision involving offers, timeouts, rejections, and
retries. Put the second inside partnerServices and you can never change matching
strategy without redeploying the service that owns driver accounts.

---

## The delivery, step by step

### Phase 0 — Accounts and sessions (built)

Two separate account systems, two databases, two cookies. A driver may also be
a customer; neither session knows about the other.

```
Customer                              Partner
   │ POST /api/users/register            │ POST /api/partners/register
   │ POST /api/users/login               │ POST /api/partners/login   (phone, not email)
   ▼                                     ▼
userServices                        partnerServices
   │ session → shared Mongo, app_type 1  │ session → shared Mongo, app_type 2
   │ cookie: lp_session                  │ cookie: lp_partner_session
   ▼                                     ▼
                    Ops clears KYC:  POST /internal/partners/{id}/verification
                    Ops clears docs: POST /internal/partners/vehicles/{id}/verification
```

**To add:** `user.registered`, `user.profile_updated`, `user.deleted` — three
events, consumed by notification, analytics, and audit. Nothing in the order
flow depends on them, which is why they are not urgent.

A partner cannot receive work until three things are true, and each is enforced
independently by partnerServices: KYC verified, a vehicle at `active`, and
status `online`.

### Phase 1 — The partner goes on duty (built)

```
Partner app                     partnerServices
   │ PATCH /api/partners/me/status  {"status": "online"}
   ├───────────────────────────────────>│ refuses unless verified + active vehicle
   │                                    │
   │ POST /api/partners/me/location     │  every 60s while online
   ├───────────────────────────────────>│ stamps location_updated_at
   │                                    ▼
   │                          discoverable by /internal/partners/available
```

The heartbeat is what makes availability real. A partner marked `online` whose
last fix is older than `PARTNER_LOCATION_STALE_MINUTES` is filtered out of the
search — status says what they chose, the timestamp says whether the platform
can still find them.

**Later:** this heartbeat is the write path that moves to the Location service,
and the point at which `location.updated` starts flowing onto Kafka.

### Phase 2 — The customer builds an order

```
Customer app                    gateway        Order            User      Pricing
   │ picks pickup + drop
   │  (saved address, /api/geo search, or a dragged pin)
   │
   │ POST /api/orders {pickup:{address_id}, drop:{…}, package, vehicle_type}
   ├──────────────────────────────>│──────────>│
   │                               │           │ GET /internal/users/7/addresses/12
   │                               │           ├──────────>│   ← snapshot address + contact
   │                               │           │
   │                               │           │ POST /internal/pricing/quotes
   │                               │           ├──────────────────────>│  ← amount, quote_id
   │                               │           │
   │                               │           │ commit: order + outbox(order.created)
   │  201 {order_id, status: quoted, amount}   │
   │<──────────────────────────────────────────┤
```

Two synchronous calls, both because the customer is watching a spinner and
neither answer can arrive later. Everything the order will ever need about the
customer is now copied into the row — see the snapshot rule in
[ORDER_SERVICE.md](ORDER_SERVICE.md#three-alternatives-and-why-each-is-wrong).

`order.created` goes out through the outbox. Pricing, audit, and analytics hear
it; nobody has to be listening for the order to be valid.

### Phase 3 — Payment

```
Order ──REST──> Payment            Client ──SDK──> Provider
  create intent                      completes payment
     │                                      │
     │ PAYMENT_PENDING                      │ webhook (signed, unauthenticated at the edge)
     │                                      ▼
     │                              Payment /webhooks/razorpay
     │                                      │ verify signature, dedupe, 200 fast
     │                                      ▼
     │                              emits payment.authorized ──Kafka──┐
     ▼                                                                │
   Order consumes ◄──────────────────────────────────────────────────┘
     │ PAYMENT_PENDING → CONFIRMED
     └─ emits order.confirmed
```

The intent is created synchronously — the client needs its reference to open the
payment sheet. The *outcome* arrives as an event minutes later, from a provider
webhook, and nothing may block on it.

`order.confirmed` is the single most important event in the platform: it is what
turns "someone wants a delivery" into "find them a driver", and Order emits it
without knowing Dispatch exists.

### Phase 4 — Finding a partner (Dispatch)

```
                 Kafka: order.confirmed
                          │
                          ▼
                     Dispatch
                          │ GET /internal/partners/available
                          │   ?lat&lng&vehicle_type&min_capacity&radius_km
                          ├────────────────────────────> partnerServices
                          │   ← [{partner, vehicle, distance_km}, …] nearest first
                          │
             ┌────────────┴─────────────┐
             │  offer loop, one at a time│
             │  ┌─────────────────────┐  │
             │  │ offer → partner app │  │   push (RabbitMQ → FCM), 30s TTL
             │  │   accept → break    │  │
             │  │   reject / timeout  │──┼──> emits partner.rejected, next candidate
             │  └─────────────────────┘  │
             └────────────┬──────────────┘
                          │ on accept:
                          │ PATCH /internal/partners/{id}/status {"status":"on_trip"}
                          │   ← 409 means someone else claimed them: try the next one
                          │
                          └─ emits partner.assigned ──Kafka──> Order, notification, location, audit
                                                                 │
                                                    Order: SEARCHING_PARTNER → PARTNER_ASSIGNED
                                                    snapshots partner name, phone, vehicle number
```

Three things in that diagram carry the design:

- **The availability query is synchronous.** Dispatch cannot rank candidates it
  does not have, and a stale read model would offer work to a driver who went
  offline two minutes ago. Later, if the query becomes hot, Dispatch keeps its
  own read model fed by `location.updated` — an optimisation, not the starting
  point.
- **The 409 on claiming a partner is the concurrency control.** Two dispatches
  racing for one driver both call `PATCH /internal/partners/{id}/status`; the
  state machine in partnerServices lets exactly one of them through. Without
  that, two customers get the same driver.
- **Rejection is an ordinary outcome, not an error.** `partner.rejected` feeds
  analytics and Dispatch's own retry loop. After N candidates or T seconds,
  Dispatch gives up and says so — and Order decides what the customer sees.

### Phase 5 — Pickup

```
Partner app ──POST /api/orders/{id}/picked-up──> Order
                                                   │ authorises: X-Subject-Type = partner
                                                   │             AND partner_id == order.partner_id
                                                   │ PARTNER_ASSIGNED → PICKED_UP
                                                   └─ emits order.picked_up
```

This is the one endpoint a **partner** calls on an order, and it is why the
gateway must forward the subject type rather than just an id. Order checks two
things: the caller is a partner, and they are *this order's* partner — a
snapshot comparison, no call to partnerServices.

### Phase 6 — Tracking

```
Partner app ──heartbeat──> Location ──location.updated──> Kafka ──> dispatch, tracking stream
                              │
Customer app ────────────────►│  live position of THIS order's partner, while the order is active
```

Two rules keep this from becoming surveillance:

- A customer may read a partner's position **only while that partner is on their
  order**, and only between `partner.assigned` and `order.delivered`.
- The order id is the key, not the partner id. Nothing in the customer API takes
  a partner id, so there is no endpoint that can be walked.

**Transport is an open decision** (WebSocket through the gateway vs SSE from
Location). Until it is answered, polling `GET /api/orders/{id}/tracking` every
few seconds is correct, cheap, and easy to replace — the shape of the answer
does not change.

### Phase 7 — Delivery and settlement

```
Partner app ──POST /api/orders/{id}/delivered──> Order
                                                   │ PICKED_UP → IN_TRANSIT → DELIVERED
                                                   └─ emits order.delivered ──> payment, notification, analytics
                                                                                   │
                              Payment captures the authorized amount ◄─────────────┘
                                     └─ emits payment.captured ──> Order: DELIVERED → SETTLED
                                                                   partner (payout)

  Dispatch (or partnerServices) returns the partner: on_trip → online
  Notification: order.delivered ──> RabbitMQ email.send ──> email worker ──> receipt
  Customer rates the delivery ──> POST /internal/partners/{id}/rating
```

**Delivered is not settled.** The goods arriving and the money moving are two
facts with two owners and two failure modes; collapsing them means either
holding the customer's payment hostage to a webhook, or calling an order
complete before anyone has been paid.

The rating call is REST today because partnerServices does not consume Kafka
yet. The moment it does, this becomes `order.rated` and the last synchronous
call out of the order flow disappears.

---

## Cancellation

Not a transition — a compensating saga, because money may already have moved.

```
Customer ──POST /api/orders/{id}/cancel──> Order
                                             │ legal up to PICKED_UP
                                             │ → CANCELLED
                                             └─ emits order.cancelled {previous_status, reason}
                                                   │
        ┌──────────────────────────┬───────────────┴────────────┬──────────────────┐
        ▼                          ▼                            ▼                  ▼
     Payment                   Dispatch                   Notification          Audit
  previous_status decides:   stop offering;            tell the partner if     records it
   quoted    → nothing       release the partner        one was assigned
   authorized→ void
   captured  → refund
```

`previous_status` is the field that makes this work: it is what tells Payment
whether there is anything to reverse, without Payment having to ask Order.

After `PICKED_UP`, cancellation is not available — goods are in a vehicle, and
the resolution is a return trip, which is a new order.

---

## The connection matrix

**Synchronous (REST).** Every row is two services whose uptime is now coupled;
the list is deliberately short.

| Caller | Callee | When | If it fails |
| --- | --- | --- | --- |
| gateway | all services | every request | 502/504 to the client |
| order | user | order creation, address snapshot | 503, order not created |
| order | pricing | order creation, quote | 503, order not created |
| order | payment | order creation, intent | 503, order not created |
| dispatch | partner | after `order.confirmed`, candidate search | retry with backoff; order stays `SEARCHING_PARTNER` |
| dispatch | partner | on accept, claim the partner | 409 → offer the next candidate |
| order | partner | rating (until partner consumes Kafka) | log and drop; a lost rating is not worth failing a request |

**Asynchronous (Kafka).** Add a consumer here without touching the producer.

| Event | Producer | Consumers |
| --- | --- | --- |
| `user.registered` / `profile_updated` / `deleted` | user | notification, analytics, audit |
| `order.created` | order | pricing, audit, analytics |
| `pricing.quoted` | pricing | order, audit |
| `payment.authorized` | payment | order, audit |
| `order.confirmed` | order | **dispatch**, notification, audit, analytics |
| `partner.assigned` | dispatch | order, notification, location, audit |
| `partner.rejected` | dispatch | dispatch (retry), analytics |
| `order.picked_up` | order | notification, analytics, audit |
| `location.updated` | location | tracking stream, dispatch |
| `order.delivered` | order | payment, notification, analytics, audit |
| `payment.captured` | payment | order, partner (payout), audit |
| `order.cancelled` | order | payment, dispatch, notification, audit |

**Work queues (RabbitMQ).** One consumer, exactly once, with retries and a
dead-letter queue: `email.send`, `push.send`. Notification is the bridge — it
consumes domain facts from Kafka and enqueues jobs on RabbitMQ.

---

## Build order

Each phase ends with something demonstrable. Nothing in a later phase is needed
to prove an earlier one.

### Phase 1 — An order exists

- Gateway: app-type check, `X-Subject-Type`, register `/api/orders`.
- User: `GET /internal/users/{id}/addresses/{id}`.
- Order: tables, state machine, `POST /v1/orders`, `GET /v1/orders`.
- **Stub Pricing** inside Order behind `clients/pricing.py`: flat base + per-km
  on the haversine distance. One file to delete later.
- **Stub Payment**: creation goes straight to `CONFIRMED` in development.

**Done when:** the existing booking widget creates a real order with snapshotted
addresses, and a bad `address_id` returns 404. No broker running.

### Phase 2 — The spine (the phase that matters)

- `docker-compose.yml`: Kafka, one broker, topic `order-events`, 3 partitions.
- `libs/contracts/`: the envelope, `order.confirmed`, `partner.assigned`.
- Order: outbox table, relay, `processed_events`.
- Dispatch: consume `order.confirmed` → call `/internal/partners/available` →
  offer to the nearest candidate → claim via `PATCH /internal/partners/{id}/status`
  → emit `partner.assigned` through its own outbox.
- Order: consume `partner.assigned`, snapshot the partner.

**Done when:** an order goes from confirmed to assigned with no HTTP between
Order and Dispatch, and the round trip survives restarting either service
mid-flight. Every hard problem in the blueprint — dual writes, idempotency,
partitioning, consumer groups — is now solved once.

### Phase 3 — The partner side of a delivery

- Partner offer screen: accept / reject with a countdown.
- Order: `picked-up` and `delivered` endpoints, partner-authorised.
- Partner returns to `online` on completion.

**Done when:** one person on two devices can run a delivery start to finish.

### Phase 4 — Real money

- Pricing as its own service; delete the stub.
- Payment with provider webhooks in `api/webhooks/`, signature-verified and
  idempotent; `payment.authorized` and `payment.captured`.
- Order stops auto-confirming.

**Done when:** an order cannot reach `CONFIRMED` without an authorization, and a
webhook replayed three times charges once.

### Phase 5 — Telling people things

- RabbitMQ, Notification as the Kafka→RabbitMQ bridge, email and push workers.
- Cancellation saga end to end.

### Phase 6 — Seeing what happened

- Audit: wildcard consumer (`order.*`, `payment.*`, `partner.*`), append-only.
- Analytics: read models.

Both are pure consumers — they can be added on any afternoon once Kafka carries
real traffic, which is the point of having built the log first.

### Phase 7 — Only when the numbers demand it

- Location as its own service with Mongo + Redis, when GPS volume outgrows
  `partners.current_latitude`.
- Dispatch read model fed by `location.updated`, when the availability query
  gets hot.
- Live tracking transport decision.

> **The tempting mistake** is to build Pricing and Payment before Dispatch,
> because that is the order the customer experiences. It puts the two services
> with the most external dependencies — tariffs, a payment provider, webhooks,
> refunds — in front of the one thing that proves the architecture works.

---

## Rules that apply at every step

- **Snapshot, don't reference.** Addresses, prices, partner names. An order is a
  record of what was agreed, and it must not change when a customer edits their
  address book.
- **Every event carries `event_id` and `occurred_at`; every consumer stores
  processed ids.** At-least-once delivery makes duplicates certain.
- **Every event is written through an outbox,** in the same transaction as the
  state change it describes.
- **Partition by `order_id`.** Ordering is only guaranteed within a partition.
- **A correlation id is generated at the gateway** and carried through every
  HTTP header and event envelope. One delivery touches six services; without it,
  debugging is guesswork.
- **Auth terminates at the gateway.** No service validates a token, and no
  service is publicly routable.
- **Migrations ship with the service.** One Alembic history per database.
- **Past tense for events, always.** `order.confirmed`, never `confirm_order`.
  A command on Kafka is a boundary in the wrong place.
