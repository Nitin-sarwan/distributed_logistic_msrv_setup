# Architecture & Getting Started

How the pieces fit together, and how to get the system running.

Component detail lives in [GATEWAY.md](GATEWAY.md),
[USER_SERVICE.md](USER_SERVICE.md) and
[PARTNER_SERVICE.md](PARTNER_SERVICE.md); schema changes in
[MIGRATIONS.md](MIGRATIONS.md).

---

## The shape of it

```
                          ┌──────────────┐
        client ──────────▶│   Gateway    │  :8000  ← the only public port
                          │  src/gateway │
                          └──┬────────┬──┘
              /api/users/*   │        │   /api/partners/*
                    ┌────────┘        └────────┐
                    ▼                          ▼
            ┌──────────────┐          ┌─────────────────┐
            │ userServices │  :8001   │ partnerServices │  :8002
            └──┬────────┬──┘          └──┬───────────┬──┘
       private │        │ shared  shared │           │ private
               ▼        ▼                ▼           ▼
       ┌───────────┐  ┌──────────────────┐   ┌────────────┐
       │ Postgres  │  │ Mongo Atlas      │   │  Postgres  │
       │  user_db  │  │ common.sessions  │   │ partner_db │
       └───────────┘  └──────────────────┘   └────────────┘

                     ┌──────────────────┐
                     │ Dispatch (later) │──── HTTP ────▶ partnerServices
                     └──────────────────┘   /internal/*     :8002
```

Three processes today: the gateway and two services. Adding a service means
adding a process and one routing entry.

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
}
```

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

Dispatch will need to know which partners are free. It will ask
`GET /internal/partners/available`, not run a query — that is the rule in its
concrete form.

The database name is pinned in the service's own `config.py`; only host, port,
and credentials come from the shared `.env`. Each database also has its own
Alembic history: `migration/` for `user_db`, `migration_partner/` for
`partner_db`.

### Service → Mongo (shared)

Sessions are the deliberate exception — one collection, shared by everything, in
`src/database/session_store.py`.

The reason: any service must be able to check whether a session is valid, and
revoking a user everywhere must be a single write. If sessions lived in
userServices' Postgres, every other service would need either SQL access to a
database it doesn't own, or a network round-trip on every request.

**Rule of thumb:** business data is private to its service; session state is
shared infrastructure.

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
| `.env` | at the repo root, gitignored |

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

# 4. Apply migrations — one history per database, so two commands
alembic upgrade head
alembic -c alembic_partner.ini upgrade head

# 5. Confirm
alembic current                          # -> a1c4e07b92d3 (head)
alembic -c alembic_partner.ini current   # -> b1f4a72c9e01 (head)
```

> **`-c alembic_partner.ini` is not optional.** Plain `alembic upgrade head`
> runs the *user* migrations. See [MIGRATIONS.md](MIGRATIONS.md).

`.env` must exist with these keys — see the tables in
[USER_SERVICE.md](USER_SERVICE.md) and [GATEWAY.md](GATEWAY.md):

```
DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
STATIC_SALT, STATIC_PEPPER, PASS_SALT_STATIC, SECRET_KEY
MONGO_URI, MONGO_DB_NAME
USER_SERVICE_URL, PARTNER_SERVICE_URL
INTERNAL_API_KEY
```

`.env.example` at the repo root lists all of them with notes.

---

## Starting the application

**Three terminals, all with the venv active, all from the repo root.**

```powershell
# Terminal 1 — userServices
uvicorn src.services.userServices.main:app --port 8001 --reload
```

```powershell
# Terminal 2 — partnerServices
uvicorn src.services.partnerServices.main:app --port 8002 --reload
```

```powershell
# Terminal 3 — the gateway
uvicorn src.gateway.main:app --port 8000 --reload
```

Order doesn't strictly matter — the gateway starts fine alone and returns `502`
for whichever service is down.

### Verify

```powershell
curl http://127.0.0.1:8000/health
```

```json
{"status":"ok","service":"gateway",
 "services":{"/api/users":{"status":"ok","service":"userServices"},
             "/api/partners":{"status":"ok","service":"partnerServices"}}}
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

Every `.exe` in `venv\Scripts\` is a small launcher stub with the absolute path
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

Everything else belongs to a single service.

---

## Current state

**userServices** — register, login, profile, logout, log-out-everywhere, token
refresh, change password, forgot/reset password.

**partnerServices** — partner register/login/refresh/logout, profile, on-off
duty with the full gate sequence, location heartbeats, vehicles with
verification and a one-active-per-partner invariant, ratings, suspension, and
the `/internal` availability search Dispatch will call.

**Gateway** — routing to both services with edge authentication, per-service
session cookies, health aggregation.

**Shared** — bcrypt hashing, Node-compatible encrypted tokens, Mongo sessions
with revocation and an `app_type` discriminator, two Alembic histories.

Not built yet: `PATCH /profile` for name and phone, email delivery for password
resets, refresh-token rotation, rate limiting, session listing, KYC document
upload, Dispatch and Order services, tests.

Known leftovers: `src/main.py` and `src/database/connection.py` are the legacy
root app — redundant now, and the latter declares a second unused `Base`.
`models/session_model.py` is orphaned since sessions moved to Mongo, and
`create_tables()` in the service's `database/connection.py` would still rebuild
that dead table behind Alembic's back.
