import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.database.connection import check_database_connection, engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    check_database_connection()
    yield
    # shutdown
    engine.dispose()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}
