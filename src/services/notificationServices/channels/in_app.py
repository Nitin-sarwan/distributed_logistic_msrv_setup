"""The feed. The one channel that is real today.

It has no `send` worth the name, because the message is already delivered by the
time this is called: the row was written, and the app reads rows. This exists so
that the caller can treat all three channels identically, and so the stored
`channels` column is complete rather than mysteriously never mentioning the
channel that actually worked.
"""

import logging

from src.services.notificationServices.channels.base import Channel, DeliveryResult
from src.services.notificationServices.domain.templates import Message
from src.services.notificationServices.domain.vocabulary import Channel as ChannelName

logger = logging.getLogger(__name__)


class InAppChannel(Channel):
    name = ChannelName.IN_APP.value

    def send(self, message: Message, notification_id: int) -> DeliveryResult:
        logger.debug(
            "in_app #%s -> %s %s",
            notification_id,
            message.recipient_type.value,
            message.recipient_id,
        )
        return DeliveryResult(channel=self.name, delivered=True)
