"""Domain errors.

Each one maps to exactly one HTTP status at the edge, and the routes are the
only place that mapping happens - a service that raises HTTPException has put
transport knowledge in the business layer.
"""


class DispatchError(Exception):
    """Base class, so a route can catch the family when that is the right thing."""

    message = "Something went wrong dispatching this delivery."

    def __init__(self, message: str | None = None):
        if message:
            self.message = message
        super().__init__(self.message)


class OfferNotFoundError(DispatchError):
    """No such offer, or not this partner's. The caller gets 404 either way."""

    message = "That delivery offer is no longer available"


class OfferExpiredError(DispatchError):
    """The driver answered after the TTL ran out.

    Distinct from OfferNotFoundError because the driver deserves a distinct
    message: the job existed and they were too slow, which is worth saying
    plainly rather than pretending it never existed.
    """

    message = "This offer has expired"


class OfferClosedError(DispatchError):
    """Already answered - usually a double tap on a slow connection."""

    message = "You have already responded to this offer"


class PartnerClaimConflictError(DispatchError):
    """partnerServices refused the claim: somebody else got this driver first.

    Not the driver's fault and not an error in this service. It is the
    concurrency control working - see PLATFORM_WORKFLOW.md on the 409.
    """

    message = "This delivery was just taken by another partner"


class PartnerServiceUnavailableError(DispatchError):
    """partnerServices could not be reached to search or to claim."""

    message = "Could not reach the partner service. Please try again."
