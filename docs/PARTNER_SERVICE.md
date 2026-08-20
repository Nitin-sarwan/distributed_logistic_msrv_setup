# Partner Service

`src/services/partnerServices/` — owns the delivery partners: who they are,
what they drive, whether they are cleared to work, and whether they are
available right now.

It is **self-contained**: its own config, its own database (`partner_db`), its
own FastAPI app, its own Alembic history. It shares only the session store
(Mongo) with the rest of the system.

---

## The line between this service and Dispatch

This is the distinction the whole design rests on:

```
Partner Service          "Who are the delivery partners?"
Dispatch Service         "Which partner should get this order?"
```

Partner Service **reports**. It answers, honestly and with everything needed to
judge: here are the partners who are verified, online, driving a cleared
vehicle, sending fresh location heartbeats, and within N km — with the distance
and the vehicle attached. Dispatch **decides**. It may weigh distance against
rating, against how many orders a partner has already refused today, against
surge zones — none of which is this service's business.

The seam is `GET /internal/partners/available`. What sits on either side of it:

| | Partner Service | Dispatch Service |
| --- | --- | --- |
| Owns | partner + vehicle rows | assignments |
| Answers | who *could* take an order | who *does* |
| Writes on assignment | `status = on_trip`, when told | the assignment itself |

Dispatch never reads `partner_db`. It asks over HTTP, the same way anything else
in this system crosses a service boundary.

```
Order
  │
  ▼
Dispatch ──── GET /internal/partners/available?lat=&lng= ────▶ Partner Service
  │                                                                  │
  │◀──────── [ {partner, vehicle, distance_km}, ... ] ───────────────┘
  │
  ├─ picks one
  │
  └─ PATCH /internal/partners/{id}/status {"status": "on_trip"}  ───▶ Partner Service
```

---

## Layout

```
src/services/partnerServices/
  main.py                      # FastAPI app + lifespan
  config.py                    # service settings, owns the DB name
  api/
    routes.py                  # partner-facing HTTP endpoints
    internal_routes.py         # service-to-service + operations
    schema.py                  # Pydantic request/response models
    dependencies.py            # get_current_partner, require_internal_key
  services/
    partner_services.py        # business rules for partners
    vehicle_services.py        # business rules for vehicles
  repositories/
    partner_repositories.py    # queries, including the availability search
    vehicle_repositories.py
  models/
    partner_model.py           # SQLAlchemy ORM tables
    vehicle_model.py
  database/
    base.py                    # DeclarativeBase — its own, not userServices'
    connection.py              # engine, SessionLocal, get_db
  utils/
    enums.py                   # the closed vocabularies
    security.py                # hashing + token cipher
    exceptions.py              # domain errors
```

### The layers

Same four as userServices, each talking only to the one below it:

| Layer | Responsibility | Must not |
| --- | --- | --- |
| `api/` | HTTP in/out, map errors to status codes | contain business rules or SQL |
| `services/` | business rules, orchestration | run queries directly |
| `repositories/` | database queries | make decisions |
| `models/` | table definitions | contain logic |

> **On `controllers/`.** The tree you sketched had one. This service does not,
> matching userServices — where `controllers/user_controller.py` was created,
> left empty, and noted in [USER_SERVICE.md](USER_SERVICE.md) as a layer the
> service layer already covers. `api/routes.py` *is* the controller: it takes
> HTTP in, calls one service method, maps domain errors to status codes, and
> returns. A fifth layer between it and `services/` would forward arguments and
> nothing else.

---

## The tables

### `partners`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | integer | PK |
| `name` | varchar(100) | |
| `phone` | varchar(10) | **unique, indexed — the login identity** |
| `email` | varchar(255) | unique, **nullable** |
| `password_hash` | text | bcrypt, never plaintext |
| `token_secret` | text | per-partner token key, 64 hex chars |
| `status` | varchar(20) | `offline` / `online` / `on_trip` / `suspended` |
| `is_verified` | boolean | KYC cleared — the gate on receiving work |
| `current_latitude` | numeric(9,6) | nullable |
| `current_longitude` | numeric(9,6) | nullable |
| `location_updated_at` | timestamptz | nullable — what makes the pin trustworthy |
| `rating` | numeric(2,1) | running average, default 5.0 |
| `rating_count` | integer | sample count behind the average |
| `is_deleted` | boolean | soft delete |
| `created_at` / `updated_at` | timestamptz | |

Database: **`partner_db`** — pinned in `config.py`, not `.env`, because it
belongs to this service alone.

**Phone is the login identity, not email.** A driver signs in with the number
their SIM already has; many will never provide an email, which is why it is
nullable here and required in `users`.

**Three columns beyond the obvious profile, each load-bearing:**

- `token_secret` — access tokens are AES-encrypted with it, so rotating one row
  invalidates only that partner's tokens, and a stolen `partners` table without
  `.env` cannot be used to mint any.
- `rating_count` — the running average cannot be updated without it. A new score
  has to be weighted against how many came before, and there is no ratings table
  to recompute from. It is also the honest number: 5.0 over two trips and 5.0
  over two thousand are not the same claim.
- `location_updated_at` — coordinates with no timestamp cannot be told apart
  from coordinates recorded last Tuesday. Without it Dispatch routes orders to a
  phone that has been in a tunnel for an hour.

Constraints live in the database as well as in Pydantic: `CHECK` on `status`, on
`rating` between 0 and 5, and on both coordinate ranges. Validation at the edge
protects the API; the constraint protects the table from a migration, a
backfill, or a `psql` session that never goes through the API at all.

### `vehicles`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | integer | PK |
| `partner_id` | integer | FK → `partners.id`, indexed |
| `vehicle_type` | varchar(20) | `two_wheeler` / `three_wheeler` / `mini_truck` / `truck` |
| `vehicle_number` | varchar(20) | **unique**, normalised uppercase alphanumeric |
| `model_name` | varchar(100) | nullable |
| `capacity` | numeric(8,2) | kilograms |
| `status` | varchar(20) | `pending` / `inactive` / `active` / `rejected` |
| `created_at` / `updated_at` | timestamptz | |

A separate table rather than columns on `partners` because the relationship is
genuinely one-to-many over time — a partner replaces a bike with a van, or keeps
both and switches by season. Flattening it loses the old vehicle's record every
time one changes, including which vehicle carried a past delivery.

**`vehicle_number` is normalised on the way in.** `"DL 01 AB 1234"`,
`"dl-01-ab-1234"` and `"DL01AB1234"` are one vehicle. Without that, the unique
constraint is defeated by a space and the same van gets registered to two
partners. Unique table-wide, not per partner: a plate identifies one physical
vehicle in the world.

---

## Two status columns, and why neither is a boolean

### `partners.status`

```
offline ⇄ online → on_trip → online
                     ↑
              suspended (operations only)
```

| Value | Meaning | Who may set it |
| --- | --- | --- |
| `offline` | not working | the partner |
| `online` | available for orders | the partner |
| `on_trip` | mid-delivery | **Dispatch** |
| `suspended` | barred | **operations** |

A partner may only move themselves between `offline` and `online`. The other two
are states somebody else put them in:

- Clearing `on_trip` from the driver's phone would strand a customer with a
  delivery that no longer belongs to anybody.
- A suspension a partner can lift is not a suspension.

The schema layer rejects the disallowed values (so the rule shows up in the
OpenAPI schema) and `PartnerService.set_status` rejects them again.

### `vehicles.status`

This one replaces the pair of booleans it is tempting to write instead —
`is_verified` + `is_active`. Two booleans admit four combinations and only three
are meaningful: *active but unverified* is a vehicle taking orders without its
papers checked, which is the exact state verification exists to prevent. One
column cannot express it.

| Value | Meaning |
| --- | --- |
| `pending` | registered, documents not yet checked — unusable |
| `inactive` | cleared, but parked |
| `active` | cleared and being driven — **at most one per partner** |
| `rejected` | documents refused |

Only `active` makes a partner available, and `active` is unreachable without
passing verification first.

**"One active vehicle per partner" is enforced by the database**, not by the
service remembering to stand the old one down:

```python
Index("uq_vehicles_one_active_per_partner", "partner_id",
      unique=True, postgresql_where=text("status = 'active'"))
```

A partial unique index constrains only the active rows, so a partner may own any
number of parked vehicles. It is also why `VehicleRepository.set_active()`
clears and sets inside one transaction — two statements that are each legal
alone are not legal in between.

---

## Going online: the gate sequence

A partner cannot receive work until four things are true, and each refusal is
returned at the moment the partner tries rather than by quietly filtering them
out of the Dispatch query. A driver sitting at a green "online" screen receiving
nothing has no way to find out why.

```
register                    →  status=offline, is_verified=false
POST /internal/…/verification  →  is_verified=true          (operations)
POST /me/vehicles           →  vehicle status=pending
POST /internal/…/vehicles/{id}/verification  →  inactive    (operations)
POST /me/vehicles/{id}/activate  →  active
PATCH /me/status {"online"}  →  online
POST /me/location            →  now visible to Dispatch
```

Skip a step and you get a specific error:

| Attempt | Response |
| --- | --- |
| go online while unverified | `403 Partner is not verified yet` |
| go online with no active vehicle | `409 Set an active verified vehicle before going online` |
| activate a `pending` vehicle | `409 This vehicle has not been verified yet` |
| go online while suspended | `409 This account is suspended. Contact support.` |
| go offline mid-delivery | `409 Finish or hand back the current delivery first` |

The invariant holds from the other direction too. Removing or de-verifying the
active vehicle, or withdrawing a partner's KYC, sends them back to `offline` in
the same operation — never leaving them showing `online` with nothing Dispatch
can match.

---

## Endpoints

Partner-facing paths are relative to the gateway, e.g.
`http://127.0.0.1:8000/api/partners/login`.

### Authentication

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| `POST` | `/api/partners/register` | public | `201`, signed in immediately |
| `POST` | `/api/partners/login` | public | `200`, phone + password |
| `POST` | `/api/partners/refresh` | public | new access token |
| `POST` | `/api/partners/logout` | partner | revokes this session |
| `POST` | `/api/partners/logout-all` | partner | every device, rotates `token_secret` |
| `POST` | `/api/partners/change-password` | partner | requires the current password |

```json
POST /api/partners/register
{"name": "Ravi Kumar", "phone": "9876543210", "password": "drive@1234"}
```

`email` is optional. `password` is at least 8 characters. `409` on a duplicate
phone or email; `422` on a phone that is not exactly 10 digits.

The response is an `AuthResponse`: the partner, an access token, a refresh
token, both expiries, and device identifiers — the same shape login returns.
Registering signs the partner in, but they are `offline` and unverified, so they
receive nothing until operations clears them.

`401 Invalid phone or password` covers both a wrong password and an unknown
number, deliberately identically — distinguishing them would let someone
enumerate which drivers are registered.

### Profile and availability

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/partners/me` | the partner's own record |
| `PATCH` | `/api/partners/me` | `name`, `email` only |
| `PATCH` | `/api/partners/me/status` | `offline` / `online` |
| `POST` | `/api/partners/me/location` | the heartbeat |

**No endpoint takes a partner id.** Identity comes from `get_current_partner`
and every query is scoped by it, so one partner cannot read or change another's
data whatever they put in the URL.

`PATCH /me` deliberately excludes `phone` — changing the login identity needs
fresh proof of ownership of the new number, and there is no OTP flow yet — as
well as `status`, `is_verified` and `rating`, which have their own routes or
belong to somebody else.

```json
POST /api/partners/me/location
{"latitude": 28.6330, "longitude": 77.2190}
```

Called on a timer while the partner app is open. Accepted in every status,
including `offline`: the timestamp is what Dispatch trusts, and a partner who
has just gone online should be routable immediately rather than after their next
heartbeat.

### Vehicles

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/partners/me/vehicles` | active first, then stable by id |
| `POST` | `/api/partners/me/vehicles` | `201`, arrives `pending` |
| `PATCH` | `/api/partners/me/vehicles/{id}` | `capacity`, `model_name` only |
| `POST` | `/api/partners/me/vehicles/{id}/activate` | stands down the previous one |
| `DELETE` | `/api/partners/me/vehicles/{id}` | `204` |

```json
POST /api/partners/me/vehicles
{"vehicle_type": "two_wheeler", "vehicle_number": "DL 01 AB 1234",
 "capacity": 25, "model_name": "Honda Activa"}
```

`capacity` is checked against the class of vehicle — a two-wheeler declared at
900kg is a `422`, because otherwise it would win every Dispatch query for heavy
loads. The ceilings live in `utils/enums.py`:

| Type | Max kg |
| --- | --- |
| `two_wheeler` | 30 |
| `three_wheeler` | 500 |
| `mini_truck` | 1500 |
| `truck` | 10000 |

`PATCH` cannot change `vehicle_type` or `vehicle_number`: both are verified
facts about a physical vehicle, and editing them after approval would carry that
approval over to a different one. Adding a second vehicle is the supported path.

A vehicle that does not exist and one belonging to another partner both return
`404` — a `403` would confirm the id is real, which the caller has no business
learning.

### Internal — `/internal/partners/*`

**Not under `/api`, and that is the access control.** The gateway routes only
the prefixes it knows about (`/api/users`, `/api/partners`); anything else is a
`404` at the edge and never reaches a service. Callers inside the deployment
reach these directly on port 8002.

`X-Internal-Key` is the second lock, for anything that can reach the service
port directly. It is compared in constant time, and an empty `INTERNAL_API_KEY`
disables the check — logged as a warning at startup, and acceptable on a local
machine only.

| Method | Path | Caller |
| --- | --- | --- |
| `GET` | `/internal/partners/available` | Dispatch |
| `GET` | `/internal/partners/{id}` | Order |
| `PATCH` | `/internal/partners/{id}/status` | Dispatch |
| `POST` | `/internal/partners/{id}/rating` | Order, after delivery |
| `POST` | `/internal/partners/{id}/verification` | operations |
| `POST` | `/internal/partners/{id}/suspension` | operations |
| `POST` | `/internal/partners/vehicles/{id}/verification` | operations |

```
GET /internal/partners/available?lat=28.6315&lng=77.2167
                                &radius_km=5&vehicle_type=two_wheeler
                                &min_capacity=20&limit=20
```

```json
[{"partner": {"id": 1, "name": "Ravi Kumar", "phone": "9876543210",
              "status": "online", "is_verified": true,
              "rating": 4.8, "rating_count": 214},
  "vehicle": {"id": 3, "vehicle_type": "two_wheeler",
              "vehicle_number": "DL01AB1234", "capacity": 25.0,
              "status": "active"},
  "distance_km": 0.207}]
```

An empty list is a valid answer and a common one at 4am. It means "nobody", not
"error", so it is a `200`.

`distance_km` is returned rather than only the ordering, because Dispatch's
ranking is its own business — it may weigh distance against rating, load
balancing, or recent refusals, and it should not have to ask twice to do that.

The partner shape here is narrower than what the partner sees about themselves:
no email, no live coordinates. That is a property of the response model, not
something the caller has to remember to filter.

```json
PATCH /internal/partners/{id}/status
{"status": "on_trip"}
```

`online → on_trip` is an assignment, `on_trip → online` a release. **Any other
transition is `409`**, and that refusal is the safety property that matters: it
is what stops two concurrent dispatches from both believing they claimed the
same partner. Setting the status a partner already holds succeeds and changes
nothing, so retrying after a network timeout is safe.

---

## The availability query

`PartnerRepository.find_available` is the one interesting piece of SQL in the
service. Six conditions have to hold at once, and each exists because dropping
it produces a specific bad assignment:

| Condition | What goes wrong without it |
| --- | --- |
| `status = 'online'` | claiming a partner who is mid-delivery, or suspended |
| `is_verified` | routing an order to someone whose KYC never cleared |
| joined vehicle at `active` | sending a 400kg load to someone on a scooter |
| `location_updated_at` fresh | routing to a phone that lost signal ten minutes ago |
| inside the radius | the entire point |
| `NOT is_deleted` | **the easy one to forget** — a soft-deleted partner keeps their last status and coordinates, so without it a deactivated account keeps getting work |

The vehicle is an **inner join**, so a partner with no active vehicle simply does
not appear. An outer join would return them with a NULL vehicle and push the
decision onto Dispatch — which is precisely the judgement this service is
supposed to own.

### Distance

Haversine, in SQL, so ordering and `LIMIT` happen in the database rather than
over a list dragged into Python:

```
a = sin²((φ₂-φ₁)/2) + cos φ₁ · cos φ₂ · sin²((λ₂-λ₁)/2)
d = 2R · asin(min(1, √a))
```

Two details that are easy to get wrong:

- **A bounding box runs first**, so `ix_partners_availability` can do the work
  and the trigonometry only evaluates on rows that already plausibly qualify.
  The box over-selects slightly — it always contains its inscribed circle's
  corners — which is why the exact distance filter still applies afterwards. The
  longitude delta divides by `cos(latitude)`, guarded against zero, since
  longitude degrees shrink towards the poles.
- **Haversine, not the spherical law of cosines.** The cosine form is the
  shorter expression and is wrong here: it ends in `acos(x)` with `x` → 1 for
  nearby points, and `acos` has an infinite derivative there, so rounding error
  is amplified without limit. Measured, it reported ~13cm for a partner standing
  exactly on the pickup pin. Harmless at that size, but the error grows as the
  *inverse* of the distance — precisely backwards for a query whose purpose is
  finding the closest partner.

Both the freshness cutoff and the heartbeat use **Postgres' clock**
(`func.now()`), not the application's. Mixing them would let a container with
drifted time quietly redefine what "stale" means.

Treating the Earth as a sphere is wrong by up to ~0.5%, which is far below the
error in a phone's GPS fix. PostGIS would fix that and is not worth a dependency
to answer "who is within 5km".

---

## Authentication

Structurally identical to userServices — the same encrypted-blob token, the same
double check at gateway and service. See [USER_SERVICE.md](USER_SERVICE.md) for
the cipher and [ARCHITECTURE.md](ARCHITECTURE.md) for why the check happens
twice. What differs here is everything that had to change because **two services
now share one session collection**.

### Sessions carry `app_type`

The session document's `user` field is a bare integer. Each service numbers its
own subjects from 1, so **user 5 and partner 5 are different people sharing a
key**. Without a discriminator, a customer tapping "log out everywhere" would
knock an unrelated partner offline mid-delivery.

So every session records an `app_type` — `1` for users, `2` for partners — and
`revoke_user_sessions()` takes it as a filter. It defaults to `None` only so
sessions written by the older Node service can still be revoked wholesale; every
caller in this repository passes it.

### A separate cookie name

`lp_partner_session`, not `lp_session`. Cookies are keyed by name and domain, so
if the partner dashboard and the customer app are ever served from the same
host, one shared name means signing into one silently overwrites the other's
credential — and each service is then handed a token it cannot decrypt.

`extract_token()` takes the cookie name as an argument rather than trying both.
A browser holding both sessions sends both cookies on the same request, and
guessing between them would authenticate whichever happened to be checked first.
The caller always knows which it wants: the gateway from the route prefix
(`ROUTE_COOKIES`), a service from the fact that it has only one kind of subject.

### `get_current_partner`

The same seven steps as `get_current_user`, plus two:

- the session's `app_type` must be this service's, and
- the token payload's `subject` must be `"partner"`.

Neither is what makes a customer's token unusable here — that is already
guaranteed, since decrypting it needs a `token_secret` stored in a database this
service cannot read. They are the checks that make the boundary **explicit**, so
a future change that accidentally shares a secret fails loudly rather than
authenticating the wrong person.

### Why `utils/security.py` is duplicated

It is byte-for-byte equivalent to userServices' copy — same `.env` secrets, same
key derivation. It is a copy anyway because importing another service's `utils`
imports that service's `config`, and that config pins `user_db`. One stray
import and partnerServices opens a connection to a database it is not allowed to
touch.

If a third service needs this, hoist the cipher into `src/common/crypto.py`
taking its secrets as arguments — the values are properties of the deployment,
not of any one service.

---

## Ratings

```
new_average = (old_average × count + score) / (count + 1)
```

The stored average is `NUMERIC(2,1)`, so each update carries a little of the
previous rounding forward. The drift is bounded by half a decimal place and
shrinks as the count grows — acceptable for a display rating, and the
alternative is a full ratings table this service has no other reason to own.

The 5.0 default over zero samples is a **placeholder, not a rating**, which is
why `rating_count` and not the average drives the arithmetic: the first real
score of 3 sets the average to exactly 3.0 rather than averaging against a five
nobody gave.

Internal-only, because the caller has to be something that knows the delivery
actually happened. Exposed to customers directly, this would be a free endpoint
for tanking any partner's rating.

---

## Configuration

`config.py` reads the same `.env`:

| Variable | Default | Notes |
| --- | --- | --- |
| `DB_HOST` | `localhost` | shared cluster |
| `DB_PORT` | `5432` | 5433 for the 18 cluster |
| `DB_USER` / `DB_PASSWORD` | — | required |
| `PARTNER_DB_NAME` | `partner_db` | this service's own database |
| `STATIC_SALT` / `STATIC_PEPPER` | — | required, password hashing |
| `PASS_SALT_STATIC` | — | required, token key |
| `SECRET_KEY` | `aes-256-cbc` | **algorithm name**, not key material |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `30` | |
| `PARTNER_APP_TYPE` | `2` | session discriminator — do not change once live |
| `PARTNER_SEARCH_RADIUS_KM` | `5.0` | default when Dispatch does not say |
| `PARTNER_SEARCH_LIMIT` | `20` | |
| `PARTNER_LOCATION_STALE_MINUTES` | `5` | heartbeat freshness cutoff |
| `INTERNAL_API_KEY` | `""` | guards `/internal/*`; empty disables the check |

Neither `DB_NAME` nor `USER_DB_NAME` is read here — those belong to other
services. Credentials in `database_url` are percent-encoded, so special
characters in a password are safe.

---

## Migrations

**`partner_db` has its own Alembic environment.** `migration/` owns `user_db`;
`migration_partner/` owns this one. They share nothing but the pattern, for two
reasons: one `alembic_version` table cannot describe two databases, and
`target_metadata` is built from exactly one `Base` — if both services shared
one, every autogenerate would see the other's tables missing and emit
`op.drop_table()` for all of them. [MIGRATIONS.md §6](MIGRATIONS.md) records
what that looks like when it happens.

```powershell
alembic -c alembic_partner.ini upgrade head
alembic -c alembic_partner.ini current
alembic -c alembic_partner.ini revision --autogenerate -m "add something"
```

> **Do not forget `-c alembic_partner.ini`.** Plain `alembic upgrade head` runs
> the *user* migrations. Pointed at `partner_db` they report nothing to do and
> then stamp its version table with user revision ids, which is genuinely
> annoying to unpick.

Everything in [MIGRATIONS.md](MIGRATIONS.md) applies, including §6: a new model
file must be imported in `migration_partner/env.py` or Alembic reads its table
as one to drop.

---

## Running

```powershell
# 1. Create the database (once)
#    psql is at C:\Program Files\PostgreSQL\18\bin\psql.exe
CREATE DATABASE partner_db;

# 2. Apply migrations
alembic -c alembic_partner.ini upgrade head

# 3. Run it
uvicorn src.services.partnerServices.main:app --port 8002 --reload
```

Startup checks Postgres (5 retries, 2s apart), pings Mongo, ensures the session
indexes, and warns if `INTERNAL_API_KEY` is unset. It **fails fast** if Postgres
is unreachable rather than starting and erroring per request.

> **`Fatal error in launcher: Unable to create process using '...python.exe'
> '...uvicorn.exe'`** (or the same for `alembic.exe`) means the venv was created
> at a different path from where the project now sits — the `.exe` stubs in
> `venv\Scripts\` hardcode their interpreter's absolute path. Prefix with
> `python -m` to carry on, and recreate the venv to fix it properly:
>
> ```powershell
> python -m alembic -c alembic_partner.ini upgrade head
> python -m uvicorn src.services.partnerServices.main:app --port 8002 --reload
> ```
>
> See [ARCHITECTURE.md § Common startup problems](ARCHITECTURE.md).

Docs at <http://127.0.0.1:8002/docs> — note that the `/internal` routes appear
there too, since it is the service's own schema, not the gateway's.

---

## A worked example

```powershell
$base = "http://127.0.0.1:8000"     # through the gateway
$svc  = "http://127.0.0.1:8002"     # direct, for /internal

# 1. Register
$r = curl.exe -s -X POST "$base/api/partners/register" `
  -H "Content-Type: application/json" `
  -d '{\"name\":\"Ravi\",\"phone\":\"9876543210\",\"password\":\"drive@1234\"}' | ConvertFrom-Json
$tok = $r.access_token ; $pid = $r.partner.id

# 2. Operations clears KYC
curl.exe -s -X POST "$svc/internal/partners/$pid/verification" `
  -H "Content-Type: application/json" -d '{\"approve\":true}'

# 3. Add a vehicle, get it cleared, put it on the road
$v = curl.exe -s -X POST "$base/api/partners/me/vehicles" -H "Authorization: Bearer $tok" `
  -H "Content-Type: application/json" `
  -d '{\"vehicle_type\":\"two_wheeler\",\"vehicle_number\":\"DL01AB1234\",\"capacity\":25}' | ConvertFrom-Json
curl.exe -s -X POST "$svc/internal/partners/vehicles/$($v.id)/verification" `
  -H "Content-Type: application/json" -d '{\"approve\":true}'
curl.exe -s -X POST "$base/api/partners/me/vehicles/$($v.id)/activate" -H "Authorization: Bearer $tok"

# 4. Go online and send a heartbeat
curl.exe -s -X PATCH "$base/api/partners/me/status" -H "Authorization: Bearer $tok" `
  -H "Content-Type: application/json" -d '{\"status\":\"online\"}'
curl.exe -s -X POST "$base/api/partners/me/location" -H "Authorization: Bearer $tok" `
  -H "Content-Type: application/json" -d '{\"latitude\":28.6330,\"longitude\":77.2190}'

# 5. What Dispatch sees
curl.exe -s "$svc/internal/partners/available?lat=28.6315&lng=77.2167"
```

---

## Not implemented

- **KYC documents.** Verification is a single boolean an operator flips. There
  is no document upload, no review queue, no rejection reason, and no licence
  number or expiry on the row — the trimmed schema deliberately leaves those to
  a later pass, most likely a `partner_documents` table plus file storage.
- **`total_deliveries`.** Dropped with the rest of the trim; `rating_count` is a
  rough proxy until an Order service owns the real number.
- **Location history.** Only the latest fix is stored, so there is no breadcrumb
  trail for live tracking or for disputes. That belongs in a Tracking service
  with a time-series store, not in this table.
- **Phone/OTP login.** Password-based today, which is not how driver apps
  normally work.
- **Rate limiting** on login and on the location heartbeat.
- **Service-to-service auth beyond a shared secret.** `X-Internal-Key` is a
  static string; mTLS or signed service tokens would be the grown-up version.
- **Session listing.** `device_session` / `device_id` are returned but no
  endpoint lists a partner's active devices.
- **Automated tests.** The behaviour above was verified end-to-end against a
  real `partner_db` — including every refusal in the gate sequence, the
  ownership checks, and the availability filters — but that harness is not
  committed. It should become a `tests/` suite.
