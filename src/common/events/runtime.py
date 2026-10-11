"""Starting and stopping the background loops, identically in every service.

The relay and the consumer are both "run until cancelled" coroutines, and a
FastAPI lifespan that starts them has to also cancel them and *wait* for the
cancellation to land. Skipping the wait is the common bug: uvicorn exits while
aiokafka is mid-shutdown, and the broker holds the consumer group's membership
open until the session timeout, so the next start of the service sits through a
rebalance for no reason.

Three copies of that would be three chances to get it wrong, so it lives here.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Coroutine

logger = logging.getLogger(__name__)

# How long to wait for a loop to finish unwinding before giving up on it. Long
# enough for aiokafka to leave the consumer group cleanly, short enough that a
# wedged task cannot hold a Ctrl-C hostage.
SHUTDOWN_TIMEOUT_SECONDS = 10.0


@asynccontextmanager
async def background_loops(*coroutines: Coroutine, service: str):
    """Run the given coroutines for the lifetime of the block.

    Used from a service lifespan:

        async with background_loops(relay.run(), consumer.run(), service="order"):
            yield
    """
    tasks = [asyncio.create_task(coro) for coro in coroutines]
    logger.info("[%s] started %d background loop(s)", service, len(tasks))

    try:
        yield
    finally:
        for task in tasks:
            task.cancel()

        # return_exceptions swallows the CancelledError each task raises back;
        # anything else is a loop that died on its own before shutdown, which is
        # worth seeing in the log.
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=SHUTDOWN_TIMEOUT_SECONDS,
        )
        for result in results:
            if isinstance(result, Exception) and not isinstance(
                result, asyncio.CancelledError
            ):
                logger.error("[%s] background loop ended with: %r", service, result)

        logger.info("[%s] background loops stopped", service)
