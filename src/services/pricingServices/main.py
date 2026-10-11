import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.services.pricingServices.api.internal_routes import router as internal_router
from src.services.pricingServices.api.routes import router as pricing_router
from src.services.pricingServices.config import settings
from src.services.pricingServices.database.connection import (
    check_database_connection,
    engine,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast rather than starting and erroring per request.
    check_database_connection()

    # No Mongo check: nothing here is authenticated by session. The public
    # routes need nobody signed in, and /internal is guarded by a shared key.
    # No Kafka either — Order asks for a quote and needs the answer before it
    # can reply to the customer, so this is a synchronous service by nature.

    if not settings.internal_api_key:
        logger.warning(
            "INTERNAL_API_KEY is not set — /internal/pricing/* is unauthenticated."
        )

    yield

    engine.dispose()


app = FastAPI(title="Pricing Service", lifespan=lifespan)

# Public. Reachable through the gateway at /api/pricing/*, with no session.
app.include_router(pricing_router, prefix="/api")

# Service-to-service. Mounted WITHOUT /api on purpose — see internal_routes.py.
app.include_router(internal_router)


@app.get("/health")
def health():
    # Not under /api, so the gateway health aggregation reaches it directly.
    return {
        "status": "ok",
        "service": "pricingServices",
        # Surfaced because it changes every price: 1.0 means distances are
        # straight lines, anything above is a detour allowance.
        "road_distance_factor": settings.road_distance_factor,
        "quote_ttl_minutes": settings.quote_ttl_minutes,
    }
