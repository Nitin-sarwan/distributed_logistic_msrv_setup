"""How a message leaves this service.

One interface, three implementations, and the interface is the deliverable. The
email and push channels log rather than send, and that is not a placeholder to
be apologised for - it is the shape the real workers plug into unchanged.

When RabbitMQ lands, `EmailChannel.send` stops calling `logger.info` and starts
publishing an `email.send` job. Nothing else in the service changes: not the
templates, not the consumer, not the preference check, not the stored row. That
is the whole reason for putting an interface in front of two log lines.

**What a channel is not allowed to do:** decide whether to send. Preference and
muting are resolved in `services/notification_services.py` before a channel is
ever reached, so a channel that is called always sends. Splitting that decision
across three implementations is how one of them ends up honouring a mute and the
other two do not.
"""

from src.services.notificationServices.channels.base import Channel, DeliveryResult
from src.services.notificationServices.channels.email import EmailChannel
from src.services.notificationServices.channels.in_app import InAppChannel
from src.services.notificationServices.channels.push import PushChannel

__all__ = [
    "Channel",
    "DeliveryResult",
    "EmailChannel",
    "InAppChannel",
    "PushChannel",
]
