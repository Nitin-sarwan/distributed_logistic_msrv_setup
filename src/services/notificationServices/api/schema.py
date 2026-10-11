"""Request and response shapes for the feed and the settings screen."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.services.notificationServices.domain.vocabulary import Category, RecipientType


class NotificationResponse(BaseModel):
    """One message, as the app renders it.

    `title` and `body` come back as stored text rather than a template id and
    arguments, because that is what the person was told - see the model. The
    client's job is to display them, not to assemble them.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    category: Category
    title: str
    body: str
    action_path: str | None

    # Which order this was about, so the app can group a delivery's messages
    # without parsing `action_path`.
    order_id: int | None

    read_at: datetime | None
    created_at: datetime


class FeedResponse(BaseModel):
    """A page of messages plus the badge number.

    The count travels with the page deliberately. Opening the feed and marking
    things read are the two moments the badge changes, and returning both in one
    call means the app never briefly shows a stale number.
    """

    items: list[NotificationResponse]
    unread_count: int


class UnreadCountResponse(BaseModel):
    """The badge, on its own.

    `capped` says the true number is higher than `count` - the count stops at
    `UNREAD_COUNT_CAP` so a neglected account does not cost a full table count
    on every poll. The app renders "99+" when it is true.
    """

    count: int
    capped: bool


class MarkReadResponse(BaseModel):
    """What changed, and the new badge.

    `updated` is 0 when the message was already read, which is not an error -
    a double tap on a slow connection is the normal way it happens.
    """

    updated: int
    unread_count: int


class PreferenceResponse(BaseModel):
    """Effective settings, whether or not the person has ever changed one.

    `mutable_categories` is sent so the settings screen can render the toggles
    from the server's list rather than a hardcoded copy. `offer` is absent from
    it on purpose: a driver who silenced job offers would be on duty and
    unreachable.
    """

    recipient_type: RecipientType
    email_enabled: bool
    push_enabled: bool
    muted_categories: list[str]
    mutable_categories: list[str]


class PreferenceUpdate(BaseModel):
    """Partial update. Anything omitted is left alone.

    All three fields are optional so a single toggle is a single field, rather
    than the client having to send back a whole settings object it may have
    fetched before somebody changed it on another device.
    """

    email_enabled: bool | None = None
    push_enabled: bool | None = None
    # Unmutable values are filtered out rather than rejected - see
    # `set_preferences`. A 422 on a settings toggle helps nobody.
    muted_categories: list[str] | None = Field(default=None, max_length=10)
