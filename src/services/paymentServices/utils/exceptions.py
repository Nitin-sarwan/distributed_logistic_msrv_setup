"""Domain errors. Each maps to one HTTP status, in the routes and nowhere else."""


class PaymentError(Exception):
    message = "Something went wrong with this payment."

    def __init__(self, message: str | None = None):
        if message:
            self.message = message
        super().__init__(self.message)


class PaymentNotFoundError(PaymentError):
    """No payment for that order, or not this driver's delivery. 404 either way,
    so a driver cannot learn which order ids exist."""

    message = "Payment not found"


class NotCollectableError(PaymentError):
    """The driver may not collect on this order: it was paid online, or it was
    cancelled. 409 — nothing is wrong, there is just nothing to collect."""

    message = "There is nothing to collect on this delivery"
