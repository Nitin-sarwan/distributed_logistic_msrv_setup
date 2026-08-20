import logging
import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from src.services.partnerServices.config import settings

logger = logging.getLogger(__name__)

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,   # drop stale connections instead of raising mid-request
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


def check_database_connection(retries: int = 5, delay: float = 2) -> None:
    for attempt in range(1, retries + 1):
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            logger.info("Database connected successfully (%s)", settings.partner_db_name)
            return
        except SQLAlchemyError as error:
            logger.warning(
                "Database connection failed (attempt %d/%d): %s",
                attempt, retries, error,
            )
            if attempt < retries:
                logger.info("Retrying in %s seconds...", delay)
                time.sleep(delay)

    raise RuntimeError(f"Could not connect to database after {retries} attempts")


# No create_tables() here, deliberately. userServices has one and MIGRATIONS.md
# §8 rule 6 explains why it should not: create_all() builds tables behind
# Alembic's back and the schema then diverges from the migration history
# silently. partner_db is Alembic's from the first revision.


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
