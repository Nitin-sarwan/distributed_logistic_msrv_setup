"""Pulling credentials off a request.

Shared by the gateway and the services so both accept exactly the same forms —
if they disagree, a request authenticates at one layer and fails at the other.

Headers only. Query strings are recorded in access logs, browser history, and
Referer headers on outbound links, so a token there should be treated as
exposed.
"""


def extract_token(request) -> str | None:
    """Find the access token.

    1. Authorization: Bearer <token>   — preferred
    2. X-Token: <token>
    """
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()

    token = request.headers.get("x-token", "").strip()
    return token or None


def extract_user_id(request) -> int | None:
    """Find the claimed user id from X-User-Id.

    This is only a hint for looking up that user's token_secret. It proves
    nothing on its own — the caller must still decrypt the token with that
    secret and confirm the payload agrees.
    """
    raw = request.headers.get("x-user-id")
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return None
