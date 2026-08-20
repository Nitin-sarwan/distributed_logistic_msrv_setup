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

# Partners get their own cookie name. Cookies are keyed by name and domain, so
# if the partner dashboard and the customer app are ever served from the same
# host, a shared name would mean signing into one silently overwrites the
# other's credential — and each service would then be handed a token it cannot
# decrypt. Two names let both sessions coexist in one browser.
PARTNER_SESSION_COOKIE_NAME = "lp_partner_session"


def extract_token(request, cookie_name: str = SESSION_COOKIE_NAME) -> str | None:
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

    `cookie_name` says which audience's cookie to read. It is an explicit
    argument rather than a "try both" fallback: a browser holding a customer
    session and a partner session sends both cookies on the same request, and
    guessing between them would authenticate whichever happened to be checked
    first. The caller always knows which one it wants — the gateway from the
    route prefix, a service from the fact that it only has one kind of subject.
    """
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()

    token = request.headers.get("x-token", "").strip()
    if token:
        return token

    cookie = (request.cookies.get(cookie_name) or "").strip()
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
