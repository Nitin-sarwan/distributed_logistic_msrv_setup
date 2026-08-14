# User Service

`src/services/userServices/` — owns user accounts: registration, credentials,
and issuing session tokens.

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
  services/
    user_services.py           # business rules
  repositories/
    user_repositories.py       # database queries
  models/
    user_model.py              # SQLAlchemy ORM table
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

### `POST /api/users/register`

Request:

```json
{
  "name": "nitish",
  "email": "aer@gmail.com",
  "phone": "9853443879",
  "password": "nit@123"
}
```

`phone` is optional; `email` is lowercased and trimmed; `phone` must be exactly
10 digits.

Response `201`:

```json
{
  "user": {
    "id": 1,
    "name": "nitish",
    "email": "aer@gmail.com",
    "phone": "9853443879",
    "created_at": "2026-08-14T21:37:43.714348+05:30"
  },
  "access_token": "f8b3ced63c9df239…",
  "token_type": "bearer",
  "expires_at": "2026-08-14T17:07:44.161446Z",
  "device_session": "ds_Lyo29eYL",
  "device_id": "di_4N8JEaH"
}
```

| Status | When |
| --- | --- |
| `201` | Created |
| `409` | `Email already registered` / `Phone already registered` |
| `422` | Validation failed (bad email, phone not 10 digits) |
| `500` | Password failed to verify after insert (see below) |

`password` and `token_secret` are **never** in a response — `UserResponse`
declares only five fields, and `response_model` filters everything else out.

### `GET /health`

```json
{"status": "ok", "service": "userServices"}
```

Not under `/api`, so the gateway's health aggregation can reach it directly.

---

## The registration flow

`UserService.register()` in `services/user_services.py`:

1. **Check duplicates** — email and phone in one query. Both are `unique` in the
   database, so checking up front turns a 500 `IntegrityError` into a clean 409.
2. **Hash the password** and generate a 32-byte `token_secret`.
3. **Insert** via the repository.
4. **Verify the stored hash** against the submitted password. If this fails the
   row is deleted and a 500 returned — better than leaving behind an account
   nobody can ever log into.
5. **Issue a token** encrypted with that user's `token_secret`.
6. **Record a session** in Mongo.

Steps 5–6 are `create_user_session()`, kept separate because **login will need
exactly those two steps** and nothing else.

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

All repository lookups filter `is_deleted = false`, so soft-deleted accounts
stay invisible.

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

### Access tokens

Not JWT — an **encrypted blob**, byte-compatible with the Node service:

```
key    = sha256(token_secret + STATIC_PEPPER + PASS_SALT_STATIC)
cipher = AES-256-CBC, random 16-byte IV, PKCS7 padding
output = hex(iv) + hex(ciphertext)
```

Payload: `id`, `name`, `email`, `jti`, `timeStamp`, `exp`.

Consequences worth knowing:

- The token is **opaque** — clients cannot read `exp` or the user id from it.
- Decryption needs `token_secret`, which is **per-user**, so validating a token
  means finding the user first.
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

`signature` comes from an optional `x-signature` request header.

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

`DB_NAME` from `.env` is deliberately **not** read — that belongs to other
services. The credentials in `database_url` are percent-encoded, so special
characters in a password are safe.

---

## Running

```powershell
uvicorn src.services.userServices.main:app --port 8001 --reload
```

Startup checks Postgres (5 retries, 2s apart), pings Mongo, and ensures the
session indexes. It **fails fast** if Postgres is unreachable rather than
starting and erroring per-request.

Docs at `http://127.0.0.1:8001/docs`.

---

## Not implemented

- **`POST /login`** — assembles from parts that already exist:
  `validate_and_get_user()` then `create_user_session()`
- **Token validation dependency** for protecting routes
- **Logout** — `revoke_session()` exists but no endpoint calls it
- `controllers/user_controller.py` is empty; the service layer covers its job
- No tests yet
