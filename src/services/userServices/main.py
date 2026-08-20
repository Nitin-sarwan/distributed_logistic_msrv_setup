import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.database.mongo import (
    check_mongo_connection,
    close_mongo_connection,
    ensure_indexes,
)
from src.services.userServices.api.geo_routes import router as geo_router
from src.services.userServices.api.routes import router as user_router
from src.services.userServices.database.connection import (
    check_database_connection,
    engine,
)
from src.services.userServices.utils.geocoder import close_geocoder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    check_database_connection()
    check_mongo_connection()
    ensure_indexes()
    yield
    # shutdown
    engine.dispose()
    close_mongo_connection()
    # The geocoder holds a pooled HTTP client. Closing it here rather than
    # leaving it to garbage collection keeps a "socket left open" warning out of
    # every clean shutdown.
    await close_geocoder()


app = FastAPI(title="User Service", lifespan=lifespan)

app.include_router(user_router, prefix="/api")

# Geocoding, at /api/geo. Outside /users because it is about places rather than
# people: no row is read or written, and no answer depends on who is asking.
# Both of its endpoints are public at the gateway — see api/geo_routes.py for
# why, and for what bounds that.
app.include_router(geo_router, prefix="/api")


@app.get("/health")
def health():
    return {"status": "ok", "service": "userServices"}
