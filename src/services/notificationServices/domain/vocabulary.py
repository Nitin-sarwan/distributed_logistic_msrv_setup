"""The closed vocabularies this service uses.

Stored as plain `varchar` columns with a CHECK constraint rather than a Postgres
ENUM type, matching the rest of the platform: a native enum needs
`ALTER TYPE ... ADD VALUE` to grow, which cannot run inside a transaction and
therefore cannot be reverted by a downgrade.

They subclass `str`, so a Pydantic response serialises them as their value and
comparisons against a raw string from the database still work.
"""

from enum import Enum


class RecipientType(str, Enum):
    """Which app a message is for.

    The same discriminator problem the session store has, for the same reason:
    customer 5 and partner 5 are different people with the same integer id,
    because each service numbers its own subjects from 1. Every read, every
    write and every preference lookup in this service is keyed by the pair, and
    a query that forgets the type shows a driver a customer's messages.
    """

    CUSTOMER = "customer"
    PARTNER = "partner"


class Channel(str, Enum):
    """How a message gets to a person.

    IN_APP is not a delivery channel in the way the others are - it *is* the
    stored row, and the feed the app reads. It always happens. The other two are
    attempted according to preference and are logging stubs until the RabbitMQ
    workers exist; the point of having them now is that the fan-out is visible
    in the log before it is visible in somebody's inbox.
    """

    IN_APP = "in_app"
    EMAIL = "email"
    PUSH = "push"


class Category(str, Enum):
    """What a message is about, for muting and for grouping in the feed.

    Coarse on purpose. A person who mutes something is making a decision in one
    second from a settings screen, and forty categories is not a choice, it is a
    form. These four are the distinctions people actually make.
    """

    # The delivery moved. Confirmed, assigned, picked up, delivered.
    ORDER = "order"

    # A job is on offer, right now, with a countdown. Partner-only, and the one
    # category that must never be mutable - a driver who silences these has
    # silently gone off duty without telling anyone.
    OFFER = "offer"

    # Money. Charged, refunded, paid out.
    PAYMENT = "payment"

    # Everything about the account itself rather than a delivery.
    ACCOUNT = "account"


# Categories a person may switch off. OFFER is deliberately absent: see above.
MUTABLE_CATEGORIES: frozenset[Category] = frozenset(
    {Category.ORDER, Category.PAYMENT, Category.ACCOUNT}
)
