"""Kafka to a handler, with deduplication. The other end of the spine.

Every consuming service builds one of these in its lifespan, registers handlers
for the event types it cares about, and runs it as a background task. The
service's own code is then just functions of `(db, envelope)` - no broker, no
offsets, no retry logic in a business method.

The three things this file exists to get right:

**Deduplication.** Delivery is at-least-once, so the same envelope will arrive
twice. The handler's work and the `processed_events` row are written in one
transaction, so a redelivery finds the marker and does nothing. Two separate
commits would leave a window where the work is done and the marker is not.

**Offsets are committed after the handler returns, never before.** Auto-commit
acknowledges a message when it is fetched, so a crash mid-handler loses it -
the exact failure the outbox prevents on the way out, reintroduced on the way
in.

**A poison message must not stop the partition.** Retrying forever is the
obvious behaviour and the wrong one: every order after the broken one is stuck
behind it. After a bounded number of attempts the envelope goes to the DLQ topic
with the error attached, the offset is committed, and the partition moves on.
Nothing drains the DLQ automatically - it is a place to look.

The handler runs in a worker thread, because SQLAlchemy's Session is
synchronous and the event loop is also serving HTTP.
"""

import asyncio
import json
import logging
import uuid
from typing import Any, Callable

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.common.events.config import kafka_settings
from src.common.events.types import ALL_EVENT_TYPES, DLQ_TOPIC, TOPIC

logger = logging.getLogger(__name__)

# A handler does its work against the session it is given and does NOT commit.
# The consumer commits, once, together with the processed-events marker - which
# is the only way the two can be atomic.
Handler = Callable[[Session, dict[str, Any]], None]

# How many times a failing handler is retried before the envelope is parked in
# the DLQ. Low on purpose: the failures worth retrying are transient (a
# momentary connection loss to another service), and those clear in seconds.
MAX_HANDLER_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0

# Backoff for connecting to a broker that is not up yet, or has just restarted.
# Starts fast, because the usual case is `docker compose up` finishing a few
# seconds after uvicorn did.
RECONNECT_MIN_SECONDS = 2.0
RECONNECT_MAX_SECONDS = 30.0


class AlreadyProcessed(Exception):
    """Raised internally when the deduplication table already has this event."""


class EventConsumer:
    """One consumer group, reading the shared topic for one service."""

    def __init__(
        self,
        group_id: str,
        session_factory: sessionmaker,
        processed_model: type,
        service_name: str,
    ):
        self._group_id = group_id
        self._session_factory = session_factory
        self._processed = processed_model
        self._service = service_name
        self._handlers: dict[str, Handler] = {}
        self._consumer: AIOKafkaConsumer | None = None
        self._dlq: AIOKafkaProducer | None = None

    # -- Registration -----------------------------------------------------

    def on(self, event_type: str, handler: Handler) -> None:
        """Register the handler for one event type.

        An unknown type is a startup error rather than a handler that silently
        never fires - which is what a typo would otherwise be, since every
        service reads every event on the shared topic and skipping unmatched
        ones is normal behaviour.
        """
        if event_type not in ALL_EVENT_TYPES:
            raise ValueError(
                f"{event_type!r} is not a registered event type. Add it to "
                f"src/common/events/types.py first."
            )
        if event_type in self._handlers:
            raise ValueError(f"{event_type!r} already has a handler in {self._service}")
        self._handlers[event_type] = handler

    # -- The loop ---------------------------------------------------------

    async def run(self) -> None:
        """Consume forever. Cancelled by the lifespan on shutdown."""
        if not kafka_settings.kafka_enabled:
            logger.warning(
                "[%s consumer] KAFKA_ENABLED is false - no events will be "
                "consumed by this service.",
                self._service,
            )
            return

        if not self._handlers:
            logger.warning(
                "[%s consumer] no handlers registered, not starting", self._service
            )
            return

        # Reconnect loop. A service must start whether or not Kafka is up, and
        # must recover on its own when a broker restarts — otherwise the order
        # of `docker compose up` and `uvicorn` matters, and a broker blip leaves
        # a service healthy and permanently deaf.
        delay = RECONNECT_MIN_SECONDS

        try:
            while True:
                try:
                    await self._connect()
                    delay = RECONNECT_MIN_SECONDS
                    await self._consume()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.warning(
                        "[%s consumer] connection to %s lost or unavailable: "
                        "%s — retrying in %.0fs",
                        self._service,
                        kafka_settings.kafka_bootstrap_servers,
                        error,
                        delay,
                    )
                    await self._stop()
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, RECONNECT_MAX_SECONDS)
        except asyncio.CancelledError:
            logger.info("[%s consumer] stopping", self._service)
            raise
        finally:
            await self._stop()

    async def _consume(self) -> None:
        """The read loop. Returns only if the consumer is closed under it."""
        async for message in self._consumer:
            try:
                await self._dispatch(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Belt and braces. _dispatch already handles its own failures;
                # anything escaping it must still not kill the loop and stop
                # the service consuming forever.
                logger.exception(
                    "[%s consumer] unhandled error, skipping offset %s",
                    self._service,
                    message.offset,
                )
            # After the handler, never before. See the module docstring.
            await self._consumer.commit()

    async def _connect(self) -> None:
        self._consumer = AIOKafkaConsumer(
            TOPIC,
            bootstrap_servers=kafka_settings.bootstrap_servers,
            group_id=self._group_id,
            # See the module docstring. This is the single most important
            # setting in the file.
            enable_auto_commit=False,
            auto_offset_reset=kafka_settings.consumer_auto_offset_reset,
            max_poll_records=kafka_settings.consumer_max_poll_records,
            value_deserializer=lambda raw: json.loads(raw.decode("utf-8")),
            key_deserializer=lambda raw: raw.decode("utf-8") if raw else None,
        )
        await self._consumer.start()

        self._dlq = AIOKafkaProducer(
            bootstrap_servers=kafka_settings.bootstrap_servers,
            acks="all",
            value_serializer=lambda value: json.dumps(value).encode("utf-8"),
            key_serializer=lambda key: key.encode("utf-8"),
        )
        await self._dlq.start()

        logger.info(
            "[%s consumer] group %s subscribed to %s, handling: %s",
            self._service,
            self._group_id,
            TOPIC,
            ", ".join(sorted(self._handlers)),
        )

    async def _stop(self) -> None:
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None
        if self._dlq is not None:
            await self._dlq.stop()
            self._dlq = None

    # -- One message ------------------------------------------------------

    async def _dispatch(self, message) -> None:
        envelope = message.value
        if not isinstance(envelope, dict):
            logger.warning(
                "[%s consumer] offset %s is not an envelope, skipping",
                self._service,
                message.offset,
            )
            return

        event_type = envelope.get("event_type", "")
        handler = self._handlers.get(event_type)

        if handler is None:
            # Normal and frequent: one topic carries every event and each
            # service cares about a few. Debug level, not a warning.
            logger.debug(
                "[%s consumer] ignoring %s", self._service, event_type or "<untyped>"
            )
            return

        event_id = envelope.get("event_id", "")
        aggregate_id = envelope.get("aggregate_id", "")

        for attempt in range(1, MAX_HANDLER_ATTEMPTS + 1):
            try:
                await asyncio.to_thread(self._run_handler, handler, envelope)
            except AlreadyProcessed:
                logger.info(
                    "[%s consumer] %s (event %s) already processed, skipping",
                    self._service,
                    event_type,
                    event_id,
                )
                return
            except Exception as error:
                logger.warning(
                    "[%s consumer] %s (event %s) attempt %d/%d failed: %s",
                    self._service,
                    event_type,
                    event_id,
                    attempt,
                    MAX_HANDLER_ATTEMPTS,
                    error,
                )
                if attempt == MAX_HANDLER_ATTEMPTS:
                    await self._to_dlq(envelope, error)
                    return
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
            else:
                logger.info(
                    "[%s consumer] handled %s for order %s (event %s)",
                    self._service,
                    event_type,
                    aggregate_id,
                    event_id,
                )
                return

    def _run_handler(self, handler: Handler, envelope: dict[str, Any]) -> None:
        """The handler and its deduplication marker, in one transaction.

        Runs on a worker thread. Raises `AlreadyProcessed` for a redelivery,
        which the caller treats as success - because it is: the work was done
        the first time.
        """
        event_id = uuid.UUID(envelope["event_id"])
        event_type = envelope["event_type"]

        db = self._session_factory()
        try:
            if db.get(self._processed, event_id) is not None:
                raise AlreadyProcessed()

            handler(db, envelope)

            db.add(self._processed(event_id=event_id, event_type=event_type))
            db.commit()
        except AlreadyProcessed:
            db.rollback()
            raise
        except IntegrityError:
            # Two workers took the same message at once and the primary key on
            # processed_events settled it. The other transaction did the work,
            # so this one is done too.
            db.rollback()
            raise AlreadyProcessed() from None
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    async def _to_dlq(self, envelope: dict[str, Any], error: Exception) -> None:
        """Park an envelope that will not process, and let the partition move on.

        The original is nested rather than flattened so a replay is a straight
        re-publish of `envelope`, with the failure metadata alongside it rather
        than mixed into it.
        """
        record = {
            "failed_by": self._service,
            "consumer_group": self._group_id,
            "error": str(error)[:2000],
            "error_type": type(error).__name__,
            "envelope": envelope,
        }
        try:
            await self._dlq.send_and_wait(
                DLQ_TOPIC,
                key=str(envelope.get("aggregate_id", "unknown")),
                value=record,
            )
            logger.error(
                "[%s consumer] gave up on %s (event %s) - sent to %s",
                self._service,
                envelope.get("event_type"),
                envelope.get("event_id"),
                DLQ_TOPIC,
            )
        except Exception:
            # The DLQ is itself unreachable. Log the whole envelope so it is at
            # least recoverable from the service log, and move on: blocking the
            # partition on a failed error-path write helps nobody.
            logger.exception(
                "[%s consumer] could not reach the DLQ. Lost envelope: %s",
                self._service,
                json.dumps(envelope),
            )
