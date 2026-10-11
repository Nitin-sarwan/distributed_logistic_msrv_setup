"""Operations reads, and one manual override.

Mounted at `/internal/dispatch`, deliberately outside `/api`. The gateway routes
`/api/*` and nothing else, so these paths do not exist as far as the public
internet is concerned. `require_internal_key` is the second lock, for anything
that can reach the service port directly.

This is where "why did this delivery take four minutes to get a driver?" is
answered - the question Dispatch owns a database in order to be able to answer.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from src.services.dispatchServices.api.dependencies import require_internal_key
from src.services.dispatchServices.api.schema import AttemptResponse, OfferSummary
from src.services.dispatchServices.database.connection import get_db
from src.services.dispatchServices.domain.states import AttemptStatus
from src.services.dispatchServices.models.attempt_model import DispatchAttempt
from src.services.dispatchServices.repositories.dispatch_repositories import (
    DispatchRepository,
)
from src.services.dispatchServices.services.dispatch_services import DispatchService

router = APIRouter(
    prefix="/internal/dispatch",
    tags=["internal"],
    # Applied to every route in the file rather than to each one, so a new
    # endpoint is protected by default instead of by memory.
    dependencies=[Depends(require_internal_key)],
)


def _to_response(
    attempt: DispatchAttempt, repository: DispatchRepository
) -> AttemptResponse:
    return AttemptResponse(
        id=attempt.id,
        order_id=attempt.order_id,
        public_ref=attempt.public_ref,
        customer_id=attempt.customer_id,
        status=attempt.status,
        vehicle_type=attempt.vehicle_type,
        package_weight_kg=attempt.package_weight_kg,
        radius_km=attempt.radius_km,
        offers_made=attempt.offers_made,
        deadline_at=attempt.deadline_at,
        assigned_partner_id=attempt.assigned_partner_id,
        assigned_at=attempt.assigned_at,
        failure_reason=attempt.failure_reason,
        ended_at=attempt.ended_at,
        correlation_id=attempt.correlation_id,
        created_at=attempt.created_at,
        offers=[
            OfferSummary.model_validate(offer)
            for offer in repository.offers_for_attempt(attempt.id)
        ],
    )


@router.get("/attempts", response_model=list[AttemptResponse])
def list_attempts(
    status_filter: AttemptStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Recent searches, newest first.

    `?status=searching` is the one an operator lives in: everything currently
    hunting for a driver, which is the queue to watch when the platform feels
    slow.
    """
    repository = DispatchRepository(db)
    attempts = repository.recent_attempts(
        limit=limit, status=status_filter.value if status_filter else None
    )
    return [_to_response(attempt, repository) for attempt in attempts]


@router.get("/orders/{order_id}", response_model=AttemptResponse)
def get_attempt_for_order(order_id: int, db: Session = Depends(get_db)):
    """One order's search and every offer it made.

    The support answer in a single call: who was asked, in what order, how far
    away each was, and what each of them did.
    """
    repository = DispatchRepository(db)
    attempt = repository.find_attempt_for_order(order_id)
    if attempt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No dispatch attempt for that order",
        )
    return _to_response(attempt, repository)


@router.post("/orders/{order_id}/cancel", response_model=AttemptResponse)
def cancel_attempt(order_id: int, db: Session = Depends(get_db)):
    """Call off a search by hand, and release any claimed driver.

    **A manual override, not part of the flow.** Cancellation normally reaches
    Dispatch as `order.cancelled` on the topic; this exists for the case where
    an operator has to stop a runaway search without touching the order - and
    for driving the behaviour without a broker running.

    It does not cancel the order. Dispatch does not own the order and must not
    pretend to: the customer's order stays exactly as it was, which is the point
    of the two being separate services.
    """
    repository = DispatchRepository(db)
    attempt = repository.find_attempt_for_order(order_id)
    if attempt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No dispatch attempt for that order",
        )

    DispatchService(db).apply_order_cancelled(order_id)
    db.commit()
    db.refresh(attempt)

    return _to_response(attempt, repository)
