"""Push, as a log line.

Same arrangement as email: when RabbitMQ lands this publishes a `push.send` job
and a worker resolves the recipient id to device tokens. This service holds no
tokens, which is the point - a device token is a credential, and the fewer
places one lives the better.

The one message where the delay this stub represents is genuinely costly is
`partner.offered`: a driver has thirty seconds and currently learns about the
job by polling. That is called out in DISPATCH_SERVICE.md rather than hidden
here.
"""

import logging

from src.services.notificationServices.channels.base import Channel, DeliveryResult
from src.services.notificationServices.domain.templates import Message
from src.services.notificationServices.domain.vocabulary import Channel as ChannelName

logger = logging.getLogger(__name__)


class PushChannel(Channel):
    name = ChannelName.PUSH.value

    def send(self, message: Message, notification_id: int) -> DeliveryResult:
        logger.info(
            "[push.send] to %s:%s | %s | %s",
            message.recipient_type.value,
            message.recipient_id,
            message.title,
            message.body,
        )
        return DeliveryResult(
            channel=self.name,
            delivered=True,
            detail="logged; no push worker yet",
        )
