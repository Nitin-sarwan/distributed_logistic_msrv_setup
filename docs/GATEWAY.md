# API Gateway

`src/gateway/main.py` — the single public entry point. Clients talk only to the
gateway; the services sit behind it and are never called directly.

Without it every service would be on its own port and callers would need to know
which. The gateway gives you one host, one port, and one place to stop
unauthenticated traffic before it reaches anything.

---

## What it does, in order

For every request:

1. **Route** — match the path to a service by prefix.
2. **Sanitise** — strip hop-by-hop and forgeable identity headers.
3. **Authenticate** — unless the path is public, require a live session.
4. **Attest** — attach the verified user id and the real client IP.
5. **Forward** — replay the request to the service.
6. **Relay** — return the service's response verbatim.

Anything that fails at 1, 3, or 5 is answered by the gateway itself and never
touches a service.

---

## Imports, and why each is here

```python
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, status
from pymongo.errors import PyMongoError

from src.common.request_auth import extract_token
from src.config import settings
from src.database.session_store import get_active_session
```

| Import | Why |
| --- | --- |
| `logging` | Upstream failures and session-store errors must be visible; a proxy that swallows errors is undebuggable. |
| `asynccontextmanager` | Turns `lifespan` into the startup/shutdown hook FastAPI expects, so the HTTP client is created once and closed cleanly. |
| `httpx` | The forwarding client. **Async**, unlike `requests`, which matters because the gateway spends nearly all its time waiting on services — a blocking call would stall the event loop for every other request. |
| `FastAPI` | The app object. |
| `HTTPException` | Used only for the fail-closed 503 when Mongo is unreachable. |
| `Request` | Read the incoming method, headers, query and body. |
| `Response` | Return raw bytes with a chosen status. **Not** `JSONResponse` — the service's body is relayed untouched, not re-serialised. |
| `status` | Named codes (`HTTP_401_UNAUTHORIZED`) instead of bare integers. |
| `PyMongoError` | Catch session-store failures specifically, so a Mongo outage becomes a clean 503 rather than an unhandled 500. |
| `extract_token` | Shared with the services, so both accept identical credential forms. If they diverged, a request could pass the edge and fail at the service. |
| `settings` | Service URLs and timeout, from `.env` — no hardcoded hosts. |
| `get_active_session` | The only way the gateway can authenticate at all (see below). |

---

## Configuration constants

### `SERVICE_ROUTES`

```python
SERVICE_ROUTES = {
    "/api/users": settings.user_service_url,
    "/api/partners": settings.partner_service_url,
    "/api/geo": settings.geo_service_url,
}
```

`/api/geo` is answered by userServices today, which is why it points at the same
URL — but it is registered as its own prefix rather than nested under
`/api/users`, so moving geocoding to a dedicated process is this one setting and
no change to any caller. See [GEO.md](GEO.md).

The routing table: path prefix → service base URL. Adding a service is one entry
here plus one setting in `.env`.

**What is *not* in this table matters too.** partnerServices also serves
`/internal/partners/*` for Dispatch and operations. Those paths have no entry
here, so the gateway answers `404` and the public internet cannot reach them at
all. An unrouted prefix is a real access control, not an oversight — see
[PARTNER_SERVICE.md](PARTNER_SERVICE.md).

### `ROUTE_COOKIES`

```python
ROUTE_COOKIES = {
    "/api/users": SESSION_COOKIE_NAME,          # lp_session
    "/api/partners": PARTNER_SESSION_COOKIE_NAME,  # lp_partner_session
    "/api/geo": SESSION_COOKIE_NAME,
}
```

Which HttpOnly cookie carries the credential for each service. The names differ
so a browser can hold a customer session and a partner session at once — a
shared name means signing into one silently overwrites the other's cookie, and
each service is then handed a token it cannot decrypt.

The `/api/geo` entry is never read on the two paths that exist, since both are
public. It is present because the lookup is unconditional for any non-public path
under a registered prefix: a typo like `/api/geo/serch` must answer `401` or
`404`, not raise a `KeyError` inside the gateway.

`extract_token()` takes the cookie name as an argument rather than trying both.
A browser holding both sends both on every request, and guessing would
authenticate whichever happened to be checked first. The gateway has already
resolved the route by the time it authenticates, so it always knows which to
read.

### `PUBLIC_PATHS`

```python
PUBLIC_PATHS = {
    "/api/users/register",
    "/api/users/login",
    "/api/users/refresh",
    "/api/users/forgot-password",
    "/api/users/reset-password",
    "/api/partners/register",
    "/api/partners/login",
    "/api/partners/refresh",
    "/api/geo/search",
    "/api/geo/reverse",
    "/health",
}
```

Reachable without a token, each for a specific reason:

| Path | Why public |
| --- | --- |
| `register`, `login` | You cannot hold a token before you have one. |
| `geo/search`, `geo/reverse` | The home page lets a visitor describe a delivery before signing in. Bounded by a per-IP quota and a shared cache rather than by a session — see [GEO.md](GEO.md). |
| `refresh` | Called precisely *because* the access token expired. The refresh token in the body is the credential. |
| `forgot-password`, `reset-password` | Reached by someone who cannot sign in, by definition. |

partnerServices has no forgot/reset pair yet — see
[PARTNER_SERVICE.md](PARTNER_SERVICE.md) § Not implemented.

**Everything not listed requires authentication** — the safe default, since
forgetting to list a new endpoint makes it protected rather than open.

Matching is exact (after stripping a trailing `/`). A public path with a
variable segment would need a prefix rule instead.

### `IDENTITY_HEADERS`

```python
IDENTITY_HEADERS = {"x-user-id", "x-session-id", "x-device-session"}
```

Headers the gateway *asserts* downstream. Any client-supplied copy is **removed
before** the gateway sets its own. Without this, anyone could send
`x-user-id: 1` and impersonate a user — the single most important line in the
file.

### `HOP_BY_HOP`

Connection-scoped headers that describe one hop, not the message:
`connection`, `keep-alive`, `transfer-encoding`, `upgrade`, `host`, and others.
Forwarding them corrupts the exchange. `content-length` is included because
httpx recomputes it — passing a stale one truncates or hangs the request.

---

## Functions

### `match_prefix(path) -> str | None`

Finds the registered prefix a path belongs to.

```python
for prefix in sorted(SERVICE_ROUTES, key=len, reverse=True):
    if path == prefix or path.startswith(prefix + "/"):
        return prefix
```

**Longest prefix first**, so `/api/users/admin` can be routed separately from
`/api/users` — without the sort, dict order would decide, which is arbitrary.

The `path == prefix or startswith(prefix + "/")` test is deliberate: a plain
`startswith(prefix)` would match `/api/users-admin`, sending a different
service's traffic to userServices.

It returns the **prefix**, not the URL, because the caller needs two things
keyed by it — the destination (`SERVICE_ROUTES`) and the cookie name
(`ROUTE_COOKIES`). One lookup key keeps the two tables from drifting apart.

Returns `None` when nothing matches → the caller answers 404.

### `is_public(path) -> bool`

Whether the path skips authentication. `rstrip("/")` means `/api/users/login`
and `/api/users/login/` behave the same.

### `authenticate(request, cookie_name) -> dict | None`

The edge check. Returns the session document, or `None` for unauthenticated.

**What it can and cannot do.** Access tokens are AES-encrypted with a
per-subject `token_secret` stored in the owning service's private Postgres. The
gateway has no access to that — deliberately, since that boundary is what makes
these separate services. So it **cannot decrypt the token**.

What it *can* do is look the token up in the shared Mongo session store, which
is where revocation is recorded. That answers "is there a live session for this
token?" — enough to reject anonymous traffic, not enough to be the last word.

Note what it deliberately does **not** check: whether the session belongs to the
right *kind* of subject. A customer's token presented to `/api/partners` passes
here and is rejected by partnerServices, because decrypting it needs a secret in
a database the gateway cannot read. The edge's job is to turn away anonymous
traffic, not to be the final word.

On a Mongo error it **fails closed**:

```python
except PyMongoError as error:
    raise HTTPException(status_code=503, detail="Session store unavailable")
```

If the store can't be reached, the caller can't be proven authenticated, so the
request must not be forwarded. Failing open would turn a database outage into an
authentication bypass.

### `lifespan(app)`

Creates one `httpx.AsyncClient` at startup and closes it at shutdown.

**One client per process, not per request.** A per-request client would open a
fresh connection every time, exhaust sockets under load, and throw away
connection reuse. `follow_redirects=False` because a redirect is the *service's*
answer to the client — the gateway relays it rather than chasing it.

### `health()`

Calls every registered service's `/health` (5s timeout) and aggregates:

```json
{"status":"ok","service":"gateway",
 "services":{"/api/users":{"status":"ok","service":"userServices"},
             "/api/partners":{"status":"ok","service":"partnerServices"}}}
```

`status` is `ok` only if every service reports `ok`, otherwise `degraded` with
the failure inline. The gateway keeps answering even when everything behind it
is down — which is what makes it useful for diagnosis.

### `proxy(path, request)`

The catch-all that does the forwarding. Declared **after** `/health`, because
FastAPI matches in declaration order and `/{path:path}` would otherwise swallow
it.

Step by step:

**1. Resolve, or 404.**

```python
prefix = match_prefix(full_path)
if prefix is None:  # -> 404 "No service registered for ..."
base_url = SERVICE_ROUTES[prefix]
```

**2. Filter headers.** Both `HOP_BY_HOP` and `IDENTITY_HEADERS` are dropped —
the latter *before* authentication, so a client's forged value can never
survive.

**3. Authenticate non-public paths.**

```python
if not is_public(full_path):
    session = authenticate(request, cookie_name=ROUTE_COOKIES[prefix])
    if session is None:   # -> 401 with WWW-Authenticate: Bearer
```

On success it attaches the verified identity:

```python
headers["x-user-id"] = str(session["user"])
headers["x-device-session"] = session.get("device_session", "")
```

These are **hints, not evidence**. The service re-verifies the token itself —
see [USER_SERVICE.md](USER_SERVICE.md).

**4. Add forwarding headers.**

```python
headers["x-forwarded-for"] = f"{existing}, {client_ip}" if existing else client_ip
```

Once proxied, the service's socket peer *is the gateway* — so without this every
session would record the gateway's IP as the user's. Existing values are
appended to, preserving the chain when a load balancer sits in front.

**5. Forward**, relaying method, headers, body and query string.

**6. Handle failure** — `TimeoutException` → **504**, other `HTTPError` → **502**
(logged). The service being slow or dead must not produce a stack trace.

**7. Relay the response** — status, headers (hop-by-hop stripped) and raw body,
unmodified. A `409` from the service arrives at the client as a `409`.

---

## Failure reference

| Situation | Response |
| --- | --- |
| No prefix matches | `404` No service registered for … |
| No/invalid token on a protected path | `401` Not authenticated |
| Mongo unreachable | `503` Session store unavailable |
| Service unreachable | `502` Upstream service unavailable |
| Service exceeds timeout | `504` Upstream service timed out |

Errors *from* a service (409, 422, …) are relayed untouched.

---

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `USER_SERVICE_URL` | `http://127.0.0.1:8001` | Where userServices listens |
| `PARTNER_SERVICE_URL` | `http://127.0.0.1:8002` | Where partnerServices listens |
| `GATEWAY_TIMEOUT_SECONDS` | `30.0` | Per-request upstream timeout |
| `CORS_ALLOW_ORIGINS` | `localhost:5173,127.0.0.1:5173` | Comma separated; never `*` |

---

## Running

```powershell
uvicorn src.gateway.main:app --port 8000 --reload
```

Starts fine with no services running — requests then return `502`. It must not
depend on start-up order.

> **`Fatal error in launcher: Unable to create process using '...python.exe'
> '...uvicorn.exe'`** means the venv was created at a different path from where
> the project now sits — the `.exe` stubs in `venv\Scripts\` hardcode their
> interpreter's absolute path. Use `python -m uvicorn src.gateway.main:app
> --port 8000 --reload` to carry on, and recreate the venv to fix it properly.
> See [ARCHITECTURE.md § Common startup problems](ARCHITECTURE.md).

---

## Adding a service

1. `src/config.py` — `order_service_url: str = "http://127.0.0.1:8003"`
2. `src/gateway/main.py` — `"/api/orders": settings.order_service_url`
3. `src/gateway/main.py` — an entry in `ROUTE_COOKIES`. Reuse an existing cookie
   name only if the new service authenticates the *same* subject; a new kind of
   subject needs its own name, or its login overwrites the other's cookie.
4. `.env` — `ORDER_SERVICE_URL=...`
5. Add any endpoints that must be reachable without a token to `PUBLIC_PATHS`.

If the service has endpoints only other services should call, leave them off
`/api` entirely rather than trying to protect them here — see
`/internal/partners/*`.

---

## Not implemented

- **Rate limiting**
- **Request/correlation IDs** for tracing a call across services
- **Retries / circuit breaking** — one attempt, no backoff
- **Service discovery** — URLs are static config, not a registry
- **Response caching** — including session lookups, so every authenticated
  request costs one Mongo round-trip
- **WebSocket proxying** — HTTP methods only
- **Streaming** — bodies are buffered whole, so very large uploads or downloads
  are held in memory
