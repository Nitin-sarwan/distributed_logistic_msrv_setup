"""Order opens a payment on confirm; operations can look one up.

Mounted at `/internal/payments`, outside `/api`, so the gateway never routes it:
a customer who could open their own payment could mark it paid online.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.services.paymentServices.api.dependencies import require_internal_key
from src.services.paymentServices.api.schema import OpenPayment, PaymentResponse
from src.services.paymentServices.database.connection import get_db
from src.services.paymentServices.repositories.payment_repositories import (
    PaymentRepository,
)
from src.services.paymentServices.services.payment_services import PaymentService

router = APIRouter(
    prefix="/internal/payments",
    tags=["internal"],
    dependencies=[Depends(require_internal_key)],
)


@router.post("", response_model=PaymentResponse, status_code=status.HTTP_201_CREATED)
def open_payment(payload: OpenPayment, db: Session = Depends(get_db)):
    """Start an order's payment. Idempotent on order_id.

    The status in the answer is what Order acts on: `paid` confirms the order
    now; `awaiting` on an online payment leaves it at payment_pending; an
    on-delivery payment is always `awaiting`, and confirms the order anyway.
    """
    return PaymentService(db).open_for_order(
        order_id=payload.order_id,
        public_ref=payload.public_ref,
        customer_id=payload.customer_id,
        amount=payload.amount,
        currency=payload.currency,
        method=payload.method,
    )


@router.get("/orders/{order_id}", response_model=PaymentResponse)
def get_for_order(order_id: int, db: Session = Depends(get_db)):
    payment = PaymentRepository(db).find_for_order(order_id)
    if payment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")
    return payment
