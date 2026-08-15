"""Pulling credentials off a request.

Shared by the gateway and the services so both accept exactly the same forms —
if they disagree, a request authenticates at one layer and fails at the other.

Headers and the session cookie. Query strings are never read: they are recorded
in access logs, browser history, and Referer headers on outbound links, so a
token there should be treated as exposed.
"""

# The HttpOnly session cookie browsers send. Defined here, next to the code that
# reads it, so the gateway and every service agree on the name — if they
# disagreed, a request would authenticate at one layer and fail at the other.
SESSION_COOKIE_NAME = "lp_session"


def extract_token(request) -> str | None:
    """Find the access token.

    1. Authorization: Bearer <token>   — preferred; used by non-browser clients
    2. X-Token: <token>
    3. the HttpOnly session cookie     — used by the browser frontend

    Headers are checked first so an explicit credential always wins over
    whatever cookie the browser happened to attach. That matters when a tool or
    a second account is being used from a browser that already holds a session:
    the caller's stated intent should decide, not the ambient cookie.

    The cookie is last rather than absent because it is the only form that can
    be HttpOnly. A token in a header has to be reachable by JavaScript to be
    sent, so any XSS can read it; a cookie the script cannot see survives that.
    """
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()

    token = request.headers.get("x-token", "").strip()
    if token:
        return token

    cookie = (request.cookies.get(SESSION_COOKIE_NAME) or "").strip()
    return cookie or None


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
