"""The two state machines Dispatch owns.

Pure: no SQLAlchemy, no FastAPI, no database. Dispatch's whole job is a loop
with a lot of edge cases — a driver who answers as the timer expires, two
dispatches racing for one partner, an order cancelled mid-offer — and every one
of them is a transition here rather than a branch buried in a route.

Note what these do *not* describe: the order's state, or the partner's. Those
belong to the services that own them. An attempt can be `assigned` while Order
still says `searching_partner`, for the second it takes the event to arrive, and
that is not an inconsistency to design away.
"""

from enum import Enum


class AttemptStatus(str, Enum):
    """One order's search for a driver, start to finish.

    Subclasses `str`, so a Pydantic response serialises the value and a
    comparison against a raw string from the database still works.
    """

    # Looking. Either an offer is live, or the next candidate is about to get
    # one, or the search is backing off before widening.
    SEARCHING = "searching"

    # A driver accepted and was successfully claimed. Terminal.
    ASSIGNED = "assigned"

    # Nobody accepted before the deadline, or nobody was ever available.
    # Terminal, and announced — Order decides what the customer sees.
    FAILED = "failed"

    # The order went away underneath the search. Terminal.
    CANCELLED = "cancelled"


ATTEMPT_TERMINAL: frozenset[AttemptStatus] = frozenset(
    {AttemptStatus.ASSIGNED, AttemptStatus.FAILED, AttemptStatus.CANCELLED}
)


class OfferStatus(str, Enum):
    """One job shown to one driver.

    QUEUED exists so that a candidate list is *persisted* rather than held in
    memory. Restart the service mid-search and the loop resumes from the table:
    it knows who has already been asked, who is next, and how long the current
    offer has left. A list in a local variable would silently start the whole
    search again, re-offering to drivers who already declined.
    """

    # Ranked and waiting. The driver has not been told anything.
    QUEUED = "queued"

    # Live, on the driver's screen, counting down to `expires_at`.
    OFFERED = "offered"

    # Taken. At most one per attempt, and it ends the search.
    ACCEPTED = "accepted"

    # The driver said no.
    DECLINED = "declined"

    # The driver said nothing before the TTL ran out. Not the same as DECLINED,
    # and worth keeping apart: a partner who never answers is a different
    # problem from one who answers no, and only the first is an ops issue.
    EXPIRED = "expired"

    # The driver accepted, but partnerServices refused the claim with a 409 —
    # somebody else got them first. Their fault in no sense; recorded distinctly
    # so it never counts against them.
    LOST_RACE = "lost_race"

    # Never offered: the attempt ended while this candidate was still queued.
    SUPERSEDED = "superseded"


# An offer that is still capable of becoming an assignment.
OFFER_OPEN: frozenset[OfferStatus] = frozenset(
    {OfferStatus.QUEUED, OfferStatus.OFFERED}
)

# Why an offer did not become an assignment. These are the values that travel on
# `partner.rejected`, where analytics counts them.
OFFER_REJECTION_REASONS: frozenset[OfferStatus] = frozenset(
    {OfferStatus.DECLINED, OfferStatus.EXPIRED, OfferStatus.LOST_RACE}
)


class SearchFailureReason(str, Enum):
    """Why a search ended with nobody.

    Carried on `dispatch.search_failed` so that Order, Notification and
    analytics can tell apart the three genuinely different situations — and so
    the customer is told something true. "No drivers are on duty near you" and
    "we asked eight drivers and all of them declined" are not the same message.
    """

    NO_CANDIDATES = "no_candidates"
    ALL_DECLINED = "all_declined"
    DEADLINE_EXCEEDED = "deadline_exceeded"
