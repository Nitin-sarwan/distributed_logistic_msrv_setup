import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.common.events.config import kafka_settings
from src.common.events.relay import OutboxRelay
from src.common.events.runtime import background_loops
from src.database.mongo import (
    check_mongo_connection,
    close_mongo_connection,
    ensure_indexes,
)
from src.services.dispatchServices.api.internal_routes import router as internal_router
from src.services.dispatchServices.api.routes import router as dispatch_router
from src.services.dispatchServices.config import settings
from src.services.dispatchServices.database.connection import (
    SessionLocal,
    check_database_connection,
    engine,
)
from src.services.dispatchServices.events.consumers import build_consumer
from src.services.dispatchServices.models.outbox_model import Outbox
from src.services.dispatchServices.services.dispatch_worker import DispatchWorker

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
    # which is how a driver's offer response is authenticated without calling
    # partnerServices.
    check_mongo_connection()
    ensure_indexes()

    if not settings.internal_api_key:
        logger.warning(
            "INTERNAL_API_KEY is not set — /internal/dispatch/* is "
            "unauthenticated, and calls to partnerServices carry no key."
        )
    if not kafka_settings.kafka_enabled:
        logger.warning(
            "KAFKA_ENABLED is false — no order will ever reach this service, "
            "because order.confirmed is the only way a search starts."
        )

    # Three loops, started together and stopped together.
    #
    # Deliberately NOT fail-fast the way the database check above is. A broker
    # that is down must not stop this service: drivers can still answer the
    # offers already on their screens, the worker keeps the loop moving, and the
    # outbox holds the results until Kafka returns.
    relay = OutboxRelay(SessionLocal, Outbox, service_name="dispatch")
    consumer = build_consumer()
    worker = DispatchWorker()

    async with background_loops(
        relay.run(), consumer.run(), worker.run(), service="dispatch"
    ):
        yield

    engine.dispose()
    close_mongo_connection()


app = FastAPI(title="Dispatch Service", lifespan=lifespan)

# Partner-facing. Reachable through the gateway at /api/dispatch/*, which the
# gateway authenticates with the partner cookie.
app.include_router(dispatch_router, prefix="/api")

# Operations and service-to-service. Mounted WITHOUT /api on purpose — see
# internal_routes.py.
app.include_router(internal_router)


@app.get("/health")
def health():
    # Not under /api, so the gateway health aggregation reaches it directly.
    return {
        "status": "ok",
        "service": "dispatchServices",
        # Surfaced because a false here means no delivery will ever be
        # dispatched — a condition that otherwise looks like everything working.
        "events_enabled": kafka_settings.kafka_enabled,
        "offer_ttl_seconds": settings.offer_ttl_seconds,
        "search_deadline_seconds": settings.search_deadline_seconds,
    }
