"""Broker connection settings, read from the same root .env as everything else.

Its own Settings class rather than a field on each service's config, because
the broker is infrastructure in the same category as Mongo: every process needs
the identical value, and four copies of `kafka_bootstrap_servers` is four places
for it to be wrong.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> events -> common -> src -> repo root
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class KafkaSettings(BaseSettings):
    # Matches docker-compose.yml's HOST listener. Services run on the host as
    # uvicorn processes, so this is 127.0.0.1 and not the container name.
    kafka_bootstrap_servers: str = "127.0.0.1:9092"

    # Turning this off makes every service start without a broker: the relay
    # keeps queueing to the outbox and consumers do not run. Useful for working
    # on an HTTP endpoint without Docker open; it means events stop flowing, so
    # nothing downstream of an event will happen.
    kafka_enabled: bool = True

    # ── Relay ─────────────────────────────────────────────────────────────
    #
    # How often the relay looks for unpublished outbox rows. A second is well
    # under any latency a person notices and cheap against a partial index.
    outbox_poll_seconds: float = 1.0
    outbox_batch_size: int = 100
    # After this many failures a row is left alone and logged loudly rather than
    # retried forever at full speed. It stays unpublished, so it is still
    # visible and still recoverable by hand.
    outbox_max_attempts: int = 10

    # ── Consumers ─────────────────────────────────────────────────────────
    #
    # Offsets are committed by the consumer loop after a handler returns, never
    # automatically. Auto-commit acknowledges a message the moment it is
    # fetched, so a crash mid-handler loses it — the exact failure the outbox
    # exists to prevent on the way out.
    consumer_auto_offset_reset: str = "earliest"
    consumer_max_poll_records: int = 20

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def bootstrap_servers(self) -> list[str]:
        return [s.strip() for s in self.kafka_bootstrap_servers.split(",") if s.strip()]


kafka_settings = KafkaSettings()
