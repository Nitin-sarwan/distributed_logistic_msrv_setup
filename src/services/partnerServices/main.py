import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.database.mongo import (
    check_mongo_connection,
    close_mongo_connection,
    ensure_indexes,
)
from src.services.partnerServices.api.internal_routes import router as internal_router
from src.services.partnerServices.api.routes import router as partner_router
from src.services.partnerServices.config import settings
from src.services.partnerServices.database.connection import (
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
    # startup — fail fast rather than starting and erroring per request.
    check_database_connection()
    check_mongo_connection()
    ensure_indexes()

    if not settings.internal_api_key:
        logger.warning(
            "INTERNAL_API_KEY is not set — /internal/partners/* is unauthenticated. "
            "Acceptable on a local machine only: those endpoints approve KYC and "
            "reassign partners."
        )

    yield
    # shutdown
    engine.dispose()
    close_mongo_connection()


app = FastAPI(title="Partner Service", lifespan=lifespan)

# Partner-facing. Reachable through the gateway at /api/partners/*.
app.include_router(partner_router, prefix="/api")

# Service-to-service. Mounted WITHOUT the /api prefix on purpose: the gateway
# routes /api/partners and nothing else, so these paths do not exist as far as
# the public edge is concerned. Callers inside the deployment reach them
# directly on this port.
app.include_router(internal_router)


@app.get("/health")
def health():
    # Not under /api, so the gateway's health aggregation reaches it directly.
    return {"status": "ok", "service": "partnerServices"}
