# Deploying for free: Neon + Aiven + Render + Vercel

```
browser ──▶ Vercel (React)  ── /api/* rewrite ──▶ Render: one web service
            logisticpartner.vercel.app            gateway + 7 services, one process
                                                      │        │         │
                                               Neon Postgres  Aiven   Mongo Atlas
                                               (7 databases)  Kafka   (sessions)
```

The browser only ever talks to the Vercel domain. Vercel forwards `/api/*` to
Render, so session cookies are first-party and work in every browser.

**Free-tier limits, honestly:** Render's free service sleeps after 15 minutes
without traffic; the first request then takes about a minute. While it sleeps,
nothing runs — no dispatch, no notifications. Fine for demos; real operations
need a paid instance (Starter, ~$7/mo, same files).

Do the steps in this order — each one produces values the next needs.

---

## 1. Neon — Postgres

1. <https://neon.tech> → sign up → **New project**. Region: **AWS Asia Pacific
   (Singapore)**, same as Render.
2. **Connection details** → copy the **host** (`ep-…-pooler.ap-southeast-1.aws.neon.tech`),
   **user** and **password**.
   Use the *direct* host (without `-pooler`) if migrations ever complain.

You do not create the seven databases: the backend creates them on first start.

## 2. Aiven — Kafka

1. <https://aiven.io> → sign up → **Create service** → **Apache Kafka** →
   **Free** plan, region near Singapore.
2. Service **Overview** → **Connection information** → switch the
   authentication method to **SASL**. Copy:
   - **Host:Port** → `KAFKA_BOOTSTRAP_SERVERS`
   - **User** / **Password** → `KAFKA_SASL_USERNAME` / `KAFKA_SASL_PASSWORD`
   - **CA certificate** → download, open in a text editor, copy all of it →
     `KAFKA_SSL_CA_PEM`
3. **Topics** → create both (auto-creation is off on purpose):
   - `order-events` — **3** partitions
   - `order-events.dlq` — **1** partition

If SASL is not offered on the free plan's page, check the service's
**Advanced configuration** for `kafka_authentication_methods.sasl` and enable it.

## 3. Mongo Atlas — allow Render to connect

Render's outgoing addresses change, so: Atlas → **Network Access** → **Add IP
Address** → **Allow access from anywhere** (`0.0.0.0/0`). The password in
`MONGO_URI` is then what protects it — make sure it is a strong one.

## 4. Render — the backend

1. Dashboard → **New** → **Blueprint** → connect GitHub → choose
   `distributed_logistic_msrv_setup`, branch **main**. Render reads
   `render.yaml`.
2. It asks for every `sync: false` value:

   | Key | Value |
   | --- | --- |
   | `DB_HOST`, `DB_USER`, `DB_PASSWORD` | from Neon (step 1) |
   | `MONGO_URI` | your Atlas connection string (same as local `.env`) |
   | `CORS_ALLOW_ORIGINS` | your Vercel URL, e.g. `https://logisticpartner.vercel.app` (fill in after step 5; any value for now) |
   | `KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_SASL_USERNAME`, `KAFKA_SASL_PASSWORD`, `KAFKA_SSL_CA_PEM` | from Aiven (step 2) |
   | `PAYMENT_UPI_VPA` | your business UPI ID, e.g. `yourname@okhdfcbank` |
   | `GEOCODER_USER_AGENT` | `logisticpartner/0.1 (you@example.com)` — a real contact; OpenStreetMap blocks anonymous traffic |

3. **Apply**. The first deploy creates the seven databases, migrates them, and
   starts. Watch **Logs** for `Application startup complete`.
4. Open `https://<your-service>.onrender.com/health` — every service should say
   `"ok"`.

**Note the service URL.** If it is not exactly
`https://logisticpartner-api.onrender.com` (Render adds a suffix when a name is
taken), change both destinations in the frontend's `vercel.json` to match.

## 5. Vercel — the frontend

1. <https://vercel.com> → **Add New → Project** → import
   `distributed_logistic_web`. Vercel detects Vite; `vercel.json` and
   `.env.production` configure the rest. No environment variables needed.
2. **Deploy**. Open the URL → book a delivery.
3. Put that URL into Render's `CORS_ALLOW_ORIGINS` (Environment tab) and
   redeploy Render.

## Before real users

- `PAYMENT_ONLINE_AUTOCAPTURE=false` once a payment provider exists — until then
  "Pay online now" is free.
- `PARTNER_AUTO_VERIFY=false` once there is an operations screen.
- A paid Render instance, so dispatch keeps running when nobody is browsing.
