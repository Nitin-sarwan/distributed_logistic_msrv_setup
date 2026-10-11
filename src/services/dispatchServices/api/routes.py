"""The driver's three calls. Everything else Dispatch does is events and a loop.

Mounted at `/api/dispatch`, which the gateway routes with the **partner** cookie
- see the routing table in `src/gateway/main.py`. A customer's session on these
paths is rejected here as well as there, because a service reachable directly
must not be authenticated by a header some proxy set.

There is no endpoint to *request* a driver, and there never will be. Dispatch
learns that an order needs one from `order.confirmed` on the topic; a customer
who could call Dispatch directly could dispatch an order they had not paid for.

Polling, not push, for now. `GET /offers/current` every few seconds is correct,
cheap, and easy to replace - and the shape of the answer does not change when
`partner.offered` starts driving a real push through Notification. The transport
decision is open; the contract is not.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from src.services.dispatchServices.api.dependencies import get_current_partner_id
from src.services.dispatchServices.api.schema import (
    DeclineRequest,
    OfferCard,
    OfferResponse,
)
from src.services.dispatchServices.database.connection import get_db
from src.services.dispatchServices.services.dispatch_services import DispatchService
from src.services.dispatchServices.utils.exceptions import (
    OfferClosedError,
    OfferExpiredError,
    OfferNotFoundError,
    PartnerClaimConflictError,
    PartnerServiceUnavailableError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dispatch", tags=["dispatch"])


def get_service(db: Session = Depends(get_db)) -> DispatchService:
    return DispatchService(db)


@router.get(
    "/offers/current",
    response_model=OfferCard | None,
    status_code=status.HTTP_200_OK,
)
def current_offer(
    response: Response,
    partner_id: int = Depends(get_current_partner_id),
    service: DispatchService = Depends(get_service),
):
    """What is on this driver's screen right now, or nothing.

    `null` is the answer most of the time and it is a 200, not a 404: "no offer
    for you at the moment" is a successful poll, and an app that treated it as
    an error would be showing failures all day.

    A driver can only ever have one. Claiming a partner moves them to ON_TRIP,
    which removes them from every other availability search, so two dispatches
    cannot both be holding the same person.
    """
    # Polled every few seconds and the answer changes constantly. A cached one
    # is worse than no answer at all - it is an offer the driver cannot accept.
    response.headers["Cache-Control"] = "no-store"

    found = service.current_offer(partner_id)
    if found is None:
        return None

    offer, attempt = found
    remaining = (offer.expires_at - datetime.now(timezone.utc)).total_seconds()

    return OfferCard(
        offer_id=offer.id,
        order_id=attempt.order_id,
        public_ref=attempt.public_ref,
        pickup_city=attempt.pickup_city,
        drop_city=attempt.drop_city,
        vehicle_type=attempt.vehicle_type,
        package_weight_kg=attempt.package_weight_kg,
        trip_distance_km=attempt.distance_km,
        pickup_distance_km=offer.distance_km,
        amount=attempt.amount,
        currency=attempt.currency,
        expires_at=offer.expires_at,
        # Never negative. The sweep may not have reached an offer that ran out a
        # moment ago, and a countdown showing -3 is a bug report.
        seconds_remaining=max(0, int(remaining)),
    )


@router.post(
    "/offers/{offer_id}/accept",
    response_model=OfferResponse,
    status_code=status.HTTP_200_OK,
)
def accept_offer(
    offer_id: int,
    partner_id: int = Depends(get_current_partner_id),
    service: DispatchService = Depends(get_service),
):
    """Take the job.

    Four ways this legitimately does not succeed, and they are four different
    statuses because they are four different things to tell a driver:

    * **404** - no such offer, or not theirs. The same answer either way, which
      is the only thing they should be able to learn.
    * **410 Gone** - the countdown ran out. The job existed; they were too slow.
    * **409 Conflict** - somebody else was claimed first, or they had already
      answered this one. Nothing went wrong and nothing is their fault.
    * **503** - partnerServices could not be reached, so nothing was claimed and
      nothing recorded. The offer is still live; tapping again works.
    """
    try:
        offer = service.accept(partner_id, offer_id)
    except OfferNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.message
        ) from error
    except OfferExpiredError as error:
        # 410 rather than 409: the resource was here and is now permanently
        # gone, which is exactly what Gone means and what the app should show.
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail=error.message
        ) from error
    except (OfferClosedError, PartnerClaimConflictError) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=error.message
        ) from error
    except PartnerServiceUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=error.message
        ) from error

    return OfferResponse(
        offer_id=offer.id,
        order_id=offer.order_id,
        status=offer.status,
        responded_at=offer.responded_at,
    )


@router.post(
    "/offers/{offer_id}/decline",
    response_model=OfferResponse,
    status_code=status.HTTP_200_OK,
)
def decline_offer(
    offer_id: int,
    payload: DeclineRequest | None = None,
    partner_id: int = Depends(get_current_partner_id),
    service: DispatchService = Depends(get_service),
):
    """Say no.

    Worth an endpoint of its own rather than letting the offer time out: a
    decline moves the job to the next driver within a second instead of thirty,
    and thirty seconds per uninterested driver is most of why a customer waits.

    A decline is an ordinary outcome and is never held against anybody. It is
    recorded distinctly from a timeout because the two mean different things -
    one is a choice, the other is a phone in a pocket.
    """
    if payload is not None and payload.reason:
        logger.info(
            "partner %s declined offer %s: %s", partner_id, offer_id, payload.reason
        )

    try:
        offer = service.decline(partner_id, offer_id)
    except OfferNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.message
        ) from error
    except OfferExpiredError as error:
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail=error.message
        ) from error
    except OfferClosedError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=error.message
        ) from error

    return OfferResponse(
        offer_id=offer.id,
        order_id=offer.order_id,
        status=offer.status,
        responded_at=offer.responded_at,
    )
