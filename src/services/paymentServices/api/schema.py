"""Request and response shapes. Money as a string, as everywhere on the platform."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from src.services.paymentServices.domain.states import (
    CollectedVia,
    PaymentMethod,
    PaymentStatus,
)


class OpenPayment(BaseModel):
    """Order, on confirm. Internal only."""

    order_id: int
    public_ref: str = Field(max_length=12)
    customer_id: int
    amount: Decimal = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    method: PaymentMethod


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_id: int
    public_ref: str
    method: PaymentMethod
    status: PaymentStatus
    amount: Decimal
    currency: str
    collected_via: CollectedVia | None
    paid_at: datetime | None


class CollectRequest(BaseModel):
    """What the driver says happened at the door: cash, or UPI."""

    via: CollectedVia


class UpiRequestResponse(BaseModel):
    """Everything the driver's screen needs to show a scannable QR."""

    order_id: int
    public_ref: str
    amount: Decimal
    currency: str
    # The `upi://pay?...` link the QR encodes.
    upi_link: str
    payee_vpa: str
    payee_name: str
