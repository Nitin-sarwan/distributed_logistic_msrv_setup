# API Gateway

`src/gateway/main.py` — the single public entry point. Clients talk only to the
gateway; the individual services sit behind it and are not called directly.

Without it, every service would be on its own port and callers would need to
know which. The gateway gives you one host and one port for the whole system.

---

## What it does

1. **Routes** an incoming path to the right service, by prefix.
2. **Forwards** the request — method, headers, query string, body — unchanged.
3. **Adds forwarding headers** so the service still sees the real client.
4. **Returns** the service's response verbatim: status, headers, body.
5. **Handles failure** when a service is slow or down, instead of hanging.
6. **Aggregates health** across every registered service.

It is a **pure pass-through**. It does not authenticate, authorise, rate-limit,
or rewrite payloads. Each service still validates its own requests.

---

## Routing

```python
SERVICE_ROUTES: dict[str, str] = {
    "/api/users": settings.user_service_url,
}
```

A path matches a prefix if it equals it or continues with `/`. So
`/api/users/register` → userServices, but `/api/users-admin` does **not** match
`/api/users`.

**Longest prefix wins.** With both `/api/users` and `/api/users/admin`
registered, a request to `/api/users/admin/x` goes to the more specific one.

Anything unmatched gets a `404` from the gateway itself:

```json
{"detail": "No service registered for /api/orders/list"}
```

### Adding a service

1. Add its URL to `src/config.py`:
   ```python
   order_service_url: str = "http://127.0.0.1:8002"
   ```
2. Register the prefix in `src/gateway/main.py`:
   ```python
   SERVICE_ROUTES = {
       "/api/users": settings.user_service_url,
       "/api/orders": settings.order_service_url,
   }
   ```
3. Set `ORDER_SERVICE_URL` in `.env`.

---

## Headers

### Added on the way out

| Header | Why |
| --- | --- |
| `X-Forwarded-For` | The real client IP |
| `X-Forwarded-Proto` | Original scheme (`http` / `https`) |
| `X-Forwarded-Host` | Original `Host` the client asked for |

**`X-Forwarded-For` is the one that matters.** Once traffic is proxied, the
service's socket peer is the *gateway* — so `request.client.host` would record
the gateway's IP on every session row. The gateway sets the header explicitly,
and `get_request_info()` in `src/database/session_store.py` reads it in
preference to the socket peer, so `valid_ip` stays truthful.

If the header is already present (a load balancer ahead of the gateway), the
gateway **appends** rather than overwrites, preserving the chain.

### Stripped both directions

`connection`, `keep-alive`, `proxy-authenticate`, `proxy-authorization`, `te`,
`trailers`, `transfer-encoding`, `upgrade`, `host`, `content-length`.

These are **hop-by-hop** headers: they describe a single connection, not the
message, so forwarding them corrupts the exchange. `content-length` must go
because httpx recomputes it for the new body — forwarding a stale one truncates
or hangs the request.

---

## Failure handling

| Situation | Response |
| --- | --- |
| No prefix matches | `404` `No service registered for …` |
| Service unreachable | `502` `Upstream service unavailable` |
| Service exceeds timeout | `504` `Upstream service timed out` |

Timeout is `GATEWAY_TIMEOUT_SECONDS`, default `30.0`.

Errors *from* a service — a `409` on duplicate email, a `422` on validation —
are **not** touched. They pass through with their status and body intact.

---

## Health

```
GET /health
```

The gateway calls every registered service's `/health` (5s timeout) and reports:

```json
{
  "status": "ok",
  "service": "gateway",
  "services": {
    "/api/users": {"status": "ok", "service": "userServices"}
  }
}
```

`status` is `ok` only when every service reports `ok`; otherwise `degraded`:

```json
{
  "status": "degraded",
  "service": "gateway",
  "services": {
    "/api/users": {"status": "unreachable", "error": "All connection attempts failed"}
  }
}
```

The gateway itself stays up and answers `/health` even when everything behind it
is down — which is what makes it useful for diagnosis.

---

## Configuration

Read from `.env` via `src/config.py`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `USER_SERVICE_URL` | `http://127.0.0.1:8001` | Where userServices listens |
| `GATEWAY_TIMEOUT_SECONDS` | `30.0` | Per-request upstream timeout |

---

## Running

```powershell
uvicorn src.gateway.main:app --port 8000 --reload
```

The gateway starts fine with no services running — requests then return `502`.
That is intentional: it must not depend on start-up order.

---

## Implementation notes

**One pooled `httpx.AsyncClient` per process**, created in `lifespan` and closed
on shutdown. A client per request would exhaust sockets under load and throw
away connection reuse.

**`follow_redirects=False`** — a redirect is the service's answer to the client,
so the gateway relays it rather than chasing it.

**Catch-all route.** `@app.api_route("/{path:path}")` is declared *after*
`/health`, since FastAPI matches in declaration order and the catch-all would
otherwise swallow it.

---

## Not implemented

Deliberate omissions, in rough priority order:

- **Token validation** — the natural next step. The pieces exist:
  `decrypt_data()` plus a Mongo session lookup.
- **Rate limiting**
- **Request/correlation IDs** for tracing a call across services
- **Retries / circuit breaking** — one attempt, no backoff
- **Service discovery** — URLs are static config, not a registry
- **WebSocket proxying** — HTTP methods only
- **Streaming** — bodies are buffered whole, so very large uploads or downloads
  are held in memory
