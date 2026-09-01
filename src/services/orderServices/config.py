from pathlib import Path
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> orderServices -> services -> src -> repo root
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    # Connection params are shared with the rest of the stack (same cluster).
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str
    db_password: str

    # This service owns its own database, like every other service here.
    order_db_name: str = "order_db"

    # ── Services this one calls ───────────────────────────────────────────
    #
    # Direct service URLs, not the gateway. These are server-to-server calls on
    # the internal network: routing them through the gateway would add a hop and
    # require this service to hold a *user's* session credential in order to
    # talk to another service on its own behalf.
    user_service_url: str = "http://127.0.0.1:8001"

    # Shared secret for /internal/* on other services, and for this one's own.
    # Empty disables the check — acceptable on a local machine only.
    internal_api_key: str = ""

    # How long to wait for the address lookup. The customer is on a spinner, so
    # a slow answer is a failed one.
    user_service_timeout_seconds: float = 2.0

    # ── Pricing, until the Pricing service exists ─────────────────────────
    #
    # A flat base plus a per-kilometre rate on the straight-line distance. It is
    # deliberately crude: the point of the stub is that the order flow can be
    # built and demonstrated before anyone argues about tariffs, and that
    # deleting it later touches one file.
    pricing_base_fare: float = 30.0
    pricing_per_km: float = 12.0
    pricing_minimum_fare: float = 45.0
    pricing_currency: str = "INR"

    # A quote is a price at a moment. Past this, the order must be re-quoted
    # rather than silently honoured at a stale fare.
    quote_ttl_minutes: int = 15

    # ── Payment, until the Payment service exists ─────────────────────────
    #
    # True lets `POST /orders/{id}/confirm` authorise its own payment, so the
    # flow reaches CONFIRMED without a provider. MUST be false anywhere real:
    # it is, literally, free deliveries.
    payment_autoconfirm: bool = True

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
            f"@{self.db_host}:{self.db_port}/{self.order_db_name}"
        )


settings = Settings()
