from sqlalchemy import select
from sqlalchemy.orm import Session

from src.services.paymentServices.models.payment_model import Payment


class PaymentRepository:
    """Every query this service runs. Nothing above here writes SQL."""

    def __init__(self, db: Session):
        self.db = db

    def add(self, payment: Payment) -> None:
        self.db.add(payment)

    def find_for_order(self, order_id: int) -> Payment | None:
        return self.db.scalar(select(Payment).where(Payment.order_id == order_id))

    def find_for_partner(self, order_id: int, partner_id: int) -> Payment | None:
        """This driver's delivery's payment, or None.

        Scoped by both ids in the query, so "not your delivery" and "no such
        order" are the same answer.
        """
        return self.db.scalar(
            select(Payment).where(
                Payment.order_id == order_id, Payment.partner_id == partner_id
            )
        )
