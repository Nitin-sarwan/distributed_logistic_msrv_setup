# Architecture & Getting Started

How the pieces fit together, and how to get the system running.

Component detail lives in [GATEWAY.md](GATEWAY.md) and
[USER_SERVICE.md](USER_SERVICE.md); schema changes in
[MIGRATIONS.md](MIGRATIONS.md).

---

## The shape of it

```
                    ┌──────────────┐
      client ──────▶│   Gateway    │  :8000   ← the only public port
                    │  src/gateway │
                    └──────┬───────┘
                           │ HTTP  (/api/users/* )
                           ▼
                    ┌──────────────┐
                    │ userServices │  :8001
                    └──┬────────┬──┘
                       │        │
              private  │        │  shared
                       ▼        ▼
             ┌───────────┐  ┌──────────────────┐
             │ Postgres  │  │ Mongo Atlas      │
             │  user_db  │  │ common.sessions  │
             └───────────┘  └──────────────────┘
                                     ▲
                                     │ other services share this
```

Two processes today: the gateway and one service. Adding a service means adding
a process and one routing entry.

---

## How they connect

### Client → Gateway

Everything enters at **:8000**. Clients never call a service port directly.

### Gateway → Service

Plain HTTP, routed by path prefix:

```python
SERVICE_ROUTES = {"/api/users": settings.user_service_url}
```

`USER_SERVICE_URL` in `.env` says where that service listens. The gateway
forwards the request unchanged and relays the response verbatim — including
error statuses like `409`.

It adds `X-Forwarded-For` so the service still sees the **real client IP**
rather than the gateway's. Sessions record that IP, so this is load-bearing, not
cosmetic.

### Service → Postgres (private)

Each service owns its own database and **no other service may touch it**.
userServices owns `user_db`. Cross-service data access happens over HTTP, never
by reaching into someone else's tables.

The database name is pinned in the service's own `config.py`; only host, port,
and credentials come from the shared `.env`.

### Service → Mongo (shared)

Sessions are the deliberate exception — one collection, shared by everything, in
`src/database/session_store.py`.

The reason: any service must be able to check whether a session is valid, and
revoking a user everywhere must be a single write. If sessions lived in
userServices' Postgres, every other service would need either SQL access to a
database it doesn't own, or a network round-trip on every request.

**Rule of thumb:** business data is private to its service; session state is
shared infrastructure.

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

# 2. Install dependencies
pip install -r requirement.txt
pip install -r requirements-dev.txt      # tests only

# 3. Create the database (once)
#    psql is at C:\Program Files\PostgreSQL\18\bin\psql.exe
CREATE DATABASE user_db;

# 4. Apply migrations
alembic upgrade head

# 5. Confirm
alembic current        # -> 9d7c2a5904e4 (head)
```

`.env` must exist with these keys — see the tables in
[USER_SERVICE.md](USER_SERVICE.md) and [GATEWAY.md](GATEWAY.md):

```
DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
STATIC_SALT, STATIC_PEPPER, PASS_SALT_STATIC, SECRET_KEY
MONGO_URI, MONGO_DB_NAME
USER_SERVICE_URL
```

---

## Starting the application

**Two terminals, both with the venv active, both from the repo root.**

```powershell
# Terminal 1 — the service (start first)
uvicorn src.services.userServices.main:app --port 8001 --reload
```

```powershell
# Terminal 2 — the gateway
uvicorn src.gateway.main:app --port 8000 --reload
```

Order doesn't strictly matter — the gateway starts fine alone and returns `502`
until the service is up.

### Verify

```powershell
curl http://127.0.0.1:8000/health
```

```json
{"status":"ok","service":"gateway",
 "services":{"/api/users":{"status":"ok","service":"userServices"}}}
```

`"status":"degraded"` means the gateway is up but a service isn't.

### A real request

```powershell
curl -X POST http://127.0.0.1:8000/api/users/register `
  -H "Content-Type: application/json" `
  -d '{\"name\":\"nitish\",\"email\":\"aer@gmail.com\",\"password\":\"nit@123\",\"phone\":\"9853443879\"}'
```

Expect `201` with a user, an access token, and device identifiers.

Interactive docs — note these are **per service**, since the gateway has no
OpenAPI schema of its own:

- userServices — <http://127.0.0.1:8001/docs>
- gateway — <http://127.0.0.1:8000/docs> (health only)

---

## Ports

| Port | What |
| --- | --- |
| 8000 | Gateway — the only one clients use |
| 8001 | userServices |
| 5433 | PostgreSQL 18 |
<!-- | 5432 | PostgreSQL 17 (not used by this project) | -->

---

## Common startup problems

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

---

## Conventions

- **Absolute imports from `src.`** — `from src.services.userServices…`
- **Run everything from the repo root**
- **Schema changes go through Alembic**, never `create_all()`
- **A service never touches another service's database**
- **Secrets live in `.env`**, which is gitignored and must never be committed

---

## Current state

Working: registration end to end, gateway routing, health aggregation, bcrypt
hashing, Node-compatible encrypted tokens, Mongo sessions, first migration.

Not built yet: login, logout, token validation, any service other than users,
tests.

Known leftovers: `src/main.py` and `src/database/connection.py` are the legacy
root app — redundant now, and the latter declares a second unused `Base`.
`models/session_model.py` is orphaned since sessions moved to Mongo, and
`create_tables()` in the service's `database/connection.py` would still rebuild
that dead table behind Alembic's back.
