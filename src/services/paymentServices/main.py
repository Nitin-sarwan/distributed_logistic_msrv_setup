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
from src.services.paymentServices.api.internal_routes import router as internal_router
from src.services.paymentServices.api.routes import router as driver_router
from src.services.paymentServices.config import settings
from src.services.paymentServices.database.connection import (
    SessionLocal,
    check_database_connection,
    engine,
)
from src.services.paymentServices.events.consumers import build_consumer
from src.services.paymentServices.models.outbox_model import Outbox

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_database_connection()
    # The shared session store authenticates the driver's collection calls.
    check_mongo_connection()
    ensure_indexes()

    if settings.payment_online_autocapture:
        logger.warning(
            "PAYMENT_ONLINE_AUTOCAPTURE is on — 'Pay online now' succeeds with no "
            "provider. Development only."
        )
    if settings.upi_is_placeholder:
        logger.warning(
            "PAYMENT_UPI_VPA is the placeholder — the driver's QR points at a UPI ID "
            "no bank will accept. Set it to the business's real UPI ID."
        )
    if not settings.internal_api_key:
        logger.warning("INTERNAL_API_KEY is not set — /internal/payments/* is unauthenticated.")

    # A broker outage must not stop drivers recording what they collected: the
    # outbox holds `payment.captured` until Kafka is back.
    relay = OutboxRelay(SessionLocal, Outbox, service_name="payment")
    consumer = build_consumer()
    async with background_loops(relay.run(), consumer.run(), service="payment"):
        yield

    engine.dispose()
    close_mongo_connection()


app = FastAPI(title="Payment Service", lifespan=lifespan)

# Driver-facing, through the gateway at /api/deliveries/payments with the
# partner cookie.
app.include_router(driver_router, prefix="/api")

# Service-to-service. Mounted WITHOUT /api, so the gateway never routes it.
app.include_router(internal_router)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "paymentServices",
        "events_enabled": kafka_settings.kafka_enabled,
        # Surfaced so a deployment cannot quietly be giving deliveries away.
        "online_payments_simulated": settings.payment_online_autocapture,
        "upi_vpa_is_placeholder": settings.upi_is_placeholder,
    }
