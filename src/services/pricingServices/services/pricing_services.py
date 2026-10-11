"""Business rules for prices.

Two ways to ask what a delivery costs, and the difference between them is the
design of this service:

* **An estimate** is public, anonymous, and unrecorded. A visitor on the home
  page drags a pin and sees every vehicle's price before they have an account.
  Nothing is stored, nothing is promised.
* **A quote** is internal, issued to Order for one order, and stored. It is the
  number the customer agrees to pay and it expires.

Both go through `_price`, so the price a visitor sees and the price they are
charged cannot be computed two different ways.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from src.services.pricingServices.config import settings
from src.services.pricingServices.domain.fare import (
    VEHICLE_LABELS,
    Fare,
    compute_fare,
    exceeds_capacity,
    format_kg,
    haversine_km,
    road_distance_km,
)
from src.services.pricingServices.models.quote_model import Quote
from src.services.pricingServices.models.tariff_model import Tariff
from src.services.pricingServices.repositories.pricing_repositories import (
    PricingRepository,
)
from src.services.pricingServices.utils.exceptions import (
    QuoteNotFoundError,
    UnknownVehicleTypeError,
    VehicleUnavailableError,
    WeightExceedsVehicleError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Trip:
    pickup_latitude: Decimal
    pickup_longitude: Decimal
    drop_latitude: Decimal
    drop_longitude: Decimal

    def straight_line_km(self) -> Decimal:
        return haversine_km(
            float(self.pickup_latitude),
            float(self.pickup_longitude),
            float(self.drop_latitude),
            float(self.drop_longitude),
        )


@dataclass(frozen=True)
class EstimateOption:
    """One vehicle's line on the estimate.

    An overweight or switched-off vehicle is still listed, with the reason, so
    the booking screen can show "Two wheeler — up to 30 kg" greyed out rather
    than have it silently vanish when the weight field changes.
    """

    tariff: Tariff
    fare: Fare
    available: bool
    unavailable_reason: str | None


@dataclass(frozen=True)
class Estimate:
    straight_line_km: Decimal
    distance_km: Decimal
    weight_kg: Decimal
    options: list[EstimateOption]


class PricingService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = PricingRepository(db)

    # ── Reads ─────────────────────────────────────────────────────────────

    def tariffs(self, active_only: bool = True) -> list[Tariff]:
        return self.repository.list_tariffs(active_only=active_only)

    def get_quote(self, quote_id: str) -> Quote:
        quote = self.repository.find_quote(quote_id)
        if quote is None:
            raise QuoteNotFoundError()
        return quote

    # ── Estimate: every vehicle, nothing stored ───────────────────────────

    def estimate(self, trip: Trip, weight_kg: Decimal | None) -> Estimate:
        weight = (
            weight_kg
            if weight_kg is not None
            else Decimal(str(settings.estimate_default_weight_kg))
        )
        straight = trip.straight_line_km()
        distance = road_distance_km(straight, settings.road_distance_factor)

        options = []
        for tariff in self.repository.list_tariffs(active_only=False):
            reason = None
            if not tariff.is_active:
                reason = "Not available right now"
            elif exceeds_capacity(tariff.rates(), weight):
                reason = f"Carries up to {format_kg(tariff.max_weight_kg)} kg"

            options.append(
                EstimateOption(
                    tariff=tariff,
                    fare=compute_fare(tariff.rates(), distance, weight),
                    available=reason is None,
                    unavailable_reason=reason,
                )
            )

        return Estimate(
            straight_line_km=straight,
            distance_km=distance,
            weight_kg=weight,
            options=options,
        )

    # ── Quote: one vehicle, stored, binding until it expires ──────────────

    def issue_quote(
        self,
        trip: Trip,
        vehicle_type: str,
        weight_kg: Decimal,
        customer_id: int | None = None,
    ) -> Quote:
        """Price one delivery and write the price down.

        Refuses rather than prices anything that could not actually be
        delivered — an inactive vehicle, or a parcel too heavy for it. A quote
        Order cannot fulfil is worse than no quote: it gets paid for.
        """
        tariff = self.repository.find_tariff(vehicle_type)
        if tariff is None:
            raise UnknownVehicleTypeError()
        if not tariff.is_active:
            raise VehicleUnavailableError()

        rates = tariff.rates()
        if exceeds_capacity(rates, weight_kg):
            raise WeightExceedsVehicleError(
                VEHICLE_LABELS.get(vehicle_type, vehicle_type),
                format_kg(tariff.max_weight_kg),
            )

        straight = trip.straight_line_km()
        distance = road_distance_km(straight, settings.road_distance_factor)
        fare = compute_fare(rates, distance, weight_kg)

        quote = Quote(
            id=str(uuid.uuid4()),
            customer_id=customer_id,
            tariff_id=tariff.id,
            vehicle_type=vehicle_type,
            pickup_latitude=trip.pickup_latitude,
            pickup_longitude=trip.pickup_longitude,
            drop_latitude=trip.drop_latitude,
            drop_longitude=trip.drop_longitude,
            weight_kg=weight_kg,
            straight_line_km=straight,
            distance_km=distance,
            base_fare=fare.base_fare,
            distance_charge=fare.distance_charge,
            weight_charge=fare.weight_charge,
            minimum_fare_adjustment=fare.minimum_fare_adjustment,
            amount=fare.amount,
            currency=tariff.currency,
            expires_at=datetime.now(timezone.utc)
            + timedelta(minutes=settings.quote_ttl_minutes),
        )

        self.repository.add_quote(quote)
        self.db.commit()
        self.db.refresh(quote)

        logger.info(
            "quoted %s %s for %s, %s km, %s kg (customer %s)",
            quote.amount, quote.currency, vehicle_type, distance, weight_kg, customer_id,
        )
        return quote

    # ── Operations ────────────────────────────────────────────────────────

    def update_tariff(self, vehicle_type: str, changes: dict) -> Tariff:
        """Edit a rate card in place. Issued quotes keep their own numbers."""
        tariff = self.repository.find_tariff(vehicle_type)
        if tariff is None:
            raise UnknownVehicleTypeError()

        for field, value in changes.items():
            setattr(tariff, field, value)

        self.db.commit()
        self.db.refresh(tariff)

        logger.warning("tariff for %s changed: %s", vehicle_type, changes)
        return tariff
