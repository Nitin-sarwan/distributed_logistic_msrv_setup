# Notification Service

Telling people things. Port **8005**, database **notification_db**.

The rule that defines this service, from the blueprint's own table:

> **Decides:** which channel and template, and who has opted out.
> **Must never:** contain the reason a message is being sent.

Everything below follows from that one line.

Companions: [EVENTS.md](EVENTS.md) for the spine it consumes,
[DISPATCH_SERVICE.md](DISPATCH_SERVICE.md) for where offers come from.

---

## Contents

- [What it is for](#what-it-is-for)
- [The shape of it](#the-shape-of-it)
- [Templates](#templates)
- [Channels](#channels)
- [Preferences](#preferences)
- [Storage](#storage)
- [The API](#the-api)
- [Configuration](#configuration)
- [Running it](#running-it)
- [What is stubbed](#what-is-stubbed)
- [Failure modes](#failure-modes)

---

## What it is for

Before this service, every message would have been sent by whichever service
happened to know something had happened. Order would send the confirmation
email, Dispatch would push the offer, Payment would send the receipt — and each
would need an email template, a push credential, and its own copy of "has this
person opted out?".

Instead: services emit facts, and one service turns facts into messages.

The consequence worth internalising is that **there is no endpoint here that
sends a notification**, and adding one would be the single worst change anyone
could make to this service. A service that can be told to send a message
contains none of the reasons messages are sent — and the moment it exists, every
other service starts calling it instead of emitting an event, and the coupling
the log removed comes straight back.

---

## The shape of it

```
   Kafka: order-events (every event, one consumer group)
                        │
                        ▼
            ┌───────────────────────┐
            │  events/consumers.py  │  one handler, registered per template
            └───────────┬───────────┘
                        ▼
            ┌───────────────────────┐
            │  domain/templates.py  │  what to say, to whom, on which channels
            └───────────┬───────────┘   pure functions of a dict
                        ▼
            ┌───────────────────────┐
            │  preference check     │  who asked not to hear it
            └───────────┬───────────┘
                        ▼
            ┌───────────────────────┐
            │  notifications table  │  the feed, and the permanent record
            └───────────┬───────────┘
                        ▼
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
     in_app          push            email
   (the row)      [logged]         [logged]
                        │               │
                        └── RabbitMQ ───┘  ← where the real workers plug in
```

One event can produce **several** messages, to different people, on different
channels. `partner.assigned` tells the customer their driver is coming *and*
tells the driver they have a job. Both are the same fact and neither is the
other's copy — which is why a template returns a list.

---

## Templates

`domain/templates.py` is the whole service, and it is deliberately readable end
to end. Pure functions of a dict: no database, no session, no I/O, so a template
can be checked in a REPL — which is what you want from the code that decides
what everybody reads.

| Event | Customer | Partner |
| --- | --- | --- |
| `order.created` | in-app only | — |
| `order.confirmed` | in-app + push + email | — |
| `partner.offered` | — | in-app + push |
| `partner.assigned` | in-app + push — name, vehicle, phone | in-app + push |
| `order.picked_up` | in-app + push | — |
| `order.delivered` | in-app + push + **email receipt** | in-app |
| `dispatch.search_failed` | in-app + push, reason-specific | — |
| `order.cancelled` | in-app + push + email | in-app + push, only if assigned |

Absent on purpose:

- **`dispatch.search_started`** — the customer was told "finding you a partner"
  by `order.confirmed`, one second earlier.
- **`partner.rejected`** — a driver declining is not news to anybody. It is an
  analytics fact.
- **`partner.released`** — internal bookkeeping, invisible by design.

Three details worth pointing at:

**`order.created` gets no push.** The customer is looking at the screen that
just created it; a push would buzz a phone already in their hand. It is stored
anyway, so the feed has a complete history.

**`dispatch.search_failed` reads its `reason`.** "No delivery partners are on
duty near your pickup point" and "we asked 8 nearby partners and none were able
to take it" are different situations, and telling somebody the first when the
second happened is a small lie that produces a support ticket. This is what the
reason field on that event is *for*.

**`order.cancelled` reads `previous_status`.** Promising a refund on an order
that was never charged is worse than saying nothing, so the "any amount
authorised will be released" sentence only appears when there was one.

**Nothing here calls Order, Dispatch or User.** Every message is rendered from
the event payload alone — which is why those payloads carry `public_ref` and
city names rather than only ids. A template that needed a lookup would put
another service's uptime in the path of a push notification.

### Adding one

Add a function to `TEMPLATES` in `domain/templates.py`. That is the whole change:
the Kafka subscriptions are **derived from that registry**, so there is no
second list to forget. Two lists would be two places to edit, and the failure
mode of missing one is silent — a template nobody subscribes to renders nothing,
forever, with no error anywhere.

---

## Channels

One interface, three implementations, in `channels/`. The interface is the
deliverable.

| Channel | Today | Later |
| --- | --- | --- |
| `in_app` | **Real.** It *is* the stored row | unchanged |
| `push` | Logs `[push.send] to partner:3 \| …` | publishes a `push.send` job to RabbitMQ |
| `email` | Logs `[email.send] to customer:7 \| …` | publishes an `email.send` job to RabbitMQ |

When RabbitMQ lands, `EmailChannel.send` stops calling `logger.info` and starts
publishing. Nothing else changes: not the templates, not the consumer, not the
preference check, not the stored row. That is the entire reason for putting an
interface in front of two log lines.

**The log line prints an id, not an address, and that is the boundary rather
than a missing feature.** This service does not know anybody's email address or
device token, and should not learn one — a device token is a credential, and the
fewer places one lives the better. The worker resolves the recipient id, because
a worker that sends email has a reason to hold addresses.

**A channel never decides whether to send.** Preference and muting are resolved
before a channel is reached, so a channel that is called always sends. Splitting
that decision across three implementations is how one of them ends up honouring
a mute and the other two do not.

**A channel failure is swallowed and logged.** A dead email worker must not roll
back the consumer's transaction, because a redelivered Kafka message replays
*every* channel for that event — and the customer would get a second push
because an email bounced.

---

## Preferences

`notification_preferences`, keyed by `(recipient_type, recipient_id)`.

**A missing row means every default.** Rows are created on the first change,
never at registration, so a person who has never opened the settings screen has
no row and gets everything. The table holds decisions, not people.

Three gates, in order of authority:

1. The category is muted → only `in_app` survives.
2. The person switched the channel off.
3. The deployment switched the channel off (`EMAIL_ENABLED`, `PUSH_ENABLED`) —
   last, so a global switch cannot be mistaken for a personal preference.

`in_app` passes all three unconditionally. Turning it off would not mean "do not
tell me", it would mean "keep no record of what you told me", which nobody wants
and support cannot work without. A muted message is still stored, because "we
never told you" and "you asked us not to" are different things and somebody will
need to see which.

### The category that cannot be muted

```python
MUTABLE_CATEGORIES = {ORDER, PAYMENT, ACCOUNT}    # OFFER is absent
```

A driver who silenced job offers would be on duty and unreachable — worse for
them than for anyone. The API filters `offer` out of a mute request rather than
rejecting it: a 422 on a settings toggle helps nobody, and the server's list is
the authority.

There are four categories, not forty. A person muting something is making a
decision in one second from a settings screen; forty options is not a choice, it
is a form.

---

## Storage

**Title and body are rendered at write time and stored as text**, not assembled
from a template id when the feed is read. This is the important decision in the
schema and it is worth defending: a message is a record of what somebody was
actually told. Change a template next month and every historical notification
rendered lazily would silently change with it, so a customer and a support agent
looking at the same row would read different things.

The cost is that a typo cannot be retroactively fixed. That is the correct cost.

Every row also carries its provenance — `event_id`, `event_type`, `order_id`,
`correlation_id` — so "why did they get this?" is a lookup rather than a
reconstruction.

### Deduplication, twice

```sql
UNIQUE (event_id, recipient_type, recipient_id)
```

`processed_events` already stops a redelivery reaching the handler. This is the
second line, and it is here because the visible symptom of the first one failing
is somebody's phone buzzing twice.

Not `event_id` alone: `partner.assigned` legitimately produces two rows.

The write uses a **SAVEPOINT**, not a bare flush. One event's messages are all
written in the consumer's single transaction, so rolling the whole thing back on
a duplicate would throw away the siblings that succeeded — failing to re-notify
the driver would silently un-notify the customer too.

### The pair is never split

`recipient_type` + `recipient_id`, everywhere. Customer 5 and partner 5 are
different people, because each service numbers its own subjects from 1. The auth
dependency returns a `Recipient` named tuple rather than an int for exactly this
reason: the two can never disagree if they are never apart.

---

## The API

Two prefixes, same endpoints, because the gateway maps **one cookie per prefix**:

| Prefix | Cookie | Recipient |
| --- | --- | --- |
| `/api/notifications` | `lp_session` | customer |
| `/api/deliveries/notifications` | `lp_partner_session` | partner |

> The partner prefix nests under `/api/deliveries`, which the gateway also
> routes to orderServices. That works because the gateway matches the **longest**
> prefix first. It is load-bearing: delete the more specific entry from
> `SERVICE_ROUTES` and driver feed requests silently go to orderServices, which
> 404s them.

A single prefix would have to guess which cookie a browser holding both was
presenting, and guessing wrong shows one app the other's messages.

| Method | Path | What |
| --- | --- | --- |
| GET | `` | The feed, newest first, plus the badge count |
| GET | `/unread-count` | Just the badge. Polled |
| POST | `/{id}/read` | Mark one read |
| POST | `/read-all` | Clear the badge |
| GET | `/preferences` | Effective settings |
| PATCH | `/preferences` | Partial update |

Nothing takes a recipient id from the path or the body, so there is no endpoint
that can be walked. Every query is scoped by the pair in its `WHERE` clause.

**The count travels with the feed page.** Opening the feed and marking things
read are the two moments the badge changes; returning both in one call means the
app never briefly shows a stale number.

**`POST /{id}/read` answers 200 with `updated: 0`** for a message that is not
this person's or is already read — deliberately not a 404. The two cases are
indistinguishable to the caller and should be, and an app marking things read as
they scroll should not have to handle an error for a double tap.

**The badge count stops at `UNREAD_COUNT_CAP`.** `capped: true` means the real
number is higher; the app renders "99+". Counting the whole backlog of a
neglected account on every poll would be work done to be thrown away.

### Operations — `/internal/notifications/*`

| Method | Path | What |
| --- | --- | --- |
| GET | `/orders/{id}` | Everything anybody was told about one delivery |

Unscoped by recipient — both the customer's messages and the driver's — because
the question being asked is about the delivery rather than about a person. That
is exactly why it is not reachable from the gateway. When somebody says "I was
never told my order was cancelled", this is the call that settles it, including
which channels were attempted.

---

## Configuration

| Key | Default | What |
| --- | --- | --- |
| `NOTIFICATION_DB_NAME` | `notification_db` | Its own database |
| `INTERNAL_API_KEY` | *(empty)* | For `/internal/*` |
| `EMAIL_ENABLED` | `true` | The stub logs. On by default so the fan-out is visible |
| `PUSH_ENABLED` | `true` | Same |
| `UNREAD_COUNT_CAP` | `99` | Badge ceiling |
| `FEED_DEFAULT_LIMIT` | `30` | Page size |
| `FEED_MAX_LIMIT` | `100` | Ceiling on `?limit=` |

---

## Running it

```powershell
uvicorn src.services.notificationServices.main:app --port 8005 --reload
```

One background loop, not two: Notification is a **terminal consumer** — it reads
domain facts and produces messages, emits no events of its own, and therefore
has no outbox and no relay.

Migrations:

```powershell
alembic -c alembic_notification.ini upgrade head    # -> e2c81a4d6f30 (head)
```

Startup logs the registered templates, and `/health` reports `templates`,
`events_enabled`, `email_stubbed` and `push_stubbed` — so a deployment cannot
believe it is sending email when it is writing log lines.

---

## What is stubbed

**Email and push log instead of sending.** The interface, the preference check,
the storage and the fan-out are all real; only the last hop is a `logger.info`.
The `[email.send]` and `[push.send]` prefixes are the RabbitMQ queue names they
will become.

The one place the delay genuinely costs something is `partner.offered`: a driver
has thirty seconds to answer and currently learns about the job by polling
`GET /api/dispatch/offers/current`. Polling every two or three seconds is
correct, cheap and easy to replace; the contract does not change when push
lands.

**Not built:** device token registration, email address resolution, digests,
quiet hours, per-message delivery receipts, and the RabbitMQ workers themselves.

---

## Failure modes

**Nobody is getting anything.**
`GET /health` → `events_enabled`. Then check the consumer group's lag:

```powershell
docker exec logistic-kafka /opt/kafka/bin/kafka-consumer-groups.sh `
  --bootstrap-server localhost:9092 --describe --group notification-service
```

**Some events produce nothing.**
Expected for anything absent from `TEMPLATES` — most of the topic. Confirm with
the startup line listing the registered templates.

**A message went to the wrong person.**
Look for a query missing half the pair. Every read must filter on
`recipient_type` *and* `recipient_id`; filtering on the id alone shows customer
5 partner 5's messages.

**A duplicate reached somebody.**
Both defences failed, which should be impossible. Check `processed_events` for
the event id in `notification_db`, and whether
`uq_notifications_event_recipient` still exists on the table.

**The feed is empty after a `docker compose down -v`.**
Offsets were wiped, so the group re-reads from the beginning — but
`processed_events` still holds every marker, so nothing is written again. That
is correct behaviour: the messages are already in the table. An empty feed means
the table was dropped too.
