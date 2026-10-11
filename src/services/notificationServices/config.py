from pathlib import Path
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> notificationServices -> services -> src -> repo root
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    # Connection params are shared with the rest of the stack (same cluster).
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str
    db_password: str

    # Hosted Postgres (Neon, Render) requires TLS: set DB_SSLMODE=require there.
    # Empty leaves the URL as it was, for the local cluster.
    db_sslmode: str = ""

    # This service owns its own database. It holds messages and preferences —
    # what was said to whom, and who asked not to be told. It holds no orders,
    # no partners, and no reason why any message was sent.
    notification_db_name: str = "notification_db"

    # Shared secret for /internal/notifications/*, which the gateway does not
    # route. Empty disables the check — acceptable on a local machine only.
    internal_api_key: str = ""

    # ── Channels ──────────────────────────────────────────────────────────
    #
    # In-app is always on: it *is* the stored row, and the feed the frontend
    # reads. These two switch the outbound channels, which are logging stubs
    # until an email worker and a push worker exist behind RabbitMQ.
    #
    # They are on by default so the log shows exactly what a real deployment
    # would send, which is the only way to notice that an event fans out to
    # four messages before it does so to real people.
    email_enabled: bool = True
    push_enabled: bool = True

    # ── Feed ──────────────────────────────────────────────────────────────
    #
    # The badge count is polled constantly. Capped so a neglected account with
    # ten thousand unread rows returns "99+" rather than doing a full count on
    # every poll.
    unread_count_cap: int = 99
    feed_default_limit: int = 30
    feed_max_limit: int = 100

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def database_url(self) -> str:
        # Percent-encode credentials so characters that are structural in a URL
        # (@ : / ? #) can appear in a password without breaking the parse.
        user = quote(self.db_user, safe="")
        password = quote(self.db_password, safe="")
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.notification_db_name}"
            + (f"?sslmode={self.db_sslmode}" if self.db_sslmode else "")
        )


settings = Settings()
