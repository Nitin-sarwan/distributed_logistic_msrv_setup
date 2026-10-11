"""The offer loop. Everything this service exists to do is in this file.

Dispatch answers one question - *who gets this order?* - and it is a policy
question, not a fact about the fleet. partnerServices already answers "who could
take this?"; the difference between the two is offers, timeouts, declines,
races, and giving up, and all of that lives here. Putting it inside
partnerServices instead would mean never being able to change matching strategy
without redeploying the service that owns driver accounts.

Three properties the loop has to have:

* **One driver at a time.** Broadcasting a job to five drivers means four of
  them tap accept and four of them are told no. `uq_dispatch_offers_one_live`
  enforces it in the database.
* **Restartable at any instant.** The ranked candidate list, who has been asked,
  what they said, and how long the live offer has left are all rows. Kill the
  process mid-search and the next tick resumes exactly where it stopped.
* **Correct under a race.** The claim on partnerServices is the referee - its
  state machine lets one ONLINE -> ON_TRIP through and 409s the rest - and every
  method that touches an attempt takes the attempt's row lock first, in that
  order, so the worker and a driver's accept cannot deadlock.

Methods come in two shapes, and the split is not stylistic:

* `apply_*` stages work and does **not** commit. Kafka handlers call these,
  because a handler's work and its `processed_events` marker must be one
  transaction - commit them separately and a crash in between makes the
  redelivery do it twice.
* Everything else owns its transaction, because an HTTP route or the worker
  loop has nothing else to commit alongside.
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.services.dispatchServices.clients import partner_client
from src.services.dispatchServices.config import settings
from src.services.dispatchServices.domain.states import (
    AttemptStatus,
    OfferStatus,
    SearchFailureReason,
)
from src.services.dispatchServices.events import payloads, publisher
from src.services.dispatchServices.models.attempt_model import DispatchAttempt
from src.services.dispatchServices.models.offer_model import DispatchOffer
from src.services.dispatchServices.repositories.dispatch_repositories import (
    DispatchRepository,
)
from src.services.dispatchServices.utils.exceptions import (
    OfferClosedError,
    OfferExpiredError,
    OfferNotFoundError,
    PartnerClaimConflictError,
    PartnerServiceUnavailableError,
)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _decimal(value: Any, default: str = "0") -> Decimal:
    """Parse a number off an event payload.

    Everything numeric crosses the wire as a string - see `json_safe` in the
    envelope - precisely so that money and coordinates do not go through a
    float on the way. Undoing that here with `float()` would defeat the point.
    """
    if value is None:
        return Decimal(default)
    return Decimal(str(value))


class DispatchService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = DispatchRepository(db)

    # ── What Order causes ─────────────────────────────────────────────────

    def apply_order_confirmed(
        self, order_id: int, data: dict[str, Any], correlation_id: str | None = None
    ) -> DispatchAttempt | None:
        """Start searching. Stages; does not commit.

        Everything the search needs is copied out of the event. Dispatch never
        calls Order back - that is what makes the two independently restartable,
        and it is why `order.confirmed` carries coordinates and weight rather
        than only an id.

        Returns None if a search already exists for this order, which is the
        normal outcome of a redelivery.
        """
        existing = self.repository.find_attempt_for_order(order_id)
        if existing is not None:
            logger.info("order %s already has attempt %s", order_id, existing.id)
            return None

        pickup = data.get("pickup") or {}
        drop = data.get("drop") or {}
        now = _now()

        attempt = DispatchAttempt(
            order_id=order_id,
            public_ref=data.get("public_ref") or str(order_id),
            customer_id=int(data["customer_id"]),
            status=AttemptStatus.SEARCHING.value,
            pickup_latitude=_decimal(pickup.get("latitude")),
            pickup_longitude=_decimal(pickup.get("longitude")),
            pickup_city=pickup.get("city"),
            drop_city=drop.get("city"),
            vehicle_type=data["vehicle_type"],
            package_weight_kg=_decimal(data.get("package_weight_kg")),
            distance_km=(
                _decimal(data["distance_km"]) if data.get("distance_km") else None
            ),
            amount=_decimal(data["amount"]) if data.get("amount") else None,
            currency=data.get("currency") or "INR",
            radius_km=Decimal(str(settings.search_radius_km)),
            # Absolute, so a restart does not hand the search a fresh clock.
            deadline_at=now + timedelta(seconds=settings.search_deadline_seconds),
            correlation_id=correlation_id,
        )

        self.repository.add_attempt(attempt)
        # Flush, not commit: the event payload needs the attempt id, and both
        # rows still have to land or fail together.
        self.db.flush()

        publisher.publish(
            self.db,
            publisher.DISPATCH_SEARCH_STARTED,
            order_id,
            payloads.search_started(attempt),
            correlation_id,
        )

        logger.info(
            "search started for order %s (attempt %s), deadline %s",
            order_id,
            attempt.id,
            attempt.deadline_at.isoformat(),
        )
        return attempt

    def apply_order_cancelled(
        self, order_id: int, correlation_id: str | None = None
    ) -> None:
        """The order went away. Stop offering, and hand back any claimed driver.

        Stages; does not commit. The release is an HTTP call made inside the
        caller's transaction, which is deliberate: if partnerServices cannot be
        reached, the whole handler fails and Kafka redelivers it, rather than
        leaving a driver marked ON_TRIP for a delivery that no longer exists.
        """
        attempt = self.repository.find_attempt_for_order(order_id)
        if attempt is None:
            # Cancelled before it was ever confirmed, so no search ever ran.
            return

        if attempt.status in (AttemptStatus.CANCELLED.value, AttemptStatus.FAILED.value):
            return

        partner_id = attempt.assigned_partner_id
        if partner_id is not None:
            partner_client.release(partner_id)
            publisher.publish(
                self.db,
                publisher.PARTNER_RELEASED,
                order_id,
                payloads.partner_released(attempt, partner_id, "order_cancelled"),
                correlation_id or attempt.correlation_id,
            )

        # Every open offer is closed as SUPERSEDED rather than declined. Nobody
        # rejected anything - the job was withdrawn, and counting it against a
        # driver's acceptance rate would be wrong.
        self._close_open_offers(attempt)

        attempt.status = AttemptStatus.CANCELLED.value
        attempt.ended_at = _now()
        logger.info("attempt %s cancelled with the order", attempt.id)

    def apply_order_delivered(
        self, order_id: int, correlation_id: str | None = None
    ) -> None:
        """The delivery finished. Put the driver back in the pool.

        Stages; does not commit. ON_TRIP -> ONLINE is the last thing that has to
        happen for a driver to be offered another job, and it is Dispatch's to
        do because Dispatch is what claimed them.
        """
        attempt = self.repository.find_attempt_for_order(order_id)
        if attempt is None or attempt.assigned_partner_id is None:
            return

        partner_id = attempt.assigned_partner_id
        partner_client.release(partner_id)
        publisher.publish(
            self.db,
            publisher.PARTNER_RELEASED,
            order_id,
            payloads.partner_released(attempt, partner_id, "delivered"),
            correlation_id or attempt.correlation_id,
        )
        logger.info("partner %s released after order %s", partner_id, order_id)

    # ── The worker's two jobs ─────────────────────────────────────────────

    def expire_due_offers(self) -> int:
        """Withdraw offers whose countdown ran out. Commits per offer.

        Separate from `advance` because an expiry is time-driven and an advance
        is state-driven, and because a slow candidate search for one order must
        not delay the expiry of another order's offer.
        """
        now = _now()
        expired = 0

        for offer_id in self.repository.expired_live_offers(now):
            if self._expire_one(offer_id, now):
                expired += 1

        return expired

    def _expire_one(self, offer_id: int, now: datetime) -> bool:
        offer = self.db.get(DispatchOffer, offer_id)
        if offer is None:
            return False

        # Attempt first, then offer. Every path in this file takes the locks in
        # that order; taking them the other way round here is how the worker and
        # a driver's accept would deadlock.
        attempt = self.repository.lock_attempt(offer.attempt_id)
        offer = self.repository.lock_offer(offer_id)

        if attempt is None or offer is None:
            self.db.rollback()
            return False

        # Somebody accepted while we were waiting on the lock. Their commit
        # already moved the offer; nothing to expire.
        if offer.status != OfferStatus.OFFERED.value:
            self.db.rollback()
            return False

        self._reject_offer(attempt, offer, OfferStatus.EXPIRED, now)
        self.db.commit()

        logger.info(
            "offer %s to partner %s timed out on order %s",
            offer.id,
            offer.partner_id,
            attempt.order_id,
        )
        return True

    def advance(self, attempt_id: int) -> None:
        """Move one search forward by one step. Commits.

        Exactly one of: end it, do nothing because an offer is live, load
        candidates, or make the next offer. Doing at most one thing per tick is
        what keeps the loop's behaviour readable in the log.
        """
        attempt = self.repository.lock_attempt(attempt_id)
        if attempt is None or attempt.status != AttemptStatus.SEARCHING.value:
            self.db.rollback()
            return

        now = _now()

        # 1. Out of time.
        if now >= attempt.deadline_at:
            self._fail(attempt, self._deadline_reason(attempt), now)
            self.db.commit()
            return

        # 2. Somebody is looking at it. Their TTL is handled by expire_due_offers.
        if self.repository.live_offer(attempt.id) is not None:
            self.db.rollback()
            return

        # 3. Nobody queued: find some, unless we are backing off or capped.
        offer = self.repository.next_queued_offer(attempt.id)
        if offer is None:
            if attempt.retry_search_after and now < attempt.retry_search_after:
                self.db.rollback()
                return

            if attempt.offers_made >= settings.max_offers_per_order:
                self._fail(attempt, SearchFailureReason.ALL_DECLINED, now)
                self.db.commit()
                return

            try:
                found = self._load_candidates(attempt)
            except PartnerServiceUnavailableError:
                # Not fatal to the search. partnerServices being down for a few
                # seconds should cost this order a few seconds, not a driver.
                attempt.retry_search_after = now + timedelta(
                    seconds=settings.empty_search_backoff_seconds
                )
                self.db.commit()
                return
            except IntegrityError:
                # `uq_dispatch_offers_attempt_partner` caught a candidate the
                # exclude set missed, which means two ticks overlapped on this
                # attempt. Abandon the whole transaction rather than trying to
                # carry on without the row lock the rollback just dropped; the
                # next tick reads a consistent list and retries.
                self.db.rollback()
                logger.warning("candidate insert raced for attempt %s", attempt_id)
                return

            if found == 0:
                self._widen_or_wait(attempt, now)
                self.db.commit()
                return

            offer = self.repository.next_queued_offer(attempt.id)

        # 4. Ask the next candidate.
        if offer is not None:
            self._make_offer(attempt, offer, now)

        self.db.commit()

    # ── What a driver does ────────────────────────────────────────────────

    def accept(self, partner_id: int, offer_id: int) -> DispatchOffer:
        """A driver takes the job. Commits.

        The order of operations is the whole thing:

        1. Lock the attempt, then re-read the offer under that lock. Anything
           the worker or another request did is now visible.
        2. Refuse an offer that is not live, or whose TTL has passed. Checking
           the clock here as well as in the sweep is what stops a driver tapping
           at T+0.2s from taking a job already handed to somebody else.
        3. **Claim the driver on partnerServices.** This is the referee. A 409
           means somebody else got them, and the correct answer to the driver is
           "that one's gone", not an error.
        4. Only then record the acceptance and announce it.

        Doing (3) after (4) would let two orders be assigned the same driver and
        leave the second to be unpicked by hand.
        """
        offer = self.repository.find_offer_for_partner(offer_id, partner_id)
        if offer is None:
            raise OfferNotFoundError()

        attempt = self.repository.lock_attempt(offer.attempt_id)
        offer = self.repository.lock_offer(offer_id)
        if attempt is None or offer is None:
            self.db.rollback()
            raise OfferNotFoundError()

        now = _now()
        self._require_live(offer, attempt, now)

        try:
            partner_client.claim(partner_id)
        except PartnerClaimConflictError:
            # Not this driver's fault, and recorded so that it never counts
            # against them. The search continues with the next candidate on the
            # worker's next tick.
            self._reject_offer(attempt, offer, OfferStatus.LOST_RACE, now)
            self.db.commit()
            raise
        except PartnerServiceUnavailableError:
            # Nothing was claimed and nothing is recorded. The offer is still
            # live and the driver can tap again.
            self.db.rollback()
            raise

        offer.status = OfferStatus.ACCEPTED.value
        offer.responded_at = now

        attempt.status = AttemptStatus.ASSIGNED.value
        attempt.assigned_partner_id = partner_id
        attempt.assigned_at = now
        attempt.ended_at = now

        # Everyone else in the queue was never asked, so nothing is rejected -
        # the list is simply closed.
        self._close_open_offers(attempt, skip_offer_id=offer.id)

        publisher.publish(
            self.db,
            publisher.PARTNER_ASSIGNED,
            attempt.order_id,
            payloads.partner_assigned(attempt, offer),
            attempt.correlation_id,
        )

        self.db.commit()
        self.db.refresh(offer)

        logger.info(
            "partner %s accepted order %s (offer %s, rank %s of %s asked)",
            partner_id,
            attempt.order_id,
            offer.id,
            offer.rank,
            attempt.offers_made,
        )
        return offer

    def decline(self, partner_id: int, offer_id: int) -> DispatchOffer:
        """A driver says no. Commits.

        An ordinary outcome, not an error. The next candidate gets the job on
        the worker's next tick - within a second, not at the end of the TTL,
        which is the reason a decline button is worth having at all.
        """
        offer = self.repository.find_offer_for_partner(offer_id, partner_id)
        if offer is None:
            raise OfferNotFoundError()

        attempt = self.repository.lock_attempt(offer.attempt_id)
        offer = self.repository.lock_offer(offer_id)
        if attempt is None or offer is None:
            self.db.rollback()
            raise OfferNotFoundError()

        now = _now()
        self._require_live(offer, attempt, now)

        self._reject_offer(attempt, offer, OfferStatus.DECLINED, now)
        self.db.commit()
        self.db.refresh(offer)

        logger.info("partner %s declined order %s", partner_id, attempt.order_id)
        return offer

    def current_offer(self, partner_id: int) -> tuple[DispatchOffer, DispatchAttempt] | None:
        """What is on this driver's screen right now, with the job's details."""
        offer = self.repository.current_offer_for_partner(partner_id)
        if offer is None:
            return None

        attempt = self.repository.find_attempt(offer.attempt_id)
        if attempt is None:
            return None
        return offer, attempt

    # ── Internals ─────────────────────────────────────────────────────────

    def _require_live(
        self, offer: DispatchOffer, attempt: DispatchAttempt, now: datetime
    ) -> None:
        """Refuse anything that is not a live offer on a live search.

        Four distinct answers, because a driver deserves to be told which of
        them happened: gone, expired, already answered, and withdrawn all look
        the same from the phone otherwise.
        """
        if offer.status == OfferStatus.EXPIRED.value:
            self.db.rollback()
            raise OfferExpiredError()

        if offer.status != OfferStatus.OFFERED.value:
            self.db.rollback()
            raise OfferClosedError()

        if offer.expires_at is not None and offer.expires_at <= now:
            # The sweep has not reached it yet. Expire it here rather than
            # letting a late tap through - and commit, so the worker's next tick
            # moves to the next candidate immediately.
            self._reject_offer(attempt, offer, OfferStatus.EXPIRED, now)
            self.db.commit()
            raise OfferExpiredError()

        if attempt.status != AttemptStatus.SEARCHING.value:
            self.db.rollback()
            raise OfferClosedError("This delivery is no longer available")

    def _reject_offer(
        self,
        attempt: DispatchAttempt,
        offer: DispatchOffer,
        status: OfferStatus,
        now: datetime,
    ) -> None:
        """Close one offer as a rejection and announce it. Stages only.

        The three rejection statuses - declined, expired, lost_race - are kept
        apart all the way onto the topic. Analytics needs them separate: a
        driver who never answers is an operations problem, one who declines is a
        pricing signal, and one who lost a race is neither.
        """
        offer.status = status.value
        offer.responded_at = now

        publisher.publish(
            self.db,
            publisher.PARTNER_REJECTED,
            attempt.order_id,
            payloads.partner_rejected(attempt, offer, status.value),
            attempt.correlation_id,
        )

    def _close_open_offers(
        self, attempt: DispatchAttempt, skip_offer_id: int | None = None
    ) -> None:
        """Retire every offer that can no longer become an assignment.

        Silent: no `partner.rejected` for any of them. A QUEUED candidate was
        never told the job existed, and a live offer withdrawn because the order
        was cancelled is not a rejection by anybody.
        """
        for offer in self.repository.open_offers(attempt.id):
            if skip_offer_id is not None and offer.id == skip_offer_id:
                continue
            offer.status = OfferStatus.SUPERSEDED.value

    def _load_candidates(self, attempt: DispatchAttempt) -> int:
        """Ask partnerServices who is nearby, and queue them. Stages only.

        Returns how many *new* candidates were queued. Drivers already on this
        attempt's list are filtered out, so a widened re-search adds only people
        the first radius could not see - re-asking someone who already declined
        would be both rude and useless.
        """
        exclude = self.repository.already_offered_partner_ids(attempt.id)
        remaining = settings.max_offers_per_order - attempt.offers_made
        if remaining <= 0:
            return 0

        candidates = partner_client.find_available(
            latitude=attempt.pickup_latitude,
            longitude=attempt.pickup_longitude,
            vehicle_type=attempt.vehicle_type,
            min_capacity=attempt.package_weight_kg,
            radius_km=float(attempt.radius_km),
            limit=settings.search_limit,
        )

        existing = self.repository.offers_for_attempt(attempt.id)
        rank = max((offer.rank for offer in existing), default=0)
        added = 0

        for candidate in candidates:
            if candidate.partner_id in exclude:
                continue
            if added >= remaining:
                break

            rank += 1
            added += 1
            self.repository.add_offer(
                DispatchOffer(
                    attempt_id=attempt.id,
                    order_id=attempt.order_id,
                    partner_id=candidate.partner_id,
                    partner_name=candidate.name,
                    partner_phone=candidate.phone,
                    vehicle_id=candidate.vehicle_id,
                    vehicle_number=candidate.vehicle_number,
                    vehicle_type=candidate.vehicle_type,
                    distance_km=candidate.distance_km,
                    rank=rank,
                    status=OfferStatus.QUEUED.value,
                )
            )

        # Flush rather than commit, and let an IntegrityError out to `advance`.
        # Rolling back here would silently drop the attempt's row lock while the
        # caller still believed it held one.
        self.db.flush()

        logger.info(
            "attempt %s: %d candidate(s) queued at %.1fkm",
            attempt.id,
            added,
            float(attempt.radius_km),
        )
        return added

    def _widen_or_wait(self, attempt: DispatchAttempt, now: datetime) -> None:
        """Nobody in range. Look further next time, or wait and look again.

        Widening is bounded by SEARCH_MAX_RADIUS_KM, because past some distance
        the nearest driver is not a useful answer - twenty minutes away is worse
        for the customer than being told to try again.
        """
        max_radius = Decimal(str(settings.search_max_radius_km))

        if attempt.radius_km < max_radius:
            widened = attempt.radius_km * Decimal(str(settings.search_widen_factor))
            attempt.radius_km = min(widened, max_radius)
            logger.info(
                "attempt %s: nobody in range, widening to %.1fkm",
                attempt.id,
                float(attempt.radius_km),
            )

        attempt.retry_search_after = now + timedelta(
            seconds=settings.empty_search_backoff_seconds
        )

    def _deadline_reason(self, attempt: DispatchAttempt) -> SearchFailureReason:
        """Why the search is ending, in the terms a customer will hear.

        Three genuinely different situations, and Notification renders three
        different messages from them. Collapsing them into "no driver found"
        would tell somebody nobody was available when in fact eight people said
        no.
        """
        if attempt.offers_made == 0:
            return SearchFailureReason.NO_CANDIDATES
        return SearchFailureReason.DEADLINE_EXCEEDED

    def _fail(
        self,
        attempt: DispatchAttempt,
        reason: SearchFailureReason,
        now: datetime,
    ) -> None:
        """End the search with nobody, and say so. Stages only."""
        live = self.repository.live_offer(attempt.id)
        if live is not None:
            # Somebody was still looking at it when the clock ran out. That is
            # an expiry, and it is announced as one.
            self._reject_offer(attempt, live, OfferStatus.EXPIRED, now)

        self._close_open_offers(attempt)

        attempt.status = AttemptStatus.FAILED.value
        attempt.failure_reason = reason.value
        attempt.ended_at = now

        publisher.publish(
            self.db,
            publisher.DISPATCH_SEARCH_FAILED,
            attempt.order_id,
            payloads.search_failed(attempt, reason.value),
            attempt.correlation_id,
        )

        logger.warning(
            "attempt %s failed for order %s: %s after %d offer(s)",
            attempt.id,
            attempt.order_id,
            reason.value,
            attempt.offers_made,
        )

    def _make_offer(
        self, attempt: DispatchAttempt, offer: DispatchOffer, now: datetime
    ) -> None:
        """Put the job on one driver's screen. Stages only."""
        expires = now + timedelta(seconds=settings.offer_ttl_seconds)

        offer.status = OfferStatus.OFFERED.value
        offer.offered_at = now
        # Never let an offer outlive the search. A driver counting down 30
        # seconds on a job that will be cancelled in 8 is being lied to.
        offer.expires_at = min(expires, attempt.deadline_at)

        attempt.offers_made += 1

        publisher.publish(
            self.db,
            publisher.PARTNER_OFFERED,
            attempt.order_id,
            payloads.partner_offered(attempt, offer),
            attempt.correlation_id,
        )

        logger.info(
            "offered order %s to partner %s (rank %s, %.1fkm away, expires %s)",
            attempt.order_id,
            offer.partner_id,
            offer.rank,
            float(offer.distance_km),
            offer.expires_at.isoformat(),
        )
