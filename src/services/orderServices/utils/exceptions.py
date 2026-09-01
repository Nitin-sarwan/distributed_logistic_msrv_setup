"""Domain errors.

Each one maps to exactly one HTTP status at the edge, and the routes are the
only place that mapping happens — a service that raises HTTPException has put
transport knowledge in the business layer.
"""


class OrderError(Exception):
    """Base class, so a route can catch the family when that is the right thing."""

    message = "Something went wrong with this order."

    def __init__(self, message: str | None = None):
        if message:
            self.message = message
        super().__init__(self.message)


class OrderNotFoundError(OrderError):
    """No such order, or not this customer's. The caller gets 404 either way."""

    message = "Order not found"


class AddressNotFoundError(OrderError):
    """The address id does not belong to this customer, or does not exist."""

    message = "That delivery address could not be found"


class AddressServiceUnavailableError(OrderError):
    """The User service could not be reached to resolve an address.

    Distinct from AddressNotFoundError on purpose: one is the customer's
    problem to fix, the other is ours, and they answer 404 and 503.
    """

    message = "Could not confirm your delivery address. Please try again."


class InvalidTransitionError(OrderError):
    """The order is not in a state where this makes sense."""

    message = "This order cannot be changed from its current state"


class QuoteExpiredError(OrderError):
    message = "That fare estimate has expired. Please get a new one."


class NotYourOrderError(OrderError):
    """A partner acting on an order that is not assigned to them."""

    message = "This delivery is not assigned to you"
