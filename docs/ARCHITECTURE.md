# Architecture & Getting Started

How the pieces fit together, and how to get the system running.

Component detail lives in [GATEWAY.md](GATEWAY.md),
[USER_SERVICE.md](USER_SERVICE.md), [PARTNER_SERVICE.md](PARTNER_SERVICE.md),
[ORDER_SERVICE.md](ORDER_SERVICE.md), [DISPATCH_SERVICE.md](DISPATCH_SERVICE.md),
[NOTIFICATION_SERVICE.md](NOTIFICATION_SERVICE.md) and [PRICING_SERVICE.md](PRICING_SERVICE.md); the event spine in
[EVENTS.md](EVENTS.md); schema changes in [MIGRATIONS.md](MIGRATIONS.md).

---

## The shape of it

```
                          ┌──────────────┐
        client ──────────▶│   Gateway    │  :8000  ← the only public port
                          │  src/gateway │
                          └───┬───┬───┬──┘
       /api/users            │   │   │            /api/dispatch
       /api/geo   ┌──────────┘   │   └──────────┐ /api/deliveries/notifications
                  ▼              ▼              ▼
          ┌──────────────┐ ┌───────────┐ ┌──────────────┐  ┌──────────────┐
          │ userServices │ │  partner  │ │    order     │  │   dispatch   │
          │    :8001     │ │  :8002    │ │    :8003     │  │    :8004     │
          └──────┬───────┘ └─────┬─────┘ └──────┬───────┘  └──────┬───────┘
                 │               │              │                 │
                 ▼               ▼              ▼                 ▼
           ┌──────────┐   ┌───────────┐  ┌───────────┐    ┌─────────────┐
           │ user_db  │   │partner_db │  │ order_db  │    │ dispatch_db │
           └──────────┘   └───────────┘  └───────────┘    └─────────────┘

                          ┌──────────────────┐   ┌──────────────────┐
                          │  notification    │   │ Mongo Atlas      │
                          │      :8005       │   │ common.sessions  │
                          └────────┬─────────┘   └──────────────────┘
                                   ▼              shared by every service
                          ┌──────────────────┐
                          │ notification_db  │
                          └──────────────────┘

  ══════════════════ the event spine ══════════════════════════════════════
        order ──outbox──▶ relay ─┐                 ┌─▶ order
                                 ├─▶ Kafka :9092 ──┼─▶ dispatch
     dispatch ──outbox──▶ relay ─┘  order-events   └─▶ notification
                                    3 partitions, keyed by order_id

  ─────────────── the one synchronous cross-service call ──────────────────
        dispatch ──HTTP──▶ partner  /internal/partners/available   (search)
        dispatch ──HTTP──▶ partner  /internal/partners/{id}/status (claim)
        order    ──HTTP──▶ user     /internal/users/{id}/addresses (snapshot)
        order    ──HTTP──▶ pricing  /internal/pricing/quotes       (quote)

  pricing :8006 → pricing_db. Public estimates at /api/pricing; no Mongo, no Kafka.
```

Six processes: the gateway and five services, plus one broker in Docker. Adding
a service means adding a process, one routing entry, and — if it consumes — a
consumer group.

**A fourth prefix, not a fourth process.** `/api/geo/*` — address search and
reverse lookup, the two endpoints every map in the frontend rests on — is routed
as its own prefix but answered by userServices, because it is stateless and owns
no tables. `GEO_SERVICE_URL` in `.env` is all that stands between that and a
process of its own. It is also the only prefix whose endpoints are public, which
[GEO.md](GEO.md) explains and bounds.

**Not everything goes through the gateway.** partnerServices also exposes
`/internal/partners/*` for Dispatch and an operations console. Those paths are
absent from `SERVICE_ROUTES`, so the gateway answers `404` for them and the
public internet cannot reach them at all — see
[PARTNER_SERVICE.md](PARTNER_SERVICE.md).

---

## How they connect

### Client → Gateway

Everything enters at **:8000**. Clients never call a service port directly.

### Gateway → Service

Plain HTTP, routed by path prefix:

```python
SERVICE_ROUTES = {
    "/api/users": settings.user_service_url,
    "/api/partners": settings.partner_service_url,
    "/api/geo": settings.geo_service_url,
    "/api/orders": settings.order_service_url,
    "/api/deliveries": settings.order_service_url,
    "/api/dispatch": settings.dispatch_service_url,
    "/api/notifications": settings.notification_service_url,
    "/api/deliveries/notifications": settings.notification_service_url,
    "/api/pricing": settings.pricing_service_url,
}
```

**Longest prefix wins,** and one entry depends on it:
`/api/deliveries/notifications` is more specific than `/api/deliveries`, so the
driver's feed reaches notificationServices while everything else under that
prefix reaches orderServices. It nests there rather than standing alone because
that is what makes the cookie right — this table maps one cookie per prefix, and
a driver's notifications must be authenticated with the driver's cookie.

`USER_SERVICE_URL` and `PARTNER_SERVICE_URL` in `.env` say where each service
listens. The gateway forwards the request unchanged and relays the response
verbatim — including error statuses like `409`.

It adds `X-Forwarded-For` so the service still sees the **real client IP**
rather than the gateway's. Sessions record that IP, so this is load-bearing, not
cosmetic.

### Service → Postgres (private)

Each service owns its own database and **no other service may touch it**.
userServices owns `user_db`; partnerServices owns `partner_db`. Cross-service
data access happens over HTTP, never by reaching into someone else's tables.

Dispatch needs to know which partners are free. It asks
`GET /internal/partners/available`, and never runs a query against `partner_db` —
that is the rule in its concrete form. It owns `dispatch_db`, which holds
attempts and offers: the record of *how* a partner was chosen, and no partners.

The database name is pinned in the service's own `config.py`; only host, port,
and credentials come from the shared `.env`. Each database also has its own
Alembic history — five databases, five histories:

| Database | Owner | Alembic history | Config |
| --- | --- | --- | --- |
| `user_db` | userServices | `migration/` | `alembic.ini` |
| `partner_db` | partnerServices | `migration_partner/` | `alembic_partner.ini` |
| `order_db` | orderServices | `migration_order/` | `alembic_order.ini` |
| `dispatch_db` | dispatchServices | `migration_dispatch/` | `alembic_dispatch.ini` |
| `notification_db` | notificationServices | `migration_notification/` | `alembic_notification.ini` |
| `pricing_db` | pricingServices | `migration_pricing/` | `alembic_pricing.ini` |
| `payment_db` | paymentServices | `migration_payment/` | `alembic_payment.ini` |

### Service → Mongo (shared)

Sessions are the deliberate exception — one collection, shared by everything, in
`src/database/session_store.py`.

The reason: any service must be able to check whether a session is valid, and
revoking a user everywhere must be a single write. If sessions lived in
userServices' Postgres, every other service would need either SQL access to a
database it doesn't own, or a network round-trip on every request.

**Rule of thumb:** business data is private to its service; session state is
shared infrastructure.

### Service → Kafka (shared)

The second piece of shared infrastructure, and the one that carries the
delivery. One topic, `order-events`, three partitions, every event keyed by
`order_id` so that all the facts about one delivery stay in order.

Nothing publishes to it directly. A service writes an event row to its **own**
outbox table in the same transaction as the state change, and a relay drains
that table onto the topic. Consumers deduplicate on `event_id` against their own
`processed_events` table. [EVENTS.md](EVENTS.md) is the whole story.

This is what lets Order confirm a delivery without knowing Dispatch exists, and
Dispatch assign a driver without knowing Order exists.

**Rule of thumb:** if a service needs to *ask* something, that is HTTP. If it
needs to *announce* something, that is an event.

**The cost of sharing it: `app_type`.** A session document's `user` field is a
bare integer, and each service numbers its own subjects from 1 — so user 5 and
partner 5 are different people with the same key. Every session records an
`app_type` (`1` users, `2` partners) and every bulk revoke filters on it.
Without that, a customer tapping "log out everywhere" signs out an unrelated
delivery partner mid-job. The same collision is why the two services set
differently-named session cookies.

---

## How authentication is split

Authentication happens **twice**, and that is deliberate — neither layer can do
the other's job.

```
   client ──Authorization: Bearer──▶ Gateway ────────▶ userServices
                                       │                    │
                        "is there a live session          "decrypt the token
                         for this token?"                  with this user's
                                       │                   token_secret"
                                       ▼                        ▼
                                Mongo sessions            Postgres users
                                  (shared)                  (private)
```

| | Gateway | Service |
| --- | --- | --- |
| Question | Is there a live session? | Is this token cryptographically valid for this user? |
| Reads | Mongo session store | Its own Postgres, then decrypts |
| Can decrypt? | **No** | Yes |
| Purpose | Reject anonymous traffic at the edge | The authoritative answer |

**Why the gateway can't finish the job.** Tokens are AES-encrypted with a
per-user `token_secret` that lives in userServices' private Postgres. The
gateway has no access to it — that boundary is what makes these separate
services. It authenticates against the shared session store instead, which is
enough to reject unauthenticated traffic but not enough to be final.

**Why the service checks again.** A service reachable directly — by a bug, a
misrouted prefix, or a future internal caller — must not be authenticated by a
header some proxy set. The gateway's `X-User-Id` saves a lookup; it is not
evidence. A direct request with a forged `X-User-Id` and no token gets `401`.

**Why not just fetch `token_secret` over HTTP?** It would work, and it is the
wrong trade. The secret currently exists in one table and one process; putting
it on the wire on every request multiplies where it can leak, and forces the
service to expose an endpoint that hands out key material — anything reaching
that endpoint could mint tokens for every user. If the gateway ever needs the
full check, the right shape is token *introspection*: ask the service for a
verdict, not for the key.

Public paths (`register`, `login`, `health`) skip both checks — you cannot hold
a token before you have one.

---

## Requirements

| | Notes |
| --- | --- |
| Python 3.13 | with the project's `venv` |
| PostgreSQL | this project uses the **18** cluster on port **5433** |
| MongoDB Atlas | cluster reachable, IP allowlisted |
| **Docker Desktop** | for the Kafka broker in `docker-compose.yml` — see [EVENTS.md](EVENTS.md) |
| `.env` | at the repo root, gitignored |

> Without Docker, set `KAFKA_ENABLED=false`. Every HTTP endpoint still works and
> events queue in each outbox, but nothing downstream of an event happens: no
> order is dispatched and no notification is written.

> Two Postgres clusters are installed on this machine — **17 on 5432** and
> **18 on 5433**. `DB_PORT=5433` targets 18. Pointing at the wrong one produces
> a password-authentication failure, not a missing-table error.

---

## First-time setup

```powershell
# 1. Activate the venv  (prompt should show (venv))
.\venv\Scripts\Activate.ps1
#    Blocked by execution policy? Allow it for this session only:
#    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#    In Git Bash instead:  source venv/Scripts/activate

# 2. Install dependencies
pip install -r requirement.txt
pip install -r requirements-dev.txt      # tests only

# 3. Create the databases (once)
#    psql is at C:\Program Files\PostgreSQL\18\bin\psql.exe
CREATE DATABASE user_db;
CREATE DATABASE partner_db;
CREATE DATABASE order_db;
CREATE DATABASE dispatch_db;
CREATE DATABASE notification_db;
CREATE DATABASE pricing_db;
CREATE DATABASE payment_db;

# 4. Apply migrations — one history per database, so five commands
alembic upgrade head
alembic -c alembic_partner.ini upgrade head
alembic -c alembic_order.ini upgrade head
alembic -c alembic_dispatch.ini upgrade head
alembic -c alembic_notification.ini upgrade head
alembic -c alembic_pricing.ini upgrade head
alembic -c alembic_payment.ini upgrade head

# 5. Confirm
alembic current                              # -> a1c4e07b92d3 (head)
alembic -c alembic_partner.ini current       # -> b1f4a72c9e01 (head)
alembic -c alembic_order.ini current         # -> a8d4e61f2c90 (head)
alembic -c alembic_dispatch.ini current      # -> d5b30f81ac47 (head)
alembic -c alembic_notification.ini current  # -> e2c81a4d6f30 (head)
alembic -c alembic_pricing.ini current       # -> f7a2c9d1b3e5 (head)

# 6. Start the broker — Kafka, one container, no Zookeeper
docker compose up -d
docker compose logs -f kafka                 # ~15s on a cold start
```

> **`-c alembic_partner.ini` is not optional.** Plain `alembic upgrade head`
> runs the *user* migrations. See [MIGRATIONS.md](MIGRATIONS.md).

`.env` must exist with these keys — see the tables in
[USER_SERVICE.md](USER_SERVICE.md) and [GATEWAY.md](GATEWAY.md):

```
DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
STATIC_SALT, STATIC_PEPPER, PASS_SALT_STATIC, SECRET_KEY
MONGO_URI, MONGO_DB_NAME
USER_SERVICE_URL, PARTNER_SERVICE_URL, ORDER_SERVICE_URL
DISPATCH_SERVICE_URL, NOTIFICATION_SERVICE_URL
KAFKA_BOOTSTRAP_SERVERS, KAFKA_ENABLED
INTERNAL_API_KEY
```

`.env.example` at the repo root lists all of them with notes.

---

## Starting the application

**The broker first, then six terminals** — all with the venv active, all from
the repo root.

```powershell
# Once, and it stays up between sessions
docker compose up -d
```

```powershell
# Terminal 1 — userServices  (also answers /api/geo)
uvicorn src.services.userServices.main:app --port 8001 --reload
```

```powershell
# Terminal 2 — partnerServices
uvicorn src.services.partnerServices.main:app --port 8002 --reload
```

```powershell
# Terminal 3 — orderServices        + outbox relay, + Kafka consumer
uvicorn src.services.orderServices.main:app --port 8003 --reload
```

```powershell
# Terminal 4 — dispatchServices     + outbox relay, + consumer, + offer worker
uvicorn src.services.dispatchServices.main:app --port 8004 --reload
```

```powershell
# Terminal 5 — notificationServices + Kafka consumer
uvicorn src.services.notificationServices.main:app --port 8005 --reload
```

```powershell
# Terminal 6 — pricingServices     (Order calls it on every new order)
uvicorn src.services.pricingServices.main:app --port 8006 --reload
```

```powershell
# Terminal 7 — paymentServices     + outbox relay, + Kafka consumer
uvicorn src.services.paymentServices.main:app --port 8007 --reload
```

```powershell
# Terminal 8 — the gateway
uvicorn src.gateway.main:app --port 8000 --reload
```

Order doesn't strictly matter — the gateway starts fine alone and returns `502`
for whichever service is down, and no service fails to start because the broker
is missing. That last part is deliberate: a broker outage must not stop the
platform taking orders. The outbox keeps queueing and the relay drains it when
Kafka returns.

> Six terminals is a lot. The three that matter for a delivery to complete
> end to end are **partner, order and dispatch** — user is only needed to sign
> in and snapshot an address, and notification only writes messages.

### Verify

```powershell
curl http://127.0.0.1:8000/health
```

```json
{"status":"ok","service":"gateway",
 "services":{"/api/users":{"status":"ok","service":"userServices"},
             "/api/partners":{"status":"ok","service":"partnerServices"},
             "/api/orders":{"status":"ok","service":"orderServices",
                            "payment_stubbed":true,"events_enabled":true},
             "/api/dispatch":{"status":"ok","service":"dispatchServices",
                              "events_enabled":true,"offer_ttl_seconds":30},
             "/api/notifications":{"status":"ok","service":"notificationServices",
                                   "events_enabled":true,"templates":8}}}
```

`"status":"degraded"` means the gateway is up but a service isn't.

### A real request

```powershell
curl -X POST http://127.0.0.1:8000/api/users/register `
  -H "Content-Type: application/json" `
  -d '{\"name\":\"nitish\",\"email\":\"aer@gmail.com\",\"password\":\"nit@123\",\"phone\":\"9853443879\"}'
```

Expect `201` with a user, an access token, and device identifiers.

### An authenticated request

Registering or logging in returns an `access_token`. Send it as a header —
query parameters are not accepted:

```powershell
curl.exe http://127.0.0.1:8000/api/users/profile -H "Authorization: Bearer <token>"
```

`401 Not authenticated` means no token, a bad token, or a revoked session.
`POST /api/users/logout` with the same header revokes it — the next request
with that token fails immediately.

Register and login also return a **refresh token**. When the access token
expires (60 minutes), exchange it rather than signing in again:

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/users/refresh -H "Content-Type: application/json" -d "{\"refresh_token\":\"<token>\"}"
```

Interactive docs — note these are **per service**, since the gateway has no
OpenAPI schema of its own:

- userServices — <http://127.0.0.1:8001/docs>
- partnerServices — <http://127.0.0.1:8002/docs>
- gateway — <http://127.0.0.1:8000/docs> (health only)

---

## Ports

| Port | What |
| --- | --- |
| 8000 | Gateway — the only one clients use |
| 8001 | userServices |
| 8002 | partnerServices (also serves `/internal/*`, not exposed by the gateway) |
| 8003 | orderServices (`/api/orders`, `/api/deliveries`, `/internal/orders/*`) |
| 8004 | dispatchServices (`/api/dispatch`, `/internal/dispatch/*`) |
| 8005 | notificationServices (`/api/notifications`, `/api/deliveries/notifications`) |
| 8006 | pricingServices (`/api/pricing` public, `/internal/pricing/*`) |
| 8007 | paymentServices (`/api/deliveries/payments` for drivers, `/internal/payments/*`) |
| 9092 | Kafka — in Docker, the HOST listener |
| 8080 | Kafka UI — optional, in Docker |
| 5433 | PostgreSQL 18 |
<!-- | 5432 | PostgreSQL 17 (not used by this project) | -->

---

## Common startup problems

**`Fatal error in launcher: Unable to create process using '...python.exe' '...uvicorn.exe'`**

The venv was created at one path and the project has since been **moved**. Read
the two paths in the message — the first is where the venv thinks its Python
lives, the second is where it actually is:

```
Fatal error in launcher: Unable to create process using
  '"C:\Users\...\Desktop\distributed_logistic_msrv\venv\Scripts\python.exe"          ← baked in
   "C:\Users\...\Desktop\distributed_logistic\distributed_logistic_msrv\venv\scripts\uvicorn.exe"'   ← actual
```

Every `.exe` in `venv\Scripts\Activate.ps1` is a small launcher stub with the absolute path
of its interpreter **compiled into it** at creation time. Move the venv and they
all break at once — `uvicorn`, `alembic`, `pip`, `pytest`, the lot. Activation
still appears to succeed, which is what makes this confusing: `(venv)` shows in
the prompt and `python` works fine.

Confirm it with:

```powershell
Get-Content venv\pyvenv.cfg    # the `command =` line records the original path
```

**Run right now** — invoke the module instead of the stub:

```powershell
python -m uvicorn src.gateway.main:app --port 8000 --reload
python -m alembic upgrade head
python -m pip install -r requirement.txt
```

This works because `venv\Scripts\python.exe` is a real copy of the interpreter,
not a launcher stub.

**Fix it properly** — recreate the venv so the stubs are rebuilt in place:

```powershell
deactivate
Remove-Item -Recurse -Force venv
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirement.txt
pip install -r requirements-dev.txt
```

Nothing is lost: the venv holds no project code, and `requirement.txt` is fully
pinned.

> Editing `pyvenv.cfg` does **not** help. Its `command =` line is only a record
> of how the venv was made; the broken path lives inside each `.exe`, so only
> recreating them fixes it.

**`404 Not Found` on a valid path**
Usually the wrong app. `src.main:app` is the legacy root app and has only
`/health`. Use `src.gateway.main:app` or the service directly.

**`Error loading ASGI app. Attribute "main:app" not found in module "src"`**
Dots between packages, one colon before the attribute:
`src.gateway.main:app`, not `src:main:app`.

**`[Errno 10048] only one usage of each socket address`**
The port is taken — often an old `--reload` process. Find and kill it:
```powershell
netstat -ano | Select-String ":8000" ; Stop-Process -Id <PID>
```

**`502 Upstream service unavailable`**
The gateway is up, the service isn't. Start it on the port in
`USER_SERVICE_URL`.

**`password authentication failed for user "postgres"`**
Wrong cluster or wrong password. Check `DB_PORT` — 5433 is 18, 5432 is 17.

**`RuntimeError: Could not connect to database after 5 attempts`**
Postgres unreachable at startup. The service retries 5 times, 2s apart, then
exits deliberately rather than serving broken requests.

**`ModuleNotFoundError: No module named 'src'`**
Run from the repo root.

**`ValidationError: field required`**
A key is missing from `.env`. The message names it.

**`401 Not authenticated` on an endpoint that should be public**
It is not in the gateway's `PUBLIC_PATHS`. Matching is exact, so a new public
endpoint has to be listed there.

**`503 Session store unavailable`**
The gateway could not reach Mongo. It fails closed on purpose — treating an
outage as "authenticated" would be a bypass.

**A route returns 404 that you know exists**
The service is running older code. `--reload` keeps serving the previous app if
the new one fails to import, so a syntax error mid-edit leaves a stale route
table. Check with `curl.exe http://127.0.0.1:8001/openapi.json`.

---

## Conventions

- **Absolute imports from `src.`** — `from src.services.userServices…`
- **Run everything from the repo root**
- **Never move the project folder with the venv inside it** — the launcher stubs
  in `venv\Scripts\` hardcode absolute paths. If you must move it, recreate the
  venv afterwards (see Common startup problems).
- **Schema changes go through Alembic**, never `create_all()`
- **A service never touches another service's database**
- **A service authenticates its own requests** — never trust a proxy header
- **Tokens travel in headers**, never query strings
- **Secrets live in `.env`**, which is gitignored and must never be committed

---

## Shared code

`src/` holds the few things that genuinely belong to everyone:

| Module | What | Why shared |
| --- | --- | --- |
| `config.py` | Mongo URI, service URLs, gateway timeout | Infrastructure, not business data |
| `database/mongo.py` | Mongo client, indexes | One pooled client per process |
| `database/session_store.py` | Session CRUD | Any service must be able to validate or revoke a session |
| `common/request_auth.py` | Token / user-id extraction | Gateway and services must accept identical forms, or a request passes one layer and fails the other |
| `common/events/` | Envelope, event types, outbox mixins, relay, consumer | A producer and a consumer that each keep their own copy agree right up until one is edited. This is the blueprint's `libs/contracts/` — see [EVENTS.md](EVENTS.md) |

Everything else belongs to a single service.

---

## Current state

**userServices** — register, login, profile, logout, log-out-everywhere, token
refresh, change password, forgot/reset password.

**partnerServices** — partner register/login/refresh/logout, profile, on-off
duty with the full gate sequence, location heartbeats, vehicles with
verification and a one-active-per-partner invariant, ratings, suspension, and
the `/internal` availability search Dispatch calls.

**orderServices** — the order state machine, address snapshotting from User,
binding quotes from Pricing, payment via paymentServices, the customer and partner endpoints, an outbox, and
consumers for `partner.assigned`, `dispatch.search_started` and
`dispatch.search_failed`. See [ORDER_SERVICE.md](ORDER_SERVICE.md).

**dispatchServices** — the offer loop: consumes `order.confirmed`, searches
partnerServices, offers to one driver at a time with a TTL, claims the winner
(409 resolves the race), emits `partner.assigned`, and releases the driver on
delivery or cancellation. See [DISPATCH_SERVICE.md](DISPATCH_SERVICE.md).

**notificationServices** — the message feed. Consumes eight event types, renders
per-recipient templates, stores them, honours per-category and per-channel
preferences, and fans out to in-app (real) plus email and push (logging stubs
where the RabbitMQ workers will go). See
[NOTIFICATION_SERVICE.md](NOTIFICATION_SERVICE.md).

**pricingServices** — a rate card per vehicle type, public fare estimates for
every vehicle, and binding stored quotes for Order, refusing parcels too heavy
for the vehicle. See [PRICING_SERVICE.md](PRICING_SERVICE.md).

**paymentServices** — pay online (simulated until a provider is connected) or
on delivery by cash or a UPI QR the driver shows; settles delivered orders on
`payment.captured`. See [PAYMENT_SERVICE.md](PAYMENT_SERVICE.md).

**The event spine** — Kafka in `docker-compose.yml`, one topic keyed by
`order_id`, the transactional outbox and its relay in every producer, and
`processed_events` deduplication in every consumer. See [EVENTS.md](EVENTS.md).

**Gateway** — routing to all five services with edge authentication, per-service
session cookies, health aggregation.

**Shared** — bcrypt hashing, Node-compatible encrypted tokens, Mongo sessions
with revocation and an `app_type` discriminator, the event contracts in
`src/common/events/`, five Alembic histories.

Not built yet: `PATCH /profile` for name and phone, email delivery for password
resets, refresh-token rotation, rate limiting, session listing, KYC document
upload, a real payment provider (online payments and verified UPI), RabbitMQ and the email/push workers,
live tracking, Audit and Analytics, tests.

Known leftovers: `src/main.py` and `src/database/connection.py` are the legacy
root app — redundant now, and the latter declares a second unused `Base`.
`models/session_model.py` is orphaned since sessions moved to Mongo, and
`create_tables()` in the service's `database/connection.py` would still rebuild
that dead table behind Alembic's back.
