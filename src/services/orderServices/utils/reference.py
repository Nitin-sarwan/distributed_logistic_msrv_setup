import secrets

# No I, O, 0, 1: a reference gets read aloud on the phone and written on a box.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LENGTH = 6


def generate_public_ref() -> str:
    """A short human-facing order reference, e.g. "LP-8F3K2Q".

    Separate from the primary key because ids leak: a sequential integer in a
    URL tells a customer how many orders the platform has taken, and invites
    them to try the neighbouring one. This is random, unguessable at this
    length for the volumes involved, and still short enough to say out loud.

    Uniqueness is enforced by the column, not by this function — the caller
    retries on conflict.
    """
    suffix = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
    return f"LP-{suffix}"
