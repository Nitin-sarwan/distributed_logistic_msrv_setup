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

    # ── Pricing ───────────────────────────────────────────────────────────
    #
    # Asked for a binding quote on every order creation. The fare rules, the
    # rate card and the quote TTL all live in pricingServices now; this service
    # only knows where to ask and how long to wait.
    pricing_service_url: str = "http://127.0.0.1:8006"

    # Same budget as the address lookup, for the same reason: the customer is on
    # a spinner, so a slow answer is a failed one.
    pricing_service_timeout_seconds: float = 2.0

    # ── Live tracking ─────────────────────────────────────────────────────
    #
    # The driver's position is read from partnerServices on each tracking poll.
    # Short timeout: a customer's map refreshing a few seconds late is fine, a
    # page hanging on it is not.
    partner_service_url: str = "http://127.0.0.1:8002"
    partner_service_timeout_seconds: float = 2.0

    # A position older than this is shown as "last seen", not as live. The app
    # heartbeats every 60s, so three missed beats means the phone is not
    # reporting.
    tracking_stale_seconds: int = 180

    # ── Payment ───────────────────────────────────────────────────────────
    #
    # Opened on every confirm. Whether online payments are simulated, and the
    # UPI ID drivers show, are paymentServices' settings, not this service's.
    payment_service_url: str = "http://127.0.0.1:8007"
    payment_service_timeout_seconds: float = 2.0

    # ── What to do when Dispatch finds nobody ─────────────────────────────
    #
    # Dispatch reports `dispatch.search_failed`; this decides what the customer
    # experiences, because that policy belongs to the service that owns the
    # order rather than to the one that ran the search.
    #
    # True cancels the order with a reason, which releases the payment
    # authorisation and tells the customer to try again. False leaves it in
    # SEARCHING_PARTNER for an operator to deal with — reasonable only if
    # someone is actually watching.
    cancel_on_dispatch_failure: bool = True

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
