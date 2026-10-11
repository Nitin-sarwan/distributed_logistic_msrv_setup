"""Outbox rows to Kafka. The only thing in the platform that produces messages.

One relay runs inside each producing service as a background task, started in
that service's lifespan. It is the second half of the outbox pattern: the
publisher wrote a row inside the business transaction, and this drains it.

Three properties it has to have, and how each is obtained:

* **Nothing is lost.** A row stays unpublished until the broker has acknowledged
  it. A crash anywhere in this file leaves the row exactly as it was.
* **Order is preserved per aggregate.** Rows go out in id order and the batch
  *stops* at the first failure. Skipping a failed row to make progress on later
  ones would deliver `order.delivered` before `order.picked_up`.
* **Duplicates are possible and that is accepted.** The window is: broker
  acknowledges, process dies, `published_at` never written, row is sent again on
  restart. Closing it needs a transaction across Postgres and Kafka, which is
  the thing this pattern exists to avoid. Consumers deduplicate instead.

The database work runs in a worker thread. SQLAlchemy's Session is synchronous,
and calling it directly from a coroutine blocks the event loop that is also
serving HTTP.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from aiokafka import AIOKafkaProducer
from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from src.common.events.config import kafka_settings
from src.common.events.types import TOPIC

logger = logging.getLogger(__name__)

# Backoff for connecting to a broker that is not up yet. Starts fast, because
# the usual case is `docker compose up` finishing a few seconds after uvicorn
# did; caps low enough that a broker coming back is picked up promptly rather
# than after a coffee.
RECONNECT_MIN_SECONDS = 2.0
RECONNECT_MAX_SECONDS = 30.0


class OutboxRelay:
    """Drains one service's outbox onto the topic.

    Constructed with the service's session factory and its own Outbox model,
    which is all that differs between the services running one.
    """

    def __init__(
        self,
        session_factory: sessionmaker,
        outbox_model: type,
        service_name: str,
    ):
        self._session_factory = session_factory
        self._outbox = outbox_model
        self._service = service_name
        self._producer: AIOKafkaProducer | None = None

    # -- The loop ---------------------------------------------------------

    async def run(self) -> None:
        """Poll forever. Cancelled by the lifespan on shutdown."""
        if not kafka_settings.kafka_enabled:
            logger.warning(
                "[%s relay] KAFKA_ENABLED is false - events will queue in the "
                "outbox and never be delivered.",
                self._service,
            )
            return

        try:
            await self._connect()

            while True:
                try:
                    sent = await self._cycle()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Never let the loop die. An unhandled exception here stops
                    # every event this service will ever emit, silently.
                    logger.exception("[%s relay] cycle failed", self._service)
                    sent = 0

                # Drain a backlog at full speed; idle politely when there is
                # nothing to do.
                if sent == 0:
                    await asyncio.sleep(kafka_settings.outbox_poll_seconds)
        except asyncio.CancelledError:
            logger.info("[%s relay] stopping", self._service)
            raise
        finally:
            if self._producer is not None:
                await self._producer.stop()
                self._producer = None

    async def _connect(self) -> None:
        """Keep trying until the broker is there.

        A service must start whether or not Kafka is up — that is the whole
        point of having an outbox. Failing here instead would mean the order of
        `docker compose up` and `uvicorn` mattered, and that starting a service
        thirty seconds too early left it permanently without a relay while
        appearing healthy.

        Events queue in the table the entire time and go out in order once this
        returns.
        """
        delay = RECONNECT_MIN_SECONDS
        attempt = 0

        while True:
            attempt += 1
            try:
                await self._start_producer()
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "[%s relay] broker unreachable at %s (attempt %d): %s — "
                    "events are queueing in the outbox, retrying in %.0fs",
                    self._service,
                    kafka_settings.kafka_bootstrap_servers,
                    attempt,
                    error,
                    delay,
                )
                if self._producer is not None:
                    # start() can leave a half-open client behind.
                    try:
                        await self._producer.stop()
                    except Exception:
                        pass
                    self._producer = None

                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_SECONDS)

    async def _start_producer(self) -> None:
        self._producer = AIOKafkaProducer(
            bootstrap_servers=kafka_settings.bootstrap_servers,
            # Every in-sync replica must have the record before it counts as
            # sent. On a single broker this is the same as acks=1; it is set
            # explicitly so adding brokers later does not quietly weaken it.
            acks="all",
            # Kafka deduplicates a producer's own retries, so a network blip
            # does not put the same record on the partition twice. That is the
            # broker-side half; the consumer-side half is still
            # processed_events, because the relay process itself can restart.
            enable_idempotence=True,
            value_serializer=lambda value: json.dumps(value).encode("utf-8"),
            key_serializer=lambda key: key.encode("utf-8"),
        )
        await self._producer.start()
        logger.info(
            "[%s relay] producing to %s on %s",
            self._service,
            TOPIC,
            kafka_settings.kafka_bootstrap_servers,
        )

    async def _cycle(self) -> int:
        """One poll: fetch a batch, send until something fails, mark what went."""
        batch = await asyncio.to_thread(self._fetch_batch)
        if not batch:
            return 0

        published: list[int] = []

        for row_id, event_id, event_type, aggregate_id, payload in batch:
            try:
                await self._producer.send_and_wait(
                    TOPIC,
                    key=aggregate_id,
                    value=payload,
                )
            except Exception as error:
                logger.warning(
                    "[%s relay] send failed for %s (event %s): %s",
                    self._service,
                    event_type,
                    event_id,
                    error,
                )
                await asyncio.to_thread(self._record_failure, row_id, str(error))
                # Stop here. Later rows may be for the same order, and putting
                # them on the partition ahead of this one reorders the delivery.
                break

            published.append(row_id)
            logger.info(
                "[%s relay] published %s for order %s (event %s)",
                self._service,
                event_type,
                aggregate_id,
                event_id,
            )

        if published:
            await asyncio.to_thread(self._mark_published, published)

        return len(published)

    # -- Database, on a worker thread -------------------------------------

    def _fetch_batch(self) -> list[tuple]:
        """Unpublished rows, oldest first, that still have retries left.

        No row locking. A second relay for the same service - two reload
        workers overlapping, say - would publish the same rows twice, and that
        is the duplicate consumers already handle. Locking here would buy
        nothing the deduplication table does not already give.
        """
        with self._session_factory() as db:
            rows = db.execute(
                select(
                    self._outbox.id,
                    self._outbox.event_id,
                    self._outbox.event_type,
                    self._outbox.aggregate_id,
                    self._outbox.payload,
                )
                .where(
                    self._outbox.published_at.is_(None),
                    self._outbox.attempts < kafka_settings.outbox_max_attempts,
                )
                .order_by(self._outbox.id)
                .limit(kafka_settings.outbox_batch_size)
            ).all()
        return [tuple(row) for row in rows]

    def _mark_published(self, row_ids: list[int]) -> None:
        with self._session_factory() as db:
            db.execute(
                update(self._outbox)
                .where(self._outbox.id.in_(row_ids))
                .values(published_at=datetime.now(timezone.utc), last_error=None)
            )
            db.commit()

    def _record_failure(self, row_id: int, error: str) -> None:
        """Count the attempt so a permanently broken row stops retrying.

        It is never deleted and never marked published: it stays visible in the
        table, which is where you look when a customer says nothing happened.
        """
        with self._session_factory() as db:
            db.execute(
                update(self._outbox)
                .where(self._outbox.id == row_id)
                .values(
                    attempts=self._outbox.attempts + 1,
                    # Truncated: a driver traceback can be enormous and the
                    # column is here for triage, not forensics.
                    last_error=error[:1000],
                )
            )
            db.commit()
