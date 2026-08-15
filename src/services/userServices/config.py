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
