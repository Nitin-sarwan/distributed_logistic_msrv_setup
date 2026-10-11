"""The driver's three calls: what to collect, the UPI QR, and "they paid".

Mounted at `/api/deliveries/payments`. That nests under the driver's
`/api/deliveries` prefix on purpose: the gateway maps one cookie per prefix, and
the longest prefix wins, so this reaches Payment with the **partner** cookie —
the same arrangement as the driver's notifications.

Customers have no route here. They see payment on their order, which Order
keeps up to date from `payment.captured`.
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from src.services.paymentServices.api.dependencies import get_current_partner_id
from src.services.paymentServices.api.schema import (
    CollectRequest,
    PaymentResponse,
    UpiRequestResponse,
)
from src.services.paymentServices.config import settings
from src.services.paymentServices.database.connection import get_db
from src.services.paymentServices.services.payment_services import PaymentService
from src.services.paymentServices.utils.exceptions import (
    NotCollectableError,
    PaymentNotFoundError,
)

router = APIRouter(prefix="/deliveries/payments", tags=["payments (driver)"])


def get_service(db: Session = Depends(get_db)) -> PaymentService:
    return PaymentService(db)


def _http(error) -> HTTPException:
    if isinstance(error, PaymentNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error.message)
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=error.message)


@router.get("/{order_id}", response_model=PaymentResponse)
def get_payment(
    order_id: int,
    response: Response,
    partner_id: int = Depends(get_current_partner_id),
    service: PaymentService = Depends(get_service),
):
    """Collect or not, and how much. Only for this driver's delivery."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.for_partner(partner_id, order_id)
    except PaymentNotFoundError as error:
        raise _http(error) from error


@router.get("/{order_id}/upi", response_model=UpiRequestResponse)
def get_upi_request(
    order_id: int,
    partner_id: int = Depends(get_current_partner_id),
    service: PaymentService = Depends(get_service),
):
    """The UPI link to show as a QR, with the amount and reference filled in."""
    try:
        payment, link = service.upi_request(partner_id, order_id)
    except (PaymentNotFoundError, NotCollectableError) as error:
        raise _http(error) from error

    return UpiRequestResponse(
        order_id=payment.order_id,
        public_ref=payment.public_ref,
        amount=payment.amount,
        currency=payment.currency,
        upi_link=link,
        payee_vpa=settings.payment_upi_vpa,
        payee_name=settings.payment_upi_payee_name,
    )


@router.post("/{order_id}/collect", response_model=PaymentResponse)
def collect_payment(
    order_id: int,
    payload: CollectRequest,
    partner_id: int = Depends(get_current_partner_id),
    service: PaymentService = Depends(get_service),
):
    """The customer paid the driver. 409 if there is nothing to collect."""
    try:
        return service.collect(partner_id, order_id, payload.via)
    except (PaymentNotFoundError, NotCollectableError) as error:
        raise _http(error) from error
