from pathlib import Path
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> dispatchServices -> services -> src -> repo root
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    # Connection params are shared with the rest of the stack (same cluster).
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str
    db_password: str

    # This service owns its own database, like every other service here. It
    # holds attempts and offers — the record of *how* a partner was chosen,
    # which is Dispatch's business and nobody else's. It holds no orders and no
    # partners.
    dispatch_db_name: str = "dispatch_db"

    # ── The one service Dispatch calls ────────────────────────────────────
    #
    # Direct service URL, not the gateway. This is a server-to-server call on
    # the internal network: routing it through the gateway would add a hop and
    # require Dispatch to hold somebody's session credential in order to act on
    # its own behalf.
    partner_service_url: str = "http://127.0.0.1:8002"

    # Shared secret for /internal/* on partnerServices, and for this service's
    # own. Empty disables the check — acceptable on a local machine only.
    internal_api_key: str = ""

    # The availability search and the claim. Short, because both sit in a loop
    # that a driver is waiting on: a slow answer costs the whole offer its TTL.
    partner_service_timeout_seconds: float = 3.0

    # ── The offer loop ────────────────────────────────────────────────────
    #
    # How long a driver has to answer before the offer is withdrawn and the next
    # candidate gets it. This is the single most user-visible number in the
    # service: too short and drivers cannot reach their phone, too long and a
    # customer waits through a queue of people who were never going to answer.
    offer_ttl_seconds: int = 30

    # How far to look, and how many candidates to ask partnerServices for. Both
    # are passed to /internal/partners/available, which has its own defaults —
    # these override them, because "how wide should the search be" is Dispatch's
    # policy rather than the partner service's.
    search_radius_km: float = 5.0
    search_limit: int = 20

    # If the first radius finds nobody, widen and look again. A delivery from a
    # thin part of the city is worth a second look before it is refused.
    search_widen_factor: float = 2.0
    search_max_radius_km: float = 15.0

    # Two independent give-up conditions. Whichever comes first ends the search
    # with `dispatch.search_failed`.
    #
    # The deadline is what a customer experiences; the offer cap is what stops a
    # dense area from cycling through fifty drivers who are all mid-delivery.
    search_deadline_seconds: int = 180
    max_offers_per_order: int = 10

    # How long to wait before re-running the availability search when it came
    # back empty. Hammering partnerServices every tick while nobody is on duty
    # is the obvious failure mode of a loop like this.
    empty_search_backoff_seconds: int = 15

    # ── The worker loop ───────────────────────────────────────────────────
    #
    # How often the background loop looks for work: offers to expire, and
    # attempts waiting for their next candidate. It bounds how late an expiry
    # can be, so it wants to be well under the offer TTL.
    worker_tick_seconds: float = 1.0

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
            f"@{self.db_host}:{self.db_port}/{self.dispatch_db_name}"
        )


settings = Settings()
