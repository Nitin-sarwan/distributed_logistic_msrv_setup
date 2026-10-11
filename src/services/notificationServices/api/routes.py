"""The feed, twice: once for customers, once for drivers.

One router factory rather than two route files, because the endpoints are
genuinely identical - list, count, mark read, preferences - and the only
difference is which cookie authenticates them and therefore which
`recipient_type` the rows are scoped by. Two copies would be two places for the
scoping to drift, and the failure mode of that drift is showing one person
another person's messages.

**Why two prefixes at all.** The gateway maps one cookie to one path prefix, so
`/api/notifications` is authenticated with the customer cookie and
`/api/deliveries/notifications` with the partner cookie - the same split that
already exists between `/api/orders` and `/api/deliveries`. A single prefix
would have to guess which of the two cookies a browser holding both was
presenting, and guessing wrong signs somebody into the wrong app.

The partner prefix nests under `/api/deliveries`, which the gateway also routes
to orderServices. That works because the gateway matches the **longest** prefix
first: `/api/deliveries/notifications` wins over `/api/deliveries`. It is
load-bearing, and it is the reason the more specific entry must never be removed
from `SERVICE_ROUTES` while these routes exist.
"""

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from src.services.notificationServices.api.dependencies import (
    Recipient,
    get_customer,
    get_partner,
)
from src.services.notificationServices.api.schema import (
    FeedResponse,
    MarkReadResponse,
    NotificationResponse,
    PreferenceResponse,
    PreferenceUpdate,
    UnreadCountResponse,
)
from src.services.notificationServices.config import settings
from src.services.notificationServices.database.connection import get_db
from src.services.notificationServices.domain.vocabulary import MUTABLE_CATEGORIES
from src.services.notificationServices.services.notification_services import (
    NotificationService,
)

MUTABLE = sorted(category.value for category in MUTABLE_CATEGORIES)


def build_router(prefix: str, recipient_dependency, tag: str) -> APIRouter:
    """One feed API, bound to one kind of recipient.

    `recipient_dependency` resolves the session into a `Recipient` - the
    `(type, id)` pair - and every query below is scoped by both halves of it.
    Nothing in this file takes a recipient id from the path or the body, so
    there is no endpoint that can be walked.
    """
    router = APIRouter(prefix=prefix, tags=[tag])

    def service(db: Session = Depends(get_db)) -> NotificationService:
        return NotificationService(db)

    @router.get("", response_model=FeedResponse, status_code=status.HTTP_200_OK)
    def list_notifications(
        response: Response,
        unread_only: bool = Query(default=False),
        limit: int = Query(default=settings.feed_default_limit, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        recipient: Recipient = Depends(recipient_dependency),
        notifications: NotificationService = Depends(service),
    ):
        """This person's messages, newest first, with the badge count.

        The count comes back with the page so the app never briefly shows a
        stale badge next to a fresh list.
        """
        # Someone else's messages must never be served from a shared cache, and
        # the content changes constantly in any case.
        response.headers["Cache-Control"] = "no-store"

        items = notifications.feed(
            recipient.type, recipient.id, unread_only=unread_only,
            limit=limit, offset=offset,
        )
        return FeedResponse(
            items=[NotificationResponse.model_validate(item) for item in items],
            unread_count=notifications.unread_count(recipient.type, recipient.id),
        )

    @router.get(
        "/unread-count",
        response_model=UnreadCountResponse,
        status_code=status.HTTP_200_OK,
    )
    def unread_count(
        response: Response,
        recipient: Recipient = Depends(recipient_dependency),
        notifications: NotificationService = Depends(service),
    ):
        """The badge. Polled far more often than anything else here.

        Counting stops at UNREAD_COUNT_CAP, so `capped` true means "at least
        this many" - which is all the app needs to render "99+".
        """
        response.headers["Cache-Control"] = "no-store"

        count = notifications.unread_count(recipient.type, recipient.id)
        return UnreadCountResponse(
            count=min(count, settings.unread_count_cap),
            capped=count > settings.unread_count_cap,
        )

    @router.post(
        "/{notification_id}/read",
        response_model=MarkReadResponse,
        status_code=status.HTTP_200_OK,
    )
    def mark_read(
        notification_id: int,
        recipient: Recipient = Depends(recipient_dependency),
        notifications: NotificationService = Depends(service),
    ):
        """Mark one message read.

        200 with `updated: 0` for a message that is not this person's, or is
        already read. Deliberately not a 404: the two cases are indistinguishable
        to the caller and should be, and an app marking things read as they
        scroll past should not have to handle an error for a double tap.
        """
        changed = notifications.mark_read(recipient.type, recipient.id, notification_id)
        return MarkReadResponse(
            updated=1 if changed else 0,
            unread_count=notifications.unread_count(recipient.type, recipient.id),
        )

    @router.post(
        "/read-all", response_model=MarkReadResponse, status_code=status.HTTP_200_OK
    )
    def mark_all_read(
        recipient: Recipient = Depends(recipient_dependency),
        notifications: NotificationService = Depends(service),
    ):
        """Clear the badge in one call."""
        count = notifications.mark_all_read(recipient.type, recipient.id)
        return MarkReadResponse(updated=count, unread_count=0)

    @router.get(
        "/preferences",
        response_model=PreferenceResponse,
        status_code=status.HTTP_200_OK,
    )
    def get_preferences(
        recipient: Recipient = Depends(recipient_dependency),
        notifications: NotificationService = Depends(service),
    ):
        """Effective settings, whether or not this person has ever changed one.

        Never 404s on somebody with no row: no row means all defaults, and the
        settings screen has to render either way.
        """
        current = notifications.get_preferences(recipient.type, recipient.id)
        return PreferenceResponse(
            recipient_type=recipient.type,
            mutable_categories=MUTABLE,
            **current,
        )

    @router.patch(
        "/preferences",
        response_model=PreferenceResponse,
        status_code=status.HTTP_200_OK,
    )
    def update_preferences(
        payload: PreferenceUpdate,
        recipient: Recipient = Depends(recipient_dependency),
        notifications: NotificationService = Depends(service),
    ):
        """Change what this person hears about, and how.

        Partial: anything omitted is left alone, so a single toggle is a single
        field. In-app is not switchable - it is the stored record, and there is
        no version of "stop keeping a record of what you were told" that helps
        anybody.
        """
        current = notifications.set_preferences(
            recipient.type,
            recipient.id,
            email_enabled=payload.email_enabled,
            push_enabled=payload.push_enabled,
            muted_categories=payload.muted_categories,
        )
        return PreferenceResponse(
            recipient_type=recipient.type,
            mutable_categories=MUTABLE,
            **current,
        )

    return router


# Customer app. Gateway prefix /api/notifications, customer cookie.
customer_router = build_router("/notifications", get_customer, "notifications")

# Partner app. Gateway prefix /api/deliveries/notifications, partner cookie —
# longest-prefix-wins puts it here rather than at orderServices. See the module
# docstring.
partner_router = build_router(
    "/deliveries/notifications", get_partner, "notifications (partner)"
)
