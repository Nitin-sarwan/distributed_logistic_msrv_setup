from sqlalchemy import select
from sqlalchemy.orm import Session

from src.services.pricingServices.models.quote_model import Quote
from src.services.pricingServices.models.tariff_model import Tariff


class PricingRepository:
    """Every query this service runs. Nothing above here writes SQL."""

    def __init__(self, db: Session):
        self.db = db

    # ── Tariffs ───────────────────────────────────────────────────────────

    def list_tariffs(self, active_only: bool = False) -> list[Tariff]:
        # Cheapest vehicle first, which is the order a customer reads options in.
        query = select(Tariff).order_by(Tariff.max_weight_kg, Tariff.id)
        if active_only:
            query = query.where(Tariff.is_active.is_(True))
        return list(self.db.scalars(query))

    def find_tariff(self, vehicle_type: str) -> Tariff | None:
        return self.db.scalar(select(Tariff).where(Tariff.vehicle_type == vehicle_type))

    # ── Quotes ────────────────────────────────────────────────────────────

    def add_quote(self, quote: Quote) -> None:
        self.db.add(quote)

    def find_quote(self, quote_id: str) -> Quote | None:
        return self.db.get(Quote, quote_id)
