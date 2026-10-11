from pathlib import Path
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> userServices -> services -> src -> repo root
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    # Connection params are shared with the rest of the stack (same cluster).
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str
    db_password: str

    # This service owns its own database. DB_NAME in the shared .env belongs to
    # other services, so it is deliberately not read here — override with
    # USER_DB_NAME if this service ever moves to its own cluster.
    user_db_name: str = "user_db"

    # Password hardening. The pepper is the security-relevant one: it lives only
    # here, never in the DB, so a stolen users table cannot be cracked offline.
    static_salt: str
    static_pepper: str

    # Token cipher, kept byte-compatible with the Node encryptData/decryptData.
    # SECRET_KEY holds the OpenSSL algorithm name, not a key.
    pass_salt_static: str
    secret_key: str = "aes-256-cbc"

    # Access tokens are AES-256-CBC encrypted per-user with that user's
    # token_secret, so rotating a row's token_secret invalidates only that
    # user's tokens.
    access_token_expire_minutes: int = 60

    # Refresh tokens live far longer, so they are stored as their own session
    # and can be revoked independently of the access token they mint.
    refresh_token_expire_days: int = 30

    password_reset_expire_minutes: int = 30

    # Sessions are one shared Mongo collection and `user` in those documents is
    # a bare integer id, so user 5 here and partner 5 in partnerServices share a
    # key. Every session this service writes carries this discriminator, and
    # every bulk revoke filters on it — otherwise "log out everywhere" would
    # reach across services and sign out a stranger.
    user_app_type: int = 1

    # ── Session cookie ────────────────────────────────────────────────────
    # Login and register also return the access token as an HttpOnly cookie, so
    # a browser holds a credential JavaScript cannot read. The same token is
    # still returned in the body for non-browser clients.

    # Restricts the cookie to HTTPS. MUST be true anywhere real — over plain
    # HTTP the cookie is readable by anyone on the network, which defeats the
    # point of making it HttpOnly. False by default only so local development
    # over http:// works without certificates.
    session_cookie_secure: bool = False

    # "lax" blocks the cookie on cross-site POSTs, which is what stops another
    # site from making authenticated calls on a signed-in user's behalf. Only
    # loosen to "none" if the frontend is on a genuinely different registrable
    # domain, and add CSRF tokens if you do — "none" removes this protection.
    session_cookie_samesite: str = "lax"

    # Shared secret for /internal/*, which the gateway does not route. The
    # Order service presents it when resolving an address to snapshot. Empty
    # disables the check — local machines only.
    internal_api_key: str = ""

    # ── Geocoding ─────────────────────────────────────────────────────────
    # Powers /api/geo — the address search and reverse lookup behind every map
    # in the frontend. Nothing here is secret; the provider is a public service.

    # Swap this and utils/geocoder.py's mapping to change provider. Nothing else
    # in the codebase knows which one is in use.
    geocoder_base_url: str = "https://nominatim.openstreetmap.org"

    # Nominatim's usage policy requires a User-Agent that identifies the
    # application, and blocks traffic without one. Set this to something that
    # names your deployment before running anywhere real.
    geocoder_user_agent: str = "distributed-logistic/0.1 (local development)"

    # Sent as the `From` header. How the provider reaches an operator before
    # resorting to a block. Empty omits the header.
    geocoder_contact_email: str = ""

    # ISO country codes to restrict search to, comma separated. "in" matches the
    # rest of this system's assumptions — six-digit PIN codes and ten-digit
    # phone numbers. Empty searches the whole world.
    geocoder_country_codes: str = "in"

    geocoder_timeout_seconds: float = 8.0

    # The floor between two calls to the provider, process-wide. Nominatim's
    # policy is one request per second; going faster gets an IP blocked, which
    # takes address search down for everyone.
    geocoder_min_interval_seconds: float = 1.0

    # A day. Addresses do not move, so a long TTL is both safe and the main
    # reason the throttle above is rarely reached.
    geocoder_cache_ttl_seconds: int = 86_400
    geocoder_cache_max_entries: int = 2_000

    geocoder_search_limit: int = 5

    # Per-IP quota for /api/geo, which is public. See api/geo_routes.py for what
    # this does and does not protect against.
    geo_rate_limit_per_minute: int = 30

    # Local dev only: returns the reset token in the API response because there
    # is no mail delivery yet. MUST stay false anywhere real — it hands account
    # takeover to anyone who can guess an email address.
    password_reset_expose_token: bool = False

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
            f"@{self.db_host}:{self.db_port}/{self.user_db_name}"
        )


settings = Settings()
