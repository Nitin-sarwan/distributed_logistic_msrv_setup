from pathlib import Path
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> partnerServices -> services -> src -> repo root
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    # Connection params are shared with the rest of the stack (same cluster).
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str
    db_password: str

    # This service owns its own database. Neither DB_NAME nor USER_DB_NAME is
    # read here — a partner row must never live in another service's schema.
    partner_db_name: str = "partner_db"

    # Password hardening and the token cipher. Same values as userServices reads,
    # because they are properties of the .env, not of either service: the pepper
    # and the salts have to agree across every process that hashes or decrypts.
    static_salt: str
    static_pepper: str
    pass_salt_static: str
    secret_key: str = "aes-256-cbc"

    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 30

    # ── Sessions ──────────────────────────────────────────────────────────
    #
    # Partner sessions land in the same shared Mongo collection as user
    # sessions, and `user` in that document is only an integer id. Partner 5 and
    # user 5 are different people, so every write and every bulk revoke carries
    # this discriminator — without it, a user logging out everywhere would knock
    # a partner offline mid-delivery.
    partner_app_type: int = 2

    # ── Session cookie ────────────────────────────────────────────────────
    #
    # Deliberately a different name from userServices' `lp_session`. Cookies are
    # keyed by name and domain, so if the partner dashboard and the customer app
    # are ever served from the same host, one login would silently overwrite the
    # other's credential.
    session_cookie_secure: bool = False
    session_cookie_samesite: str = "lax"

    # ── Dispatch-facing search defaults ───────────────────────────────────

    partner_search_radius_km: float = 5.0
    partner_search_limit: int = 20

    # A partner who stopped sending heartbeats is not "available" however green
    # their status column says they are — a phone that lost signal ten minutes
    # ago still reads as `online`. Dispatch must not route an order to a pin
    # that stale.
    partner_location_stale_minutes: int = 5

    # Shared secret for the /internal routes. Those are not exposed through the
    # gateway at all (it only forwards /api/*), so this is defence in depth
    # against anything that reaches the service port directly. Empty disables
    # the check, which is acceptable only on a local machine.
    internal_api_key: str = ""

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
            f"@{self.db_host}:{self.db_port}/{self.partner_db_name}"
        )


settings = Settings()
