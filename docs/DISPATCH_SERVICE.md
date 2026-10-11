# Dispatch Service

Which partner gets this order. Port **8004**, database **dispatch_db**.

Dispatch answers one question — *who gets it?* — and that is a policy question,
not a fact about the fleet. partnerServices already answers *who could take
this?*; the difference between the two is offers, timeouts, declines, races and
giving up, and all of it lives here.

Companions: [EVENTS.md](EVENTS.md) for the spine it rides on,
[PARTNER_SERVICE.md](PARTNER_SERVICE.md) for the availability search it calls,
[PLATFORM_WORKFLOW.md](PLATFORM_WORKFLOW.md) for where this falls in a delivery.

---

## Contents

- [Why it is not part of partnerServices](#why-it-is-not-part-of-partnerservices)
- [The shape of it](#the-shape-of-it)
- [The offer loop](#the-offer-loop)
- [State](#state)
- [The three hard parts](#the-three-hard-parts)
- [What it consumes and emits](#what-it-consumes-and-emits)
- [The API](#the-api)
- [Configuration](#configuration)
- [Running it](#running-it)
- [Failure modes](#failure-modes)

---

## Why it is not part of partnerServices

partnerServices already does the haversine search, filters on verification,
active vehicle, online status and heartbeat freshness, and returns candidates
nearest-first with distances. Adding "and offer it to the first one" to that
service is a few hundred lines and would work.

It is still the wrong place, for one reason:

> partnerServices owns driver **accounts**. Matching strategy changes far more
> often than account handling does.

Put the loop inside partnerServices and you can never change how work is
allocated — a different ranking, a batch auction, surge-aware matching, an
acceptance-rate weighting — without redeploying the service that holds every
driver's login, KYC status and vehicle documents. Two things with completely
different rates of change end up sharing a release.

The split is clean:

| | partnerServices | dispatchServices |
| --- | --- | --- |
| Answers | Who *could* take this? | Who *gets* it? |
| Owns | Accounts, KYC, vehicles, status, location | Attempts, offers, the policy |
| Kind of answer | A fact about the fleet | A decision |
| Changes when | Onboarding rules change | Matching strategy changes |

---

## The shape of it

```
   Kafka: order.confirmed
            │
            ▼
   ┌─────────────────────┐   writes one row, returns
   │  events/consumers   │──────────────┐
   └─────────────────────┘              │
                                        ▼
                              ┌───────────────────┐
                              │ dispatch_attempts │  status=searching
                              └─────────┬─────────┘
                                        │
        ╔═══════════════════════════════╧═══════════════════════════════╗
        ║  services/dispatch_worker.py — ticks every second             ║
        ║                                                               ║
        ║   expire live offers past their TTL                           ║
        ║   for each searching attempt:                                 ║
        ║       out of time?          → fail, emit search_failed        ║
        ║       offer already live?   → nothing to do                   ║
        ║       nobody queued?        → GET /internal/partners/available ║
        ║       otherwise             → offer the next candidate        ║
        ╚═══════════════════════════════╤═══════════════════════════════╝
                                        │
                              ┌─────────▼─────────┐
                              │  dispatch_offers  │  queued → offered → …
                              └─────────┬─────────┘
                                        │
   driver's app  ──GET /api/dispatch/offers/current──┘
                 ──POST .../accept──▶ claim on partnerServices (409 = lost)
                                   ──▶ emit partner.assigned
```

**The Kafka handler does not run the loop.** It writes the attempt row and
returns. A handler that ran a three-minute search would hold its partition open
the whole time, and every order behind it would wait.

The loop is a polling worker instead, and it is driven entirely from table
state — the ranked candidate list, who has been asked, what they said, and how
long the live offer has left are all rows. Kill the process mid-search and the
next tick resumes exactly where it stopped, because there was never anything in
memory to lose.

---

## The offer loop

**One driver at a time.** Broadcasting to five drivers means four of them tap
accept and four of them are told no. The database enforces it:

```sql
CREATE UNIQUE INDEX uq_dispatch_offers_one_live
  ON dispatch_offers (attempt_id) WHERE status = 'offered';
```

A partial unique index is the only way to say "unique among rows in this state"
in Postgres. A plain `UNIQUE(attempt_id)` would forbid the queue; a check in
Python would be one concurrent tick away from being wrong.

One tick does **at most one thing** per attempt, which is what keeps the
behaviour readable in the log:

| Condition | What happens |
| --- | --- |
| Past `deadline_at` | Fail. Expire the live offer, supersede the queue, emit `dispatch.search_failed` |
| An offer is live | Nothing. Its TTL is the sweep's business |
| Queue empty, backing off | Nothing until `retry_search_after` |
| Queue empty, offers capped | Fail with `all_declined` |
| Queue empty | Search partnerServices, queue what comes back |
| Search returned nobody | Widen the radius, set a backoff |
| Somebody queued | Offer it to them, emit `partner.offered` |

**Widening.** Start at `SEARCH_RADIUS_KM`, multiply by `SEARCH_WIDEN_FACTOR` on
an empty result, stop at `SEARCH_MAX_RADIUS_KM`. Past some distance the nearest
driver is not a useful answer — twenty minutes away is worse for the customer
than being told to try again.

**A driver is asked at most once per order.** `uq_dispatch_offers_attempt_partner`
enforces it, and the loop filters on the same set before inserting, so a widened
re-search adds only people the first radius could not see.

**Declining is worth an endpoint.** A decline moves the job on within a second
instead of thirty, and thirty seconds per uninterested driver is most of why a
customer waits.

---

## State

Two state machines, in `domain/states.py`. Neither describes the order or the
partner — those belong to the services that own them. An attempt can be
`assigned` while Order still says `searching_partner`, for the second it takes
the event to arrive, and that is not an inconsistency to design away.

### `dispatch_attempts.status`

```
   searching ──accept──▶ assigned      (terminal)
       │
       ├──deadline / all declined──▶ failed     (terminal)
       └──order.cancelled──────────▶ cancelled  (terminal)
```

### `dispatch_offers.status`

```
   queued ──▶ offered ──┬─▶ accepted    the job is theirs
      │          │      ├─▶ declined    they said no
      │          │      ├─▶ expired     they said nothing in time
      │          │      └─▶ lost_race   they accepted, someone else was claimed first
      └──────────┴────────▶ superseded  never asked; the attempt ended
```

`queued` exists so the candidate list is **persisted**, not held in memory —
that is what makes the loop restartable.

The three rejection reasons are kept apart all the way onto the topic, because
they are three different facts and only one of them is about the driver's
choice. A partner who never answers is an operations problem; one who declines
is a pricing signal; one who lost a race is neither, and it must never count
against them.

---

## The three hard parts

### 1. The claim is the referee

```
two dispatches, one driver, same instant
        │                    │
        ├─ PATCH /internal/partners/7/status {"status":"on_trip"}
        │                    ├─ PATCH  (same)
        ▼                    ▼
   200 — claimed        409 Conflict
   assign the order     mark the offer lost_race, offer the next candidate
```

partnerServices' state machine lets exactly one `ONLINE → ON_TRIP` through. That
409 is not an error to handle away — it is the answer, and at any real volume it
happens constantly.

**Order of operations in `accept()` is the whole thing:**

1. Lock the attempt row, re-read the offer under that lock.
2. Refuse anything not live, and check the clock here as well as in the sweep —
   a driver tapping at T+0.2s must not take a job already handed on.
3. **Claim on partnerServices.**
4. Only then record the acceptance and emit `partner.assigned`.

Doing (3) after (4) would let two orders be assigned the same driver and leave
the second to be unpicked by hand.

### 2. Lock ordering

Both the worker and a driver's accept mutate the same attempt. Every path in
`services/dispatch_services.py` takes **the attempt lock first, then the
offer's** — `SELECT ... FOR UPDATE`, blocking rather than skipping. Taking them
the other way round anywhere is how the two deadlock.

Blocking rather than `SKIP LOCKED` is deliberate: the loser should proceed once
the winner commits and *then* discover the offer is gone, not silently do
nothing.

### 3. The availability query has to be synchronous

Dispatch cannot rank candidates it does not have, and a read model fed by
`location.updated` would happily offer work to a driver who went offline two
minutes ago. When the query gets hot, Dispatch keeps its own read model — an
optimisation, not the starting point.

---

## What it consumes and emits

**Consumes**

| Event | What Dispatch does |
| --- | --- |
| `order.confirmed` | Write an attempt, emit `dispatch.search_started`. The worker takes it from there |
| `order.cancelled` | Stop offering, release any claimed driver, emit `partner.released` |
| `order.delivered` | Release the driver: `ON_TRIP → ONLINE`, emit `partner.released` |

Dispatch releases the driver rather than partnerServices doing it, because
Dispatch is what claimed them: the service that took a driver out of the pool
owes putting them back.

**Emits**

| Event | When |
| --- | --- |
| `dispatch.search_started` | An attempt is created. Order moves `CONFIRMED → SEARCHING_PARTNER` |
| `partner.offered` | A job goes on a driver's screen. Notification pushes it |
| `partner.rejected` | Declined, expired, or lost the race |
| `partner.assigned` | Somebody accepted and was claimed. **The event the service exists to emit** |
| `dispatch.search_failed` | Deadline, offer cap, or nobody ever available |
| `partner.released` | A claimed driver handed back |

`dispatch.search_failed` carries a `reason`, and it is load-bearing in the same
way `previous_status` is on `order.cancelled`: it lets a consumer act without
asking Dispatch anything. Order decides whether to cancel; Notification decides
what the customer reads. "No partners are on duty near you" and "we asked eight
and all declined" are not the same message.

**Dispatch never calls Order and Order never calls Dispatch.** The only HTTP
either makes is to partnerServices.

---

## The API

Three endpoints for drivers, three for operations. There is **no endpoint to
request a driver**, and there never will be — Dispatch learns that an order
needs one from `order.confirmed`. A customer who could call Dispatch could
dispatch an order they had not paid for.

### Driver — `/api/dispatch/*` (partner cookie)

| Method | Path | What |
| --- | --- | --- |
| GET | `/offers/current` | What is on this driver's screen, or `null`. Polled |
| POST | `/offers/{id}/accept` | Take the job |
| POST | `/offers/{id}/decline` | Say no. Moves it on immediately |

`GET /offers/current` returns `null` most of the time, as a **200**. "No offer
for you at the moment" is a successful poll; an app treating it as an error
would be showing failures all day. `seconds_remaining` is computed server-side —
a phone whose clock is thirty seconds fast would otherwise end the countdown
before the offer does.

Accept has four legitimate non-successes, and they are four statuses because
they are four different things to tell a driver:

| Status | Means |
| --- | --- |
| 404 | No such offer, or not theirs. The same answer either way |
| 410 Gone | The countdown ran out. The job existed; they were too slow |
| 409 Conflict | Somebody else was claimed first, or they already answered |
| 503 | partnerServices unreachable. Nothing claimed, nothing recorded — tapping again works |

**Polling, not push, for now.** `partner.offered` reaches Notification, which
logs where a `push.send` job will go. Until the push worker exists, the driver's
app polls `/offers/current`. The contract does not change when push lands.

### Operations — `/internal/dispatch/*` (not routed by the gateway)

| Method | Path | What |
| --- | --- | --- |
| GET | `/attempts?status=searching` | Everything currently hunting for a driver |
| GET | `/orders/{id}` | One order's search and every offer it made |
| POST | `/orders/{id}/cancel` | Stop a search by hand, release any claimed driver |

`GET /orders/{id}` is the support answer in one call: who was asked, in what
order, how far away, and what each of them did. That is what Dispatch owns a
database for.

The cancel is a **manual override**, not part of the flow — cancellation
normally arrives as `order.cancelled`. It does not cancel the order: Dispatch
does not own the order and must not pretend to.

---

## Configuration

All optional; the defaults are in `config.py`.

| Key | Default | What |
| --- | --- | --- |
| `DISPATCH_DB_NAME` | `dispatch_db` | Its own database |
| `PARTNER_SERVICE_URL` | `http://127.0.0.1:8002` | The one service it calls |
| `PARTNER_SERVICE_TIMEOUT_SECONDS` | `3.0` | Short: a driver is waiting on the loop |
| `INTERNAL_API_KEY` | *(empty)* | For `/internal/*`, both directions |
| `OFFER_TTL_SECONDS` | `30` | **The most user-visible number here** |
| `SEARCH_RADIUS_KM` | `5.0` | Where the search starts |
| `SEARCH_WIDEN_FACTOR` | `2.0` | Multiplier on an empty result |
| `SEARCH_MAX_RADIUS_KM` | `15.0` | Where widening stops |
| `SEARCH_LIMIT` | `20` | Candidates per query |
| `SEARCH_DEADLINE_SECONDS` | `180` | Give-up condition one |
| `MAX_OFFERS_PER_ORDER` | `10` | Give-up condition two |
| `EMPTY_SEARCH_BACKOFF_SECONDS` | `15` | Do not re-query at 4am every second |
| `WORKER_TICK_SECONDS` | `1.0` | Bounds how late an expiry can be |

`OFFER_TTL_SECONDS` is the one to think about. Too short and drivers cannot
reach their phone; too long and a customer waits through a queue of people who
were never going to answer.

---

## Running it

```powershell
uvicorn src.services.dispatchServices.main:app --port 8004 --reload
```

Three background loops start with it: the outbox relay, the Kafka consumer, and
the offer worker. All three are cancelled cleanly on shutdown.

`GET /health` reports `events_enabled`, which is worth watching — a `false`
there means no delivery will ever be dispatched, and that otherwise looks
exactly like a quiet day.

Migrations:

```powershell
alembic -c alembic_dispatch.ini upgrade head    # -> d5b30f81ac47 (head)
```

### Driving it by hand

With `KAFKA_ENABLED=false` there is no `order.confirmed` to consume, so nothing
starts. Use Order's manual override to move the state machine instead:

```powershell
curl.exe -X POST http://127.0.0.1:8003/internal/orders/42/assignment `
  -H "Content-Type: application/json" -H "X-Internal-Key: <key>" `
  -d '{\"partner_id\":3,\"partner_name\":\"Ravi K\",\"partner_phone\":\"9876543210\"}'
```

---

## Failure modes

**Nothing is being dispatched.**
Check `events_enabled` on `/health`, then the order service's outbox:
`SELECT * FROM outbox WHERE published_at IS NULL` against `order_db`. Rows with
`attempts = 0` mean no relay is running.

**An attempt sits at `searching` forever.**
It cannot past `deadline_at` — the worker fails it. If it genuinely is not
moving, the worker loop is dead: look for `dispatch worker tick failed` in the
log. Loops catch and log rather than dying, so a traceback there is the answer.

**Every accept returns 409.**
partnerServices thinks the driver is not `ONLINE`. Usually they are already
`ON_TRIP` from a delivery that never completed, or `SUSPENDED`. Check
`GET /internal/partners/{id}`.

**Every search finds nobody.**
`/internal/partners/available` is filtering everyone out. A partner has to be
verified, `ONLINE`, on an `ACTIVE` verified vehicle, *and* have sent a location
heartbeat inside `PARTNER_LOCATION_STALE_MINUTES`. The heartbeat is the one that
catches people out — a driver who set themselves online and closed the app is
filtered out by design.

**Drivers report offers that vanish.**
Expected when `OFFER_TTL_SECONDS` elapses, or when the order was cancelled. The
attempt's offer list shows which: `GET /internal/dispatch/orders/{id}`.

**A driver is stuck `ON_TRIP` after a delivery.**
`order.delivered` did not reach Dispatch. Check `processed_events` in
`dispatch_db` for that event id, and the consumer group's lag. Release by hand
with `PATCH /internal/partners/{id}/status {"status":"online"}`.
