from pathlib import Path
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> pricingServices -> services -> src -> repo root
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

    # This service owns its own database, like every other service here. It
    # holds the rate card and every quote ever issued — the record of what a
    # customer was told a delivery would cost, and on what tariff.
    pricing_db_name: str = "pricing_db"

    # Shared secret for /internal/*. Empty disables the check — acceptable on a
    # local machine only.
    internal_api_key: str = ""

    # ── How a fare is computed ────────────────────────────────────────────
    #
    # Nothing here asks a routing engine which roads exist, so the distance a
    # fare is charged on is the straight line scaled up by this factor. 1.3 is
    # the usual urban detour ratio; it is a setting rather than a constant
    # because the day a real router is wired in, it becomes 1.0.
    road_distance_factor: float = 1.3

    # A quote is a price at a moment. Past this, Order must re-quote rather than
    # silently honour a stale fare.
    quote_ttl_minutes: int = 15

    # What the public estimate assumes when the visitor has not said how heavy
    # the parcel is yet. Light enough to never trip a weight surcharge, so the
    # "from" price on the home page is never higher than the real one.
    estimate_default_weight_kg: float = 1.0

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
            f"@{self.db_host}:{self.db_port}/{self.pricing_db_name}"
            + (f"?sslmode={self.db_sslmode}" if self.db_sslmode else "")
        )


settings = Settings()
