import logging

from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.errors import PyMongoError

from src.config import settings

logger = logging.getLogger(__name__)

# One client per process. MongoClient is thread-safe and pools internally, so it
# must not be created per request.
client: MongoClient = MongoClient(
    settings.mongo_uri,
    serverSelectionTimeoutMS=10_000,
    tz_aware=True,
)

mongo_db = client[settings.mongo_db_name]


def sessions_collection() -> Collection:
    return mongo_db[settings.mongo_sessions_collection]


def check_mongo_connection() -> None:
    client.admin.command("ping")
    logger.info(
        "Mongo connected successfully (db=%s)",
        settings.mongo_db_name,
    )


def ensure_indexes() -> None:
    """Create the session indexes. Safe to call on every startup."""
    try:
        sessions = sessions_collection()
        sessions.create_index([("token", ASCENDING)], unique=True, name="token_unique")
        sessions.create_index([("user", ASCENDING)], name="user_idx")
        sessions.create_index(
            [("device_session", ASCENDING)], name="device_session_idx"
        )
        sessions.create_index(
            [("user", ASCENDING), ("is_active", ASCENDING)], name="user_active_idx"
        )
    except PyMongoError as error:
        logger.warning("Could not ensure Mongo indexes: %s", error)


def close_mongo_connection() -> None:
    client.close()
