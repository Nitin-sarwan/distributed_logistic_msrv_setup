from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for partner_db.

    Separate from userServices' Base on purpose. Alembic builds its
    `target_metadata` from one Base, so sharing would put `users` and
    `partners` in the same metadata and each service's autogenerate would
    propose dropping the other's tables.
    """
