"""Alembic environment for partner_db.

A second, separate environment. `migration/` owns `user_db` and this one owns
`partner_db`, and they share nothing but the pattern — which is the point:

* Each service owns its own database, so each needs its own migration history.
  A single `alembic_version` table cannot describe two databases.
* `target_metadata` is built from exactly one `Base`. If both services shared
  one, every autogenerate would see the other service's tables missing from the
  database it is pointed at and cheerfully emit `op.drop_table()` for all of
  them. MIGRATIONS.md §6 records what that looks like when it happens.

Run it with the matching config file, from the repo root:

    alembic -c alembic_partner.ini upgrade head

Forgetting `-c` runs the *user* migrations instead. They will report "nothing to
do" against partner_db and then stamp partner_db's version table with user
revision ids, which is a genuinely annoying mess to unpick.
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

from src.services.partnerServices.config import settings
from src.services.partnerServices.database.base import Base

# Models must be imported so they register on Base.metadata before autogenerate
# inspects it — an unimported model looks like a table that should be dropped.
from src.services.partnerServices.models import (  # noqa: F401
    partner_model,
    vehicle_model,
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
