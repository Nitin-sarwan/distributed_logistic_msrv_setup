"""Start the backend on a host: databases, migrations, then the server.

    python -m src.deploy.start

1. Create any of the seven databases that do not exist yet, on the server in
   DB_HOST (Neon, Render Postgres...). Existing ones are left alone.
2. Bring each to its Alembic head — seven histories, seven commands, the same
   ones ARCHITECTURE.md lists for local setup.
3. Run the all-in-one app (src/deploy/all_in_one.py) on $PORT.

Every step is idempotent, so a restart repeats them harmlessly. A failing
migration stops the start: serving against a half-migrated schema is worse
than not serving.
"""

import os
import subprocess
import sys
from urllib.parse import quote

import psycopg

DATABASES = {
    "user_db": "alembic.ini",
    "partner_db": "alembic_partner.ini",
    "order_db": "alembic_order.ini",
    "dispatch_db": "alembic_dispatch.ini",
    "notification_db": "alembic_notification.ini",
    "pricing_db": "alembic_pricing.ini",
    "payment_db": "alembic_payment.ini",
}


def log(message: str) -> None:
    print(f"[start] {message}", flush=True)


def ensure_databases() -> None:
    # The shared settings read real env vars on a host and `.env` locally.
    from src.config import settings

    # A database that always exists, to connect to while creating the others:
    # Neon's is `neondb`, most other hosts have `postgres`.
    admin_db = os.environ.get("DB_ADMIN_NAME", "postgres")
    conninfo = (
        f"postgresql://{quote(settings.db_user, safe='')}:"
        f"{quote(settings.db_password, safe='')}@{settings.db_host}:"
        f"{settings.db_port}/{admin_db}"
        + (f"?sslmode={settings.db_sslmode}" if settings.db_sslmode else "")
    )
    with psycopg.connect(conninfo, autocommit=True) as conn:
        existing = {row[0] for row in conn.execute("SELECT datname FROM pg_database")}
        for name in DATABASES:
            if name in existing:
                continue
            log(f"creating database {name}")
            conn.execute(f'CREATE DATABASE "{name}"')


def migrate() -> None:
    for name, config in DATABASES.items():
        log(f"migrating {name} ({config})")
        subprocess.run(
            [sys.executable, "-m", "alembic", "-c", config, "upgrade", "head"],
            check=True,
        )


def serve() -> None:
    port = os.environ.get("PORT", "10000")
    log(f"serving on :{port}")
    command = [
        sys.executable, "-m", "uvicorn", "src.deploy.all_in_one:app",
        "--host", "0.0.0.0",
        "--port", port,
        # The loopback guard on /svc/* must see the real peer address.
        "--no-proxy-headers",
    ]
    if os.name == "nt":
        # Windows exec re-splits arguments on spaces ("C:\Users\Nitish Sarwan");
        # a child process does not.
        sys.exit(subprocess.call(command))
    # On the host: replace this process, so the server is what the platform
    # supervises and no idle parent holds memory.
    os.execv(sys.executable, command)


if __name__ == "__main__":
    ensure_databases()
    migrate()
    serve()
