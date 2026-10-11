# The Event Spine

Kafka, the outbox, and the rules every producer and consumer follows.

This is the phase the blueprint calls *the phase that matters*: once an order can
reach a driver with no HTTP between Order and Dispatch, every hard problem in the
architecture — dual writes, idempotency, partitioning, consumer groups — has been
solved once, in one place, for every service that comes after.

Companions: [DISPATCH_SERVICE.md](DISPATCH_SERVICE.md) and
[NOTIFICATION_SERVICE.md](NOTIFICATION_SERVICE.md) for the two services this
phase added, [PLATFORM_WORKFLOW.md](PLATFORM_WORKFLOW.md) for where each event
falls in a delivery.

---

## Contents

- [The shape of it](#the-shape-of-it)
- [Running the broker](#running-the-broker)
- [One topic, and why](#one-topic-and-why)
- [The envelope](#the-envelope)
- [The outbox, and the relay](#the-outbox-and-the-relay)
- [Consuming](#consuming)
- [The event catalogue](#the-event-catalogue)
- [Adding an event](#adding-an-event)
- [Debugging](#debugging)
- [Rules](#rules)

---

## The shape of it

```
   ┌─────────────────────────────────────────────────────────────────┐
   │  one business transaction                                       │
   │    UPDATE orders SET status='confirmed'                         │
   │    INSERT INTO order_status_history ...                         │
   │    INSERT INTO outbox (event_id, payload) ...   ← same COMMIT   │
   └───────────────────────────┬─────────────────────────────────────┘
                               │
                    relay (background task, 1s poll)
                               │  key = order_id
                               ▼
              ╔════════════════════════════════════╗
              ║  Kafka topic: order-events          ║
              ║  3 partitions, keyed by order_id    ║
              ╚═══┬═══════════════┬═══════════════┬═╝
                  │               │               │
        group:    │     group:    │     group:    │
        order-    │   dispatch-   │  notification-│
        service   │    service    │    service    │
                  ▼               ▼               ▼
            ┌──────────┐   ┌───────────┐   ┌──────────────┐
            │  Order   │   │ Dispatch  │   │ Notification │
            └──────────┘   └───────────┘   └──────────────┘
              consumes:      consumes:        consumes:
              partner.       order.           order.*
                assigned       confirmed      partner.offered
              dispatch.*     order.cancelled  partner.assigned
                             order.delivered  dispatch.search_failed
```

Three consumer groups on one topic. Each reads every message and acts on the few
it has handlers for; an unmatched event is skipped, not an error.

Everything lives in `src/common/events/`:

| Module | What |
| --- | --- |
| `types.py` | Every event type, the topic, the consumer group ids |
| `envelope.py` | The shape of every message |
| `models.py` | `OutboxMixin` and `ProcessedEventMixin` |
| `publisher.py` | Emitting an event == writing an outbox row |
| `relay.py` | Outbox row → Kafka. The only thing that produces |
| `consumer.py` | Kafka → handler, with deduplication and a DLQ |
| `runtime.py` | Starting and stopping the loops in a lifespan |
| `config.py` | Broker settings, from the root `.env` |

This is the blueprint's `libs/contracts/`, inside `src/common` because this repo
is one deployable tree rather than a set of published packages. The rule it
enforces is the same: **the envelope, the topic and the vocabulary are defined
once.** A producer and a consumer that each keep a copy agree right up until one
of them is edited.

---

## Running the broker

**Docker is required and is not currently installed on this machine.** Install
Docker Desktop for Windows first; everything below assumes it.

```powershell
# From the repo root. One broker, no Zookeeper — Kafka 3.9 in KRaft mode is its
# own metadata quorum.
docker compose up -d

# Watch it come up (~15s on a cold start)
docker compose logs -f kafka

# What exists
docker exec logistic-kafka /opt/kafka/bin/kafka-topics.sh `
  --bootstrap-server localhost:9092 --list
```

Expect `order-events` and `order-events.dlq`. Both are created by the
`kafka-init` container, which runs once and exits — `docker compose ps` showing
it as `Exited (0)` is success, not a failure.

There is also a UI at <http://localhost:8080> — topics, partitions, consumer
group lag, and the messages themselves. Nothing depends on it; comment the
`kafka-ui` service out if you would rather not run a second container.

```powershell
docker compose down       # stop, keep the log
docker compose down -v    # stop and wipe it, offsets included
```

> `docker compose down -v` resets consumer offsets as well as messages. With
> `CONSUMER_AUTO_OFFSET_RESET=earliest` the next start of each service re-reads
> the whole topic from the beginning — harmless, because every handler is
> idempotent and `processed_events` still holds the markers.

### Without a broker

`KAFKA_ENABLED=false` in `.env` starts every service with no relay and no
consumers. HTTP endpoints work; events queue in each outbox and are delivered
whenever the broker comes back.

What stops working, and it is most of the flow: no order is dispatched, no
notification is written, no driver is released. The two internal endpoints kept
for exactly this — `POST /internal/orders/{id}/assignment` and
`POST /internal/dispatch/orders/{id}/cancel` — let you drive the state machine
by hand.

---

## One topic, and why

`order-events`, three partitions, **keyed by `order_id` on every event from
every producer**.

The blueprint suggested a topic per producer, which is the conventional answer.
This deviates, and the reason is ordering:

> Kafka guarantees ordering only *within one partition of one topic*.

Notification consumes `order.picked_up` (from Order) and `partner.assigned`
(from Dispatch). On separate topics there is no ordering between them at all, so
a customer can be told their driver arrived before being told one was found.
One topic keyed by order id puts every fact about one delivery on one partition,
in the order it happened.

The cost is that every consumer group reads every event and discards most of
them. At this volume that is free. The ordering guarantee is not.

**Three partitions** means three orders can be processed concurrently while each
individual order stays strictly ordered. Raise it when a single consumer cannot
keep up; a consumer group can never have more useful members than the topic has
partitions.

---

## The envelope

Every message, from every producer:

```json
{
  "event_id":       "3f2b8c14-...",
  "event_type":     "partner.assigned",
  "event_version":  1,
  "occurred_at":    "2026-09-02T11:04:23.518Z",
  "correlation_id": "req-9f3c...",
  "producer":       "dispatch",
  "aggregate_id":   "42",
  "data":           { "...": "..." }
}
```

| Field | Why it exists |
| --- | --- |
| `event_id` | What consumers deduplicate on. Generated once, at publish, never reused |
| `event_type` | The handler key. Past tense, always |
| `event_version` | Bumped only when a field is **removed or retyped**. Adding an optional field is safe |
| `occurred_at` | When the fact happened, not when it was delivered |
| `correlation_id` | The gateway's request id, carried the whole way. One delivery touches six services; without it, debugging is guesswork |
| `producer` | Which service emitted it. Since all four share a topic, this is how you tell at a glance |
| `aggregate_id` | The order id. **Also the partition key** — this field is the ordering guarantee |
| `data` | The payload. What consumers need, never the whole row |

**Money and timestamps cross the wire as strings.** `json_safe` in
`envelope.py` turns `Decimal("149.00")` into `"149.00"`, not `149.0`. A consumer
that parses the string into its own Decimal keeps the cents; one that reads a
float has already lost them.

**Payloads carry what consumers need, not the whole row.** `order.confirmed`
carries coordinates because Dispatch matches on them, and does not carry the
customer's phone number, because every consumer group reads it — Analytics and
Audit included — and none of them have business with it. That judgement lives in
each service's `events/payloads.py` so it is reviewable in one place.

---

## The outbox, and the relay

A service commits to Postgres and publishes to Kafka. Two systems, no shared
transaction. Publishing directly leaves a window where the state change exists
and nobody was told: nothing retries, nothing alerts, and a customer waits for a
driver who was never requested.

So:

```python
# in the business method — no broker, no network, no failure mode
publisher.publish(db, ORDER_CONFIRMED, str(order.id), payloads.order_confirmed(order))
...
db.commit()      # the order, its history row, and the event. One transaction.
```

`publish()` adds a row and returns nothing. There is deliberately no "did it
send?" to branch on — a caller that could see a send failure would be tempted to
treat it as a reason not to commit the order.

The relay does the rest, and has exactly three properties:

- **Nothing is lost.** A row stays unpublished until the broker acknowledges it.
- **Order is preserved per aggregate.** Rows go out in `id` order and the batch
  *stops* at the first failure. Skipping a failed row to make progress on later
  ones would deliver `order.delivered` before `order.picked_up`.
- **Duplicates are possible, and accepted.** Broker acknowledges, process dies,
  `published_at` never written, row sent again on restart. Closing that window
  needs a transaction across Postgres and Kafka, which is the thing the pattern
  exists to avoid.

A row that keeps failing stops being retried after `OUTBOX_MAX_ATTEMPTS` and
stays in the table with `last_error` set. It is never deleted — that table is
where you look when somebody says nothing happened.

| Service | Has an outbox | Because |
| --- | --- | --- |
| orderServices | yes | produces `order.*` |
| dispatchServices | yes | produces `partner.*` and `dispatch.*` |
| notificationServices | **no** | terminal consumer — reads facts, produces messages, emits nothing |

---

## Consuming

```python
consumer = EventConsumer(
    group_id=GROUP_DISPATCH,
    session_factory=SessionLocal,
    processed_model=ProcessedEvent,
    service_name="dispatch",
)
consumer.on(ORDER_CONFIRMED, handle_order_confirmed)
```

A handler is a plain function of `(db, envelope)`. Two rules, and both are
enforced by the consumer rather than by remembering:

**1. A handler does not commit.** The consumer commits the handler's work *and*
the `processed_events` row in one transaction. Commit them separately and a
crash in between leaves the work done and the marker missing — so the
redelivery does it twice.

**2. Offsets are committed after the handler returns, never before.**
Auto-commit is off. It acknowledges a message when it is *fetched*, so a crash
mid-handler loses it — the exact failure the outbox prevents on the way out,
reintroduced on the way in.

### Deduplication

```
message arrives ──▶ processed_events has this event_id?
                        │yes                    │no
                        ▼                       ▼
                    skip, commit offset    handler(db, envelope)
                                           INSERT processed_events
                                           COMMIT  ← one transaction
```

Delivery is at-least-once, so duplicates are certain rather than hypothetical.
Rows in `processed_events` are never expired: a pruning job that deletes
anything the broker could still redeliver reintroduces the duplicate the table
was built to stop.

Two services add a **second** line of defence, because for them a duplicate is
especially visible:

- `dispatch_attempts.order_id` is unique — a redelivered `order.confirmed`
  cannot start a second search.
- `notifications (event_id, recipient_type, recipient_id)` is unique — a
  redelivery cannot buzz somebody's phone twice. Not `event_id` alone:
  `partner.assigned` legitimately produces two messages, one to the customer and
  one to the driver.

### When a handler fails

Three attempts with backoff, then the envelope goes to `order-events.dlq` with
the error attached and the offset is committed.

Retrying forever is the obvious behaviour and the wrong one: every order behind
the broken one on that partition would be stuck behind it. Nothing drains the
DLQ automatically — it is a place to look, not a queue.

Order wraps its handlers so that two domain errors are **not** failures:
`OrderNotFoundError` and `InvalidTransitionError` are logged and skipped. An
event for an order that was cancelled while the event was in flight is not a
bug, and parking it in the DLQ would make it look like one.

---

## The event catalogue

| Event | Producer | Consumed by | Carries |
| --- | --- | --- | --- |
| `order.created` | order | notification | route, weight, vehicle type, distance |
| `order.confirmed` | order | **dispatch**, notification | coordinates, weight, vehicle type, fare |
| `dispatch.search_started` | dispatch | order | attempt id, radius, deadline |
| `partner.offered` | dispatch | notification | partner id, rank, distance, fare, `expires_at` |
| `partner.rejected` | dispatch | *(analytics)* | partner id, rank, `reason`: declined / expired / lost_race |
| `partner.assigned` | dispatch | order, notification | partner name, phone, vehicle — everything Order snapshots |
| `dispatch.search_failed` | dispatch | order, notification | `reason`: no_candidates / all_declined / deadline_exceeded |
| `order.picked_up` | order | notification | partner id, timestamp |
| `order.delivered` | order | **dispatch**, notification | amount, distance — Dispatch releases the driver |
| `order.cancelled` | order | **dispatch**, notification | `previous_status`, reason |
| `partner.released` | dispatch | *(audit)* | partner id, reason |

`order.confirmed` and `partner.assigned` are the two that carry the architecture.
The first turns "someone wants a delivery" into "find them a driver", and Order
emits it without knowing Dispatch exists. The second turns "we found one" into
an order the customer can see, and Dispatch emits it without knowing Order
exists.

Events with no consumer today — `partner.rejected`, `partner.released` — are not
waste. They are what an Analytics or Audit service is added on top of, on any
afternoon, with no change to any producer. That is the point of having built the
log.

---

## Adding an event

1. **`src/common/events/types.py`** — add the constant and put it in
   `ALL_EVENT_TYPES`. Both `publish()` and `consumer.on()` reject a string that
   is not there, so a typo is an error at startup rather than a topic nobody
   subscribes to.
2. **The producer's `events/payloads.py`** — a function returning the payload.
   Decide there what consumers actually need; that file is where the judgement
   is reviewed.
3. **The producer's business method** — `publisher.publish(...)`, before the
   commit that carries the state change.
4. **Each consumer** — a handler, and `consumer.on(...)` in `build_consumer()`.
   For Notification, add a template to `TEMPLATES` and the subscription follows
   automatically: it is derived from the registry, so there is no second list to
   forget.

**Past tense, always.** `order.confirmed`, never `confirm_order`. A command on
the log names who must act, which is exactly the coupling the log removes.

---

## Debugging

**Is anything stuck in an outbox?**

```sql
-- against order_db, dispatch_db
SELECT id, event_type, aggregate_id, attempts, last_error, occurred_at
FROM outbox WHERE published_at IS NULL ORDER BY id;
```

Rows with `attempts = 0` mean the relay is not running — check `KAFKA_ENABLED`
and the service log for `[order relay] producing to order-events`. Rows with a
climbing `attempts` and a `last_error` mean the broker is refusing them.

**Did a consumer see it?**

```sql
-- against dispatch_db, notification_db, order_db
SELECT event_type, count(*) FROM processed_events GROUP BY 1 ORDER BY 1;
```

**What is actually on the topic?**

```powershell
docker exec logistic-kafka /opt/kafka/bin/kafka-console-consumer.sh `
  --bootstrap-server localhost:9092 --topic order-events --from-beginning
```

**Is a group falling behind?**

```powershell
docker exec logistic-kafka /opt/kafka/bin/kafka-consumer-groups.sh `
  --bootstrap-server localhost:9092 --describe --group dispatch-service
```

`LAG` climbing means the consumer is slower than the producer. `LAG` frozen with
no consumer listed means the service is down.

**Anything in the DLQ?**

```powershell
docker exec logistic-kafka /opt/kafka/bin/kafka-console-consumer.sh `
  --bootstrap-server localhost:9092 --topic order-events.dlq --from-beginning
```

Each record carries `failed_by`, `consumer_group`, `error`, and the original
`envelope` nested whole — so a replay is a straight re-publish of that field.

**Follow one delivery across every service:** grep the logs for its
`correlation_id`. That is the entire reason it is carried.

---

## Rules

- **Every event is written through an outbox,** in the same transaction as the
  state change it describes.
- **Every event carries `event_id` and `occurred_at`; every consumer stores
  processed ids.** At-least-once delivery makes duplicates certain.
- **Partition by `order_id`.** Ordering is only guaranteed within a partition.
- **A handler never commits.** The consumer commits its work and its marker
  together.
- **A handler is safe to run twice** even so. The marker closes the window; a
  state check closes the rest, because a retry after a long outage can deliver
  an event about an order that has since moved on.
- **Past tense for event names, always.**
- **A correlation id is generated at the gateway** and carried through every
  HTTP header and event envelope.
- **A consumer that fails three times parks the message and moves on.** A
  partition blocked on one order is an outage for every order behind it.
