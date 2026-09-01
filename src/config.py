from pathlib import Path
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchor the .env lookup to the project root so it resolves no matter
# which directory the app is launched from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str

    # Shared session store, common to every service.
    mongo_uri: str
    mongo_db_name: str = "common"
    mongo_sessions_collection: str = "sessions"

    # Downstream services the gateway forwards to.
    user_service_url: str = "http://127.0.0.1:8001"
    partner_service_url: str = "http://127.0.0.1:8002"

    # /api/geo is served by userServices today, so this defaults to the same
    # host. It is a separate setting rather than a reuse of user_service_url so
    # that standing geocoding up as its own process is an env change, not a code
    # change.
    geo_service_url: str = "http://127.0.0.1:8001"
    order_service_url: str = "http://127.0.0.1:8003"
    gateway_timeout_seconds: float = 30.0

    # Browser origins allowed to call the gateway, comma separated.
    #
    # Must be an explicit list, never "*": the frontend sends credentialed
    # requests, and browsers reject a wildcard Allow-Origin whenever
    # Allow-Credentials is true. Listing origins is also what stops any random
    # site from making authenticated calls on a signed-in user's behalf.
    cors_allow_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origins(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.cors_allow_origins.split(",")
            if origin.strip()
        ]

    @property
    def database_url(self) -> str:
        # Percent-encode credentials so characters that are structural in a URL
        # (@ : / ? #) can appear in a password without breaking the parse.
        user = quote(self.db_user, safe="")
        password = quote(self.db_password, safe="")
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


settings = Settings()
