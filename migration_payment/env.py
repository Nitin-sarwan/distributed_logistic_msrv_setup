"""Alembic environment for payment_db.

The sixth separate environment, one per database: `migration/` owns `user_db`,
`migration_partner/` `partner_db`, `migration_order/` `order_db`,
`migration_dispatch/` `dispatch_db`, `migration_notification/`
`notification_db`, `migration_pricing/` `pricing_db`, and this one owns
`payment_db`. They share nothing but the
pattern — `target_metadata` is built from exactly one `Base`, because a shared
one would make every autogenerate propose dropping the other services' tables.

Run it with the matching config file, from the repo root:

    alembic -c alembic_payment.ini upgrade head

Forgetting `-c` runs the *user* migrations instead.
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

from src.services.paymentServices.config import settings
from src.services.paymentServices.database.base import Base

# Models must be imported so they register on Base.metadata before autogenerate
# inspects it — an unimported model looks like a table that should be dropped.
from src.services.paymentServices.models import (  # noqa: F401
    outbox_model,
    payment_model,
    processed_event_model,
)

target_metadata = Base.metadata

# Set the URL here rather than in the .ini: the password is percent-encoded, and
# configparser would treat a literal % as interpolation syntax.
config.set_main_option("sqlalchemy.url", settings.database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
