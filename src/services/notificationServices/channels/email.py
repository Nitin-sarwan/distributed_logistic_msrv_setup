"""Email, as a log line.

There is no SMTP here and no address: this service does not know anybody's email
address and should not learn one. When Notification becomes the Kafka to
RabbitMQ bridge the blueprint describes, `send` publishes an `email.send` job
carrying the recipient *id*, and the email worker - which does have a reason to
hold addresses - resolves it.

That is why the log line below prints an id rather than an address. It is not a
missing feature; it is the boundary.
"""

import logging

from src.services.notificationServices.channels.base import Channel, DeliveryResult
from src.services.notificationServices.domain.templates import Message
from src.services.notificationServices.domain.vocabulary import Channel as ChannelName

logger = logging.getLogger(__name__)


class EmailChannel(Channel):
    name = ChannelName.EMAIL.value

    def send(self, message: Message, notification_id: int) -> DeliveryResult:
        logger.info(
            "[email.send] to %s:%s | %s | %s",
            message.recipient_type.value,
            message.recipient_id,
            message.title,
            message.body,
        )
        return DeliveryResult(
            channel=self.name,
            delivered=True,
            detail="logged; no email worker yet",
        )
