"""The interface every channel implements."""

from abc import ABC, abstractmethod
from typing import NamedTuple

from src.services.notificationServices.domain.templates import Message


class DeliveryResult(NamedTuple):
    """What a channel can honestly report.

    `delivered` means "handed off", never "read" and never even "arrived". A
    queue accepted the job, or an SMTP server took the message. Anything
    stronger would be a claim this service is not in a position to make, and
    storing it as though it were would make the audit trail a fiction.
    """

    channel: str
    delivered: bool
    detail: str | None = None


class Channel(ABC):
    """One way of reaching a person.

    Called only after the preference check has already decided this message
    should go. A channel never decides whether to send - see the package
    docstring for why that is not spread across the implementations.
    """

    name: str

    @abstractmethod
    def send(self, message: Message, notification_id: int) -> DeliveryResult:
        """Deliver, or hand to something that will.

        Must not raise for an ordinary delivery failure. A dead SMTP server is
        not a reason to roll back the consumer's transaction and redeliver the
        Kafka message - that would replay every *other* channel for the same
        message too, and the customer would get a second push because an email
        bounced.
        """
