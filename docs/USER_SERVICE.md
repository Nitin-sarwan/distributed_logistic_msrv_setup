# User Service

`src/services/userServices/` — owns user accounts: registration, login,
credentials, and issuing session tokens.

It is **self-contained**: its own config, its own database engine, its own
FastAPI app. It shares only the session store (Mongo) with the rest of the
system.

---

## Layout

```
src/services/userServices/
  main.py                      # FastAPI app + lifespan
  config.py                    # service settings, owns the DB name
  api/
    routes.py                  # HTTP endpoints
    schema.py                  # Pydantic request/response models
    dependencies.py            # get_current_user — authentication
  services/
    user_services.py           # business rules
  repositories/
    user_repositories.py       # database queries
    password_reset_repositories.py
  models/
    user_model.py              # SQLAlchemy ORM tables
    password_reset_model.py
    address_model.py
  database/
    base.py                    # DeclarativeBase
    connection.py              # engine, SessionLocal, get_db
  utils/
    security.py                # hashing + token cipher
    exceptions.py              # domain errors
```

### The layers

Each layer talks only to the one below it:

| Layer | Responsibility | Must not |
| --- | --- | --- |
| `api/routes.py` | HTTP in/out, map errors to status codes | contain business rules or SQL |
| `services/` | business rules, orchestration | run queries directly |
| `repositories/` | database queries | make decisions |
| `models/` | table definitions | contain logic |

`api/schema.py` holds the **HTTP contract** — what clients send and receive.
`models/user_model.py` holds the **table**. They are deliberately separate so
your columns don't leak into your public API and each can change independently.

---

## Endpoints

All paths below are relative to the gateway, e.g.
`http://127.0.0.1:8000/api/users/register`.

### `POST /api/users/register` — public

```json
{"name": "nitish", "email": "aer@gmail.com",
 "phone": "9853443879", "password": "nit@123"}
```

`phone` is optional; `email` is lowercased and trimmed; `phone` must be exactly
10 digits.

Response `201` — an `AuthResponse`: the user, an access token, `expires_at`, and
device identifiers. **The client is logged in immediately**; no separate login
call is needed after registering.

| Status | When |
| --- | --- |
| `201` | Created |
| `409` | `Email already registered` / `Phone already registered` |
| `422` | Validation failed |
| `500` | Password failed to verify after insert (see below) |

### `POST /api/users/login` — public

```json
{"email": "aer@gmail.com", "password": "nit@123"}
```

Returns `200` with the **same `AuthResponse` shape** as register. Each login
creates a new session, so signing in on a second device does not disturb the
first.

`401 Invalid email or password` covers both a wrong password and an unknown
email — deliberately identical, since distinguishing them would let someone
enumerate which addresses are registered.

Login normalises email the same way register does. Without that, an account
created as `Aer@Gmail.com` (stored lowercased) could never be signed into.

### `GET /api/users/profile` — authenticated

Requires a token. Returns the `UserResponse` for whoever the token belongs to —
no id in the URL, since the token already determines identity.

### `POST /api/users/logout` — authenticated

Revokes the current session (`is_active = false` in Mongo). The same token then
fails on the next request. It depends on `get_current_user`, so an invalid token
cannot revoke anything.

### `POST /api/users/refresh` — public

```json
{"refresh_token": "<token>"}
```

Exchanges a refresh token for a **new access token**. The refresh token itself
survives; the previous access token is revoked, so only the newest one works.

**Public on purpose** — it is called precisely when the access token has
expired, so requiring one would defeat it. The refresh token in the body *is*
the credential.

`401 Invalid refresh token` if it is unknown, revoked, expired, or is actually
an access token.

### `POST /api/users/logout-all` — authenticated

Revokes **every** session on every device, and rotates `token_secret` so even a
session record that somehow survived could not have its token decrypted.

```json
{"detail": "Logged out everywhere", "sessions_revoked": 8}
```

This is the "someone else has my password" button. Ordinary `/logout` ends one
device; this ends all of them, including outstanding refresh tokens.

### `POST /api/users/change-password` — authenticated

```json
{"current_password": "...", "new_password": "..."}   // new_password: min 8 chars
```

Requires the **current password** as well as a valid session — being signed in
at an unattended browser must not be enough to take the account permanently.

On success: password rehashed, `token_secret` rotated, **every session
revoked**. The caller must sign in again.

`401 Current password is incorrect` if it does not match.

### `POST /api/users/forgot-password` — public

```json
{"email": "aer@gmail.com"}
```

Always returns the same message whether or not the account exists:

```json
{"detail": "If that email is registered, a reset link has been sent."}
```

Answering differently would turn this into an endpoint for discovering which
addresses are registered.

Issuing a new link retires any previous one. Tokens expire after
`PASSWORD_RESET_EXPIRE_MINUTES` (default 30).

> **No mail delivery yet.** With `PASSWORD_RESET_EXPOSE_TOKEN=true` the token is
> returned in the response so the flow is testable locally. That setting hands
> account takeover to anyone who can guess an email address — it **must** be
> false anywhere real, and the token emailed instead.

### `POST /api/users/reset-password` — public

```json
{"token": "<reset token>", "new_password": "..."}
```

Sets a new password without the old one — the reset token is the proof. On
success: `token_secret` rotated, the token marked used, every other pending
token revoked, and **every session revoked**, since a reset implies the password
may have been compromised.

`400 Reset token is invalid or expired` covers unknown, used, revoked, and
expired — all the same message.

### `GET /health` — public

```json
{"status": "ok", "service": "userServices"}
```

Not under `/api`, so the gateway's health aggregation reaches it directly.

---

## Authentication

### Sending a token

Headers only — **query parameters are not accepted**, since they end up in
access logs, browser history, and `Referer` headers:

```
Authorization: Bearer <token>     preferred
X-Token: <token>                  also accepted
```

Extraction lives in `src/common/request_auth.py`, shared with the gateway so
both layers accept exactly the same forms. If they disagreed, a request could
pass the edge and fail at the service.

### `get_current_user` — the authoritative check

`api/dependencies.py`. Add it to any route that needs a user:

```python
def profile(user: User = Depends(get_current_user)):
```

What it does, in order:

1. **Extract the token.** No token → `401`.
2. **Resolve the user.** If `X-User-Id` is present, load that row directly;
   otherwise find the user via the session store.
3. **Decrypt the token** with that user's `token_secret`. *This is the proof* —
   only that user's secret decrypts their token, so success means the token was
   genuinely issued to them.
4. **Match the payload** — `payload["id"]` must equal the loaded user's id, and
   `payload["type"]` must be `"access"`. A refresh token presented as a bearer
   token is rejected here: it is long-lived and exists only to mint access
   tokens.
5. **Check `exp`.**
6. **Check the session is still active** — decryption proves authenticity, but
   only the session store knows about revocation.
7. **`touch_session`** to update `last_activity`.

**`X-User-Id` is a lookup hint, never a claim.** Supplying someone else's id
just loads the wrong `token_secret`, decryption fails, and the request is
rejected. Step 6 runs even when the hint was used, so logout always works.

**The service never trusts the gateway.** A request arriving directly with a
forged `X-User-Id` and no token gets `401`. The gateway's headers save a lookup;
they are not evidence.

Every failure returns the same `401 Not authenticated` with
`WWW-Authenticate: Bearer`. Distinguishing "expired" from "revoked" from
"forged" would tell an attacker which of their guesses was closest.

---

## The registration flow

`UserService.register()`:

1. **Check duplicates** — email and phone in one query. Both are `unique` in the
   database, so checking up front turns a 500 `IntegrityError` into a clean 409.
2. **Hash the password** and generate a 32-byte `token_secret`.
3. **Insert** via the repository.
4. **Verify the stored hash** against the submitted password. If this fails the
   row is deleted and a 500 returned — better than leaving an account nobody can
   ever log into.
5. **Issue a token and record a session** via `create_user_session()`.

Step 5 is separate because **login needs exactly that and nothing else** —
`login()` is just `validate_and_get_user()` followed by `create_user_session()`.

---

## The users table

| Column | Type | Notes |
| --- | --- | --- |
| `id` | integer | PK |
| `name` | varchar(100) | |
| `email` | varchar(255) | unique, indexed |
| `phone` | varchar(10) | unique |
| `password` | text | bcrypt hash, never plaintext |
| `token_secret` | text | per-user token key, 64 hex chars |
| `is_deleted` | boolean | soft delete |
| `created_at` | timestamptz | `server_default=now()` |
| `updated_at` | timestamptz | `onupdate=now()` |

Database: **`user_db`** — pinned in `config.py`, not `.env`, because it belongs
to this service alone.

### Other tables

**`password_resets`** — `user_id`, `token_hash`, `expires_at`, `used_at`,
`is_revoked`, `created_at`. Only a **SHA-256 of the token** is stored, for the
same reason passwords are not stored raw: a leaked table must not let anyone
reset an account. Single use, and completing a reset revokes every other
pending token for that user.

**`address`** — created by a hand-written migration. Its model
(`address_model.py`) exists mainly so Alembic can see it: a table with no model
is invisible to autogenerate, which then reads it as a table to **drop**. See
[MIGRATIONS.md §6](MIGRATIONS.md). Nothing reads or writes it yet.

All repository lookups filter `is_deleted = false`, so soft-deleted accounts stay
invisible — including to `get_current_user`, which means deleting a user also
stops their existing tokens from working.

Schema changes go through Alembic — see [MIGRATIONS.md](MIGRATIONS.md). Never
`create_all()`.

---

## Security

### Password hashing

```
bcrypt( base64( HMAC-SHA256( key=STATIC_PEPPER, msg=STATIC_SALT + password ) ) )
```

- **bcrypt** generates its own random per-user salt, embedded in the output.
- **The pepper** is the security-relevant secret: it lives only in `.env`, never
  in the database, so a stolen `users` table cannot be cracked offline.
- **The HMAC pre-hash** also sidesteps bcrypt's silent 72-byte truncation, so
  long passwords keep their full entropy.

### Two kinds of token

| | Access | Refresh |
| --- | --- | --- |
| Lifetime | `ACCESS_TOKEN_EXPIRE_MINUTES` (60) | `REFRESH_TOKEN_EXPIRE_DAYS` (30) |
| Payload `type` | `"access"` | `"refresh"` |
| Sent as | `Authorization: Bearer` header | body of `/refresh` |
| Can authenticate a request | yes | **no** |
| Can mint tokens | no | yes |

Both use the same cipher and the same per-user key; only the `type` field and
lifetime differ. Login and register issue **both**.

The split exists so the credential that travels on every request is short-lived.
If an access token leaks it dies within the hour; the long-lived refresh token
is sent only to one endpoint.

Sessions are linked: the access session's `parent_token` is the refresh token
that minted it, so revoking a refresh token takes its access tokens with it.
A refresh call retires the access tokens it previously issued, so only the
newest one works — a stolen access token dies at the next refresh.

### Token format

Not JWT — an **encrypted blob**, byte-compatible with the Node service:

```
key    = sha256(token_secret + STATIC_PEPPER + PASS_SALT_STATIC)
cipher = AES-256-CBC, random 16-byte IV, PKCS7 padding
output = hex(iv) + hex(ciphertext)
```

Payload: `id`, `name`, `email`, `jti`, `timeStamp`, `exp`.

Consequences worth knowing:

- The token is **opaque** — clients cannot read `exp` from it, which is why
  `expires_at` is returned in the response body.
- Decryption needs `token_secret`, which is **per-user**, so validating a token
  means finding the user first. This is also why the gateway cannot fully
  validate — see [GATEWAY.md](GATEWAY.md).
- Rotating one row's `token_secret` invalidates **only that user's** tokens.
- CBC is **unauthenticated** — unlike GCM there is no tag, so tampering surfaces
  as a padding or JSON failure. `decrypt_data()` returns `None` uniformly for
  every failure, which is what keeps this safe. Keep it that way.

`STATIC_PEPPER` and `PASS_SALT_STATIC` **must match the Node service exactly**,
or tokens will not cross between them.

---

## Sessions

Sessions go to **Mongo**, not this service's Postgres — see
[ARCHITECTURE.md](ARCHITECTURE.md). Written through
`src/database/session_store.py`, matching the existing Mongoose document shape:
`user`, `valid_ip`, `os`, `app_type`, `device_id`, `device_info`,
`device_session`, `is_active`, `signature`, `token_type`, `token`,
`parent_token`, `login_id`, `last_activity`, `created_at`, `updated_at`.

Plus one addition: **`expires_at`**. The token carries its own `exp`, but only
this service can decrypt it — storing expiry in the document lets the gateway
reject stale sessions without the per-user key. It is optional on read, so
sessions written by the Node service still validate on `is_active` alone.

`signature` comes from an optional `x-signature` request header.

Because the session store is consulted on every authenticated request, **logout
is immediate** — there is no window where a revoked token still works.

---

## Configuration

`config.py` reads `.env`:

| Variable | Default | Notes |
| --- | --- | --- |
| `DB_HOST` | `localhost` | shared cluster |
| `DB_PORT` | `5432` | |
| `DB_USER` | — | required |
| `DB_PASSWORD` | — | required |
| `USER_DB_NAME` | `user_db` | this service's own database |
| `STATIC_SALT` | — | required, password hashing |
| `STATIC_PEPPER` | — | required, hashing + token key |
| `PASS_SALT_STATIC` | — | required, token key |
| `SECRET_KEY` | `aes-256-cbc` | **algorithm name**, not key material |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `30` | |
| `PASSWORD_RESET_EXPIRE_MINUTES` | `30` | |
| `PASSWORD_RESET_EXPOSE_TOKEN` | `false` | **Local dev only** — returns the reset token in the API response. Must stay false in production. |

`DB_NAME` from `.env` is deliberately **not** read — that belongs to other
services. Credentials in `database_url` are percent-encoded, so special
characters in a password are safe.

---

## Running

```powershell
uvicorn src.services.userServices.main:app --port 8001 --reload
```

Startup checks Postgres (5 retries, 2s apart), pings Mongo, and ensures the
session indexes. It **fails fast** if Postgres is unreachable rather than
starting and erroring per-request.

Docs at <http://127.0.0.1:8001/docs>.

---

## Why password changes are not just a field update

A general `PATCH /profile` for `name` and `phone` would be reasonable — and is
**not built yet**. Password deliberately does not belong in it:

- **It needs a second credential.** Changing a password requires the current
  one, not just a valid session. Otherwise an unattended signed-in browser is
  permanent account takeover. A separate route makes that mandatory rather than
  a conditional branch someone can forget.
- **It has destructive side effects.** A name change writes one column; a
  password change rotates `token_secret` and signs the user out of every device.
  An endpoint whose behaviour changes that drastically based on which field was
  sent is a trap.
- **Reset is not authenticated at all.** `forgot-password` and `reset-password`
  exist for someone who *cannot* sign in, so they cannot sit behind an endpoint
  that requires a token.

`/refresh` is separate for the same kind of reason: it is **public**, because it
is called exactly when the access token has expired.

---

## Not implemented

- **`PATCH /profile`** — `name` and `phone` are read-only; the endpoint above
  should exist
- **Email delivery** for password resets — see `PASSWORD_RESET_EXPOSE_TOKEN`
- **Refresh token rotation** — a refresh token is reusable until it expires or
  is revoked; rotating it on each use would limit the damage if one leaked
- **Rate limiting** on login, `forgot-password`, and `refresh` — nothing slows
  down password guessing
- **Session listing** — `device_session` / `device_id` are returned but no
  endpoint lists a user's active devices
- `address` has a table and a model but no endpoints
- `controllers/user_controller.py` is empty; the service layer covers its job
- No tests yet
