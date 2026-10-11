from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for pricing_db.

    One Base per database, for the reason recorded in the other services' bases
    and in MIGRATIONS.md: Alembic builds `target_metadata` from a single Base,
    so a shared one would make every autogenerate propose dropping the other
    services' tables.
    """
