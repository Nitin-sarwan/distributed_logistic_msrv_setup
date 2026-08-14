import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.database.mongo import (
    check_mongo_connection,
    close_mongo_connection,
    ensure_indexes,
)
from src.services.userServices.api.routes import router as user_router
from src.services.userServices.database.connection import (
    check_database_connection,
    engine,
)

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


app = FastAPI(title="User Service", lifespan=lifespan)

app.include_router(user_router, prefix="/api")


@app.get("/health")
def health():
    return {"status": "ok", "service": "userServices"}
