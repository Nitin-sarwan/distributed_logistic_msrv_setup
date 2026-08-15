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
SERVICE_ROUTES = {"/api/users": settings.user_service_url}
```

The routing table: path prefix → service base URL. Adding a service is one entry
here plus one setting in `.env`.

### `PUBLIC_PATHS`

```python
PUBLIC_PATHS = {
    "/api/users/register",
    "/api/users/login",
    "/api/users/refresh",
    "/api/users/forgot-password",
    "/api/users/reset-password",
    "/health",
}
```

Reachable without a token, each for a specific reason:

| Path | Why public |
| --- | --- |
| `register`, `login` | You cannot hold a token before you have one. |
| `refresh` | Called precisely *because* the access token expired. The refresh token in the body is the credential. |
| `forgot-password`, `reset-password` | Reached by someone who cannot sign in, by definition. |

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

### `resolve_service(path) -> str | None`

Finds the service for a path.

```python
for prefix in sorted(SERVICE_ROUTES, key=len, reverse=True):
    if path == prefix or path.startswith(prefix + "/"):
```

**Longest prefix first**, so `/api/users/admin` can be routed separately from
`/api/users` — without the sort, dict order would decide, which is arbitrary.

The `path == prefix or startswith(prefix + "/")` test is deliberate: a plain
`startswith(prefix)` would match `/api/users-admin`, sending a different
service's traffic to userServices.

Returns `None` when nothing matches → the caller answers 404.

### `is_public(path) -> bool`

Whether the path skips authentication. `rstrip("/")` means `/api/users/login`
and `/api/users/login/` behave the same.

### `authenticate(request) -> dict | None`

The edge check. Returns the session document, or `None` for unauthenticated.

**What it can and cannot do.** Access tokens are AES-encrypted with a per-user
`token_secret` stored in userServices' private Postgres. The gateway has no
access to that — deliberately, since that boundary is what makes these separate
services. So it **cannot decrypt the token**.

What it *can* do is look the token up in the shared Mongo session store, which
is where revocation is recorded. That answers "is there a live session for this
token?" — enough to reject anonymous traffic, not enough to be the last word.

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
 "services":{"/api/users":{"status":"ok","service":"userServices"}}}
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
base_url = resolve_service(full_path)
if base_url is None:  # -> 404 "No service registered for ..."
```

**2. Filter headers.** Both `HOP_BY_HOP` and `IDENTITY_HEADERS` are dropped —
the latter *before* authentication, so a client's forged value can never
survive.

**3. Authenticate non-public paths.**

```python
if not is_public(full_path):
    session = authenticate(request)
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
| `GATEWAY_TIMEOUT_SECONDS` | `30.0` | Per-request upstream timeout |

---

## Running

```powershell
uvicorn src.gateway.main:app --port 8000 --reload
```

Starts fine with no services running — requests then return `502`. It must not
depend on start-up order.

---

## Adding a service

1. `src/config.py` — `order_service_url: str = "http://127.0.0.1:8002"`
2. `src/gateway/main.py` — `"/api/orders": settings.order_service_url`
3. `.env` — `ORDER_SERVICE_URL=...`
4. Add any endpoints that must be reachable without a token to `PUBLIC_PATHS`.

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
