from pathlib import Path
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> paymentServices -> services -> src -> repo root
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    # Connection params are shared with the rest of the stack (same cluster).
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str
    db_password: str

    # This service owns its own database. It holds one payment per order: how
    # the customer chose to pay, whether the money has arrived, and how.
    payment_db_name: str = "payment_db"

    # Shared secret for /internal/*. Empty disables the check — acceptable on a
    # local machine only.
    internal_api_key: str = ""

    # ── Paying online, until a payment provider is connected ──────────────
    #
    # True treats "Pay online now" as paid the moment it is chosen, so the flow
    # can be built and demonstrated without a provider account. MUST be false
    # anywhere real: it is, literally, free deliveries. With it off, an online
    # order waits at payment_pending for a provider that does not exist yet.
    payment_online_autocapture: bool = True

    # ── Paying the driver over UPI ────────────────────────────────────────
    #
    # The business's UPI ID. The QR the driver shows encodes a payment to this
    # address — so it must be a real one, owned by the business, before any
    # customer scans it. The default is a placeholder that no bank will accept.
    payment_upi_vpa: str = "logisticpartner@upi"
    payment_upi_payee_name: str = "LogisticPartner"

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def upi_is_placeholder(self) -> bool:
        return self.payment_upi_vpa == "logisticpartner@upi"

    @property
    def database_url(self) -> str:
        # Percent-encode credentials so characters that are structural in a URL
        # (@ : / ? #) can appear in a password without breaking the parse.
        user = quote(self.db_user, safe="")
        password = quote(self.db_password, safe="")
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.payment_db_name}"
        )


settings = Settings()
