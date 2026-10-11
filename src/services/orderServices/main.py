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
from src.services.orderServices.api.delivery_routes import router as delivery_router
from src.services.orderServices.api.internal_routes import router as internal_router
from src.services.orderServices.api.routes import router as order_router
from src.services.orderServices.config import settings
from src.services.orderServices.database.connection import (
    SessionLocal,
    check_database_connection,
    engine,
)
from src.services.orderServices.events.consumers import build_consumer
from src.services.orderServices.models.outbox_model import Outbox

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
    # which is how this service authenticates without calling User.
    check_mongo_connection()
    ensure_indexes()

    if not settings.internal_api_key:
        logger.warning(
            "INTERNAL_API_KEY is not set — /internal/orders/* is unauthenticated, "
            "and calls to userServices carry no key."
        )

    # Both directions of the spine, started together and stopped together.
    #
    # Deliberately NOT fail-fast the way the database check above is. A broker
    # that is down must not stop this service from taking orders: the outbox
    # keeps queueing and the relay drains it when Kafka returns, which is the
    # entire reason the outbox exists. An order that cannot be created is a
    # customer turned away; an order whose event is delayed is a customer who
    # waits a little longer.
    relay = OutboxRelay(SessionLocal, Outbox, service_name="order")
    consumer = build_consumer()

    async with background_loops(relay.run(), consumer.run(), service="order"):
        yield

    engine.dispose()
    close_mongo_connection()


app = FastAPI(title="Order Service", lifespan=lifespan)

# Customer-facing. Reachable through the gateway at /api/orders/*.
app.include_router(order_router, prefix="/api")

# Partner-facing, on its own gateway prefix so the driver's cookie carries it —
# see delivery_routes.py for why that is a separation rather than a naming
# choice.
app.include_router(delivery_router, prefix="/api")

# Service-to-service. Mounted WITHOUT /api on purpose — see internal_routes.py.
app.include_router(internal_router)


@app.get("/health")
def health():
    # Not under /api, so the gateway health aggregation reaches it directly.
    return {
        "status": "ok",
        "service": "orderServices",
        # Surfaced because a false here means events are piling up in the outbox
        # and no delivery will ever be dispatched — a condition that otherwise
        # looks like everything working.
        "events_enabled": kafka_settings.kafka_enabled,
    }
