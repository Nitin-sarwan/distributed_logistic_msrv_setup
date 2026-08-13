import logging
import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from src.config import settings

logger = logging.getLogger(__name__)

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,   # drop stale connections instead of raising mid-request
)


class Base(DeclarativeBase):
    pass


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
            logger.info("Database connected successfully")
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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
