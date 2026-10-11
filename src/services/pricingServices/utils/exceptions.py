"""Domain errors.

Each one maps to exactly one HTTP status at the edge, and the routes are the
only place that mapping happens — a service that raises HTTPException has put
transport knowledge in the business layer.
"""


class PricingError(Exception):
    """Base class, so a route can catch the family when that is the right thing."""

    message = "This delivery could not be priced."

    def __init__(self, message: str | None = None):
        if message:
            self.message = message
        super().__init__(self.message)


class UnknownVehicleTypeError(PricingError):
    """No tariff exists for this vehicle type at all."""

    message = "We do not offer that vehicle type"


class VehicleUnavailableError(PricingError):
    """A tariff exists but has been switched off by operations."""

    message = "That vehicle type is not available right now"


class WeightExceedsVehicleError(PricingError):
    """The parcel is heavier than this class of vehicle can carry.

    Refused here rather than left for Dispatch to discover, because Dispatch
    would discover it as "no driver found" three minutes later — after the
    customer had paid.
    """

    def __init__(self, vehicle_label: str, max_weight: str):
        super().__init__(
            f"A {vehicle_label} carries up to {max_weight} kg. "
            "Choose a larger vehicle."
        )


class QuoteNotFoundError(PricingError):
    message = "Quote not found"
