from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchor the .env lookup to the project root so it resolves no matter
# which directory the app is launched from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    database_url: str

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
