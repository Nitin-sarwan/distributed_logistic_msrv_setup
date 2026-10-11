import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.common.events.config import kafka_settings
from src.common.events.runtime import background_loops
from src.database.mongo import (
    check_mongo_connection,
    close_mongo_connection,
    ensure_indexes,
)
from src.services.notificationServices.api.internal_routes import router as internal_router
from src.services.notificationServices.api.routes import (
    customer_router,
    partner_router,
)
from src.services.notificationServices.config import settings
from src.services.notificationServices.database.connection import (
    check_database_connection,
    engine,
)
from src.services.notificationServices.domain.templates import TEMPLATES
from src.services.notificationServices.events.consumers import build_consumer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast rather than starting and erroring per request.
    check_database_connection()
    # Mongo is not this service's data store — it is the shared session store,
    # which is how a feed request is authenticated without calling User.
    check_mongo_connection()
    ensure_indexes()

    if not settings.internal_api_key:
        logger.warning(
            "INTERNAL_API_KEY is not set — /internal/notifications/* is "
            "unauthenticated."
        )
    if not kafka_settings.kafka_enabled:
        logger.warning(
            "KAFKA_ENABLED is false — nothing will ever be added to anybody's "
            "feed, because events are the only thing that writes to it."
        )

    logger.info(
        "notification templates registered for: %s", ", ".join(sorted(TEMPLATES))
    )

    # One loop, not two. Notification is a terminal consumer — it reads domain
    # facts and produces messages, and emits no events of its own, so there is
    # no outbox and no relay.
    consumer = build_consumer()

    async with background_loops(consumer.run(), service="notification"):
        yield

    engine.dispose()
    close_mongo_connection()


app = FastAPI(title="Notification Service", lifespan=lifespan)

# Customer app. Gateway prefix /api/notifications, customer cookie.
app.include_router(customer_router, prefix="/api")

# Partner app. Gateway prefix /api/deliveries/notifications, partner cookie.
# The gateway matches the longest prefix first, which is what puts this here
# rather than at orderServices — see api/routes.py.
app.include_router(partner_router, prefix="/api")

# Operations. Mounted WITHOUT /api on purpose — see internal_routes.py.
app.include_router(internal_router)


@app.get("/health")
def health():
    # Not under /api, so the gateway health aggregation reaches it directly.
    return {
        "status": "ok",
        "service": "notificationServices",
        # Surfaced because a false here means the feed will stay empty forever —
        # a condition that otherwise looks like a quiet day.
        "events_enabled": kafka_settings.kafka_enabled,
        # Both are logging stubs until the RabbitMQ workers exist. Surfaced so a
        # deployment cannot believe it is sending email when it is writing log
        # lines.
        "email_stubbed": True,
        "push_stubbed": True,
        "templates": len(TEMPLATES),
    }
