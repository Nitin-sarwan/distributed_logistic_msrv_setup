"""The closed vocabularies this service uses.

Stored as varchar with CHECK constraints rather than Postgres ENUMs, like the
rest of the platform: a native enum cannot grow inside a transaction.
"""

from enum import Enum


class PaymentMethod(str, Enum):
    """How the customer chose to pay, at booking.

    ONLINE      paid up front, before a driver is sought.
    ON_DELIVERY paid to the driver at drop-off, in cash or over UPI.
    """

    ONLINE = "online"
    ON_DELIVERY = "on_delivery"


class PaymentStatus(str, Enum):
    """Where the money is.

    AWAITING  nothing received yet — an on-delivery order before drop-off, or an
              online one waiting for a provider.
    PAID      received. Online, or collected by the driver.
    REFUNDED  was paid, then the order was cancelled and the money returned.
    VOIDED    was never paid, and now never will be: the order was cancelled.
    """

    AWAITING = "awaiting"
    PAID = "paid"
    REFUNDED = "refunded"
    VOIDED = "voided"


class CollectedVia(str, Enum):
    """How the money actually arrived. Set when the status becomes PAID."""

    ONLINE = "online"
    CASH = "cash"
    UPI = "upi"


# What a driver may record at the door. ONLINE is the customer paying the
# platform directly, which is never something a driver says happened.
DRIVER_COLLECTABLE = {CollectedVia.CASH, CollectedVia.UPI}
