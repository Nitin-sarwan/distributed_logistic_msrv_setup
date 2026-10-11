"""The tick that drives every search forward.

Why a polling loop rather than doing the work inside the Kafka handler for
`order.confirmed`: because a search is not one action, it is a sequence spread
over minutes. Somebody has to notice that an offer expired 200ms ago and hand
the job to the next driver, and no event arrives to say so - the absence of a
response is the thing being detected. A handler that tried to run the whole loop
would have to hold a Kafka partition open for three minutes, blocking every
other order behind it.

So the handler does one thing - write the attempt row - and this loop does the
rest, entirely from table state. That is also what makes it restartable: kill
the process mid-search and the next tick picks up exactly where it stopped,
because there was never anything in memory to lose.

Each unit of work gets its **own session and its own transaction**. One order's
slow candidate search must not hold a lock that another order's accept is
waiting on, and one order's failure must not roll back another's progress.
"""

import asyncio
import logging

from src.services.dispatchServices.config import settings
from src.services.dispatchServices.database.connection import SessionLocal
from src.services.dispatchServices.repositories.dispatch_repositories import (
    DispatchRepository,
)
from src.services.dispatchServices.services.dispatch_services import DispatchService

logger = logging.getLogger(__name__)


class DispatchWorker:
    """Runs `expire` then `advance` for every live search, once per tick."""

    async def run(self) -> None:
        """Tick forever. Cancelled by the lifespan on shutdown."""
        logger.info(
            "dispatch worker started: tick %.1fs, offer TTL %ds, deadline %ds",
            settings.worker_tick_seconds,
            settings.offer_ttl_seconds,
            settings.search_deadline_seconds,
        )

        try:
            while True:
                try:
                    # Both run on worker threads: SQLAlchemy's Session is
                    # synchronous, and so is the httpx call to partnerServices
                    # inside the candidate search. Calling either directly from
                    # the coroutine would block the event loop that is also
                    # serving the driver's accept request.
                    await asyncio.to_thread(self._tick)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Never let the loop die. An unhandled exception here stops
                    # every delivery on the platform from finding a driver, and
                    # it does it silently.
                    logger.exception("dispatch worker tick failed")

                await asyncio.sleep(settings.worker_tick_seconds)
        except asyncio.CancelledError:
            logger.info("dispatch worker stopping")
            raise

    # -- One tick, on a worker thread -------------------------------------

    def _tick(self) -> None:
        self._expire_offers()

        for attempt_id in self._searching_attempts():
            self._advance(attempt_id)

    def _expire_offers(self) -> None:
        """Sweep the whole platform's live offers for timeouts, in one pass."""
        with SessionLocal() as db:
            try:
                DispatchService(db).expire_due_offers()
            except Exception:
                db.rollback()
                logger.exception("expiring offers failed")

    def _searching_attempts(self) -> list[int]:
        with SessionLocal() as db:
            return DispatchRepository(db).searching_attempt_ids()

    def _advance(self, attempt_id: int) -> None:
        """One search, one step, one transaction.

        The try/except is per attempt on purpose. partnerServices timing out on
        one order, or one poisoned row, must not stop the other searches in this
        tick from progressing.
        """
        with SessionLocal() as db:
            try:
                DispatchService(db).advance(attempt_id)
            except Exception:
                db.rollback()
                logger.exception("advancing attempt %s failed", attempt_id)
