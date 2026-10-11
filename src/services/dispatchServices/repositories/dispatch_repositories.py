"""Data access for attempts and offers. Holds no business rules.

Every partner-facing read is scoped by `partner_id` in the WHERE clause rather
than fetched and checked afterwards. That ordering is the point: a query that
cannot return someone else's offer makes the ownership check impossible to
forget, whereas `find_by_id` followed by `if offer.partner_id != caller` is one
early return away from letting a driver accept a job offered to somebody else.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.services.dispatchServices.domain.states import AttemptStatus, OfferStatus
from src.services.dispatchServices.models.attempt_model import DispatchAttempt
from src.services.dispatchServices.models.offer_model import DispatchOffer


class DispatchRepository:
    def __init__(self, db: Session):
        self.db = db

    # ── Attempts ──────────────────────────────────────────────────────────

    def find_attempt(self, attempt_id: int) -> DispatchAttempt | None:
        return self.db.get(DispatchAttempt, attempt_id)

    def find_attempt_for_order(self, order_id: int) -> DispatchAttempt | None:
        return self.db.scalar(
            select(DispatchAttempt).where(DispatchAttempt.order_id == order_id)
        )

    def searching_attempt_ids(self, limit: int = 200) -> list[int]:
        """Every attempt the worker still has work to do on, oldest first.

        Ids rather than rows: the worker processes them one at a time in its own
        session and transaction, so holding a batch of ORM objects from a
        different session would only invite a stale write.
        """
        return list(
            self.db.scalars(
                select(DispatchAttempt.id)
                .where(DispatchAttempt.status == AttemptStatus.SEARCHING.value)
                .order_by(DispatchAttempt.created_at)
                .limit(limit)
            )
        )

    def lock_attempt(self, attempt_id: int) -> DispatchAttempt | None:
        """Fetch an attempt with a row lock held for the caller's transaction.

        This is what makes the worker and the accept endpoint safe against each
        other. Both mutate the same attempt - one expiring an offer, the other
        taking it - and the second one to arrive must see the first one's work
        rather than a snapshot from before it.

        `SELECT ... FOR UPDATE` blocks rather than skipping, deliberately: the
        loser here should proceed once the winner commits and then discover the
        offer is gone, not silently do nothing.
        """
        return self.db.scalar(
            select(DispatchAttempt)
            .where(DispatchAttempt.id == attempt_id)
            .with_for_update()
        )

    def add_attempt(self, attempt: DispatchAttempt) -> None:
        """Stage an attempt. The caller commits - usually with an outbox row."""
        self.db.add(attempt)

    def recent_attempts(
        self, limit: int = 50, status: str | None = None
    ) -> list[DispatchAttempt]:
        query = select(DispatchAttempt).order_by(DispatchAttempt.created_at.desc())
        if status:
            query = query.where(DispatchAttempt.status == status)
        return list(self.db.scalars(query.limit(limit)))

    # ── Offers ────────────────────────────────────────────────────────────

    def add_offer(self, offer: DispatchOffer) -> None:
        self.db.add(offer)

    def live_offer(self, attempt_id: int) -> DispatchOffer | None:
        """The one offer currently on a driver's screen for this attempt.

        At most one can exist - `uq_dispatch_offers_one_live` is a partial
        unique index that enforces it in the database rather than in the loop.
        """
        return self.db.scalar(
            select(DispatchOffer).where(
                DispatchOffer.attempt_id == attempt_id,
                DispatchOffer.status == OfferStatus.OFFERED.value,
            )
        )

    def next_queued_offer(self, attempt_id: int) -> DispatchOffer | None:
        """The best candidate not yet asked. Nearest first, by rank."""
        return self.db.scalar(
            select(DispatchOffer)
            .where(
                DispatchOffer.attempt_id == attempt_id,
                DispatchOffer.status == OfferStatus.QUEUED.value,
            )
            .order_by(DispatchOffer.rank)
            .limit(1)
        )

    def offers_for_attempt(self, attempt_id: int) -> list[DispatchOffer]:
        return list(
            self.db.scalars(
                select(DispatchOffer)
                .where(DispatchOffer.attempt_id == attempt_id)
                .order_by(DispatchOffer.rank)
            )
        )

    def open_offers(self, attempt_id: int) -> list[DispatchOffer]:
        """Everything still capable of becoming an assignment.

        What gets closed out when an attempt ends, for any of the three reasons
        it can end.
        """
        return list(
            self.db.scalars(
                select(DispatchOffer).where(
                    DispatchOffer.attempt_id == attempt_id,
                    DispatchOffer.status.in_(
                        [OfferStatus.QUEUED.value, OfferStatus.OFFERED.value]
                    ),
                )
            )
        )

    def already_offered_partner_ids(self, attempt_id: int) -> set[int]:
        """Who this attempt has already put on the list, in any state.

        Read before a widened re-search so a driver who declined at 5km is not
        asked the identical question again at 10km. The unique constraint would
        catch it; this makes it a filter rather than an insert failure.
        """
        return set(
            self.db.scalars(
                select(DispatchOffer.partner_id).where(
                    DispatchOffer.attempt_id == attempt_id
                )
            )
        )

    def find_offer_for_partner(
        self, offer_id: int, partner_id: int
    ) -> DispatchOffer | None:
        """One offer belonging to this driver, or None.

        None covers both "no such offer" and "not yours" - the caller answers
        404 either way, so the two are deliberately not distinguished.
        """
        return self.db.scalar(
            select(DispatchOffer).where(
                DispatchOffer.id == offer_id, DispatchOffer.partner_id == partner_id
            )
        )

    def current_offer_for_partner(self, partner_id: int) -> DispatchOffer | None:
        """What is on this driver's screen right now, if anything.

        The partner app's poll. One row at most in practice: a driver who is
        ONLINE can be a candidate for several attempts, but only one dispatch
        can hold them at a time, because claiming moves them to ON_TRIP and
        ON_TRIP takes them out of every other availability search.
        """
        return self.db.scalar(
            select(DispatchOffer)
            .where(
                DispatchOffer.partner_id == partner_id,
                DispatchOffer.status == OfferStatus.OFFERED.value,
            )
            .order_by(DispatchOffer.offered_at.desc())
            .limit(1)
        )

    def expired_live_offers(self, now: datetime, limit: int = 100) -> list[int]:
        """Live offers whose countdown has run out, as ids.

        The worker's sweep. Bounded so one very bad minute cannot make a single
        tick take arbitrarily long.
        """
        return list(
            self.db.scalars(
                select(DispatchOffer.id)
                .where(
                    DispatchOffer.status == OfferStatus.OFFERED.value,
                    DispatchOffer.expires_at <= now,
                )
                .order_by(DispatchOffer.expires_at)
                .limit(limit)
            )
        )

    def lock_offer(self, offer_id: int) -> DispatchOffer | None:
        return self.db.scalar(
            select(DispatchOffer)
            .where(DispatchOffer.id == offer_id)
            .with_for_update()
        )
