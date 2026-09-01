import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pymongo.errors import PyMongoError

from src.common.request_auth import (
    PARTNER_SESSION_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    extract_token,
)
from src.config import settings
from src.database.session_store import get_active_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Longest prefix wins, so a more specific route can shadow a broader one.
#
# Only /api/* prefixes appear here, which is what makes partnerServices'
# /internal/* routes unreachable from outside: an unrouted path is a 404 at the
# edge and never reaches any service.
SERVICE_ROUTES: dict[str, str] = {
    "/api/users": settings.user_service_url,
    "/api/partners": settings.partner_service_url,
    # Geocoding. Answered by userServices today, which is why it points at the
    # same URL — but it is registered as its own prefix rather than nested under
    # /api/users, so moving it to a dedicated service later is this one line.
    "/api/geo": settings.geo_service_url,
    "/api/orders": settings.order_service_url,
    # Same service, partner-facing. A separate prefix because the cookie that
    # authenticates it is a different one.
    "/api/deliveries": settings.order_service_url,
}

# Which HttpOnly cookie carries the credential for each service.
#
# The two names are distinct so a browser can hold a customer session and a
# partner session at once — see PARTNER_SESSION_COOKIE_NAME. The gateway has
# already resolved the route by the time it authenticates, so it always knows
# which of the two to read; guessing between them would authenticate whichever
# cookie happened to be checked first.
ROUTE_COOKIES: dict[str, str] = {
    "/api/users": SESSION_COOKIE_NAME,
    "/api/partners": PARTNER_SESSION_COOKIE_NAME,
    # Both geo endpoints are public, so this is never read on the paths that
    # exist. It is here because the lookup below is unconditional for any
    # non-public path under a registered prefix: a typo like /api/geo/serch must
    # answer 401 or 404, not blow up on a KeyError.
    "/api/geo": SESSION_COOKIE_NAME,
    # Orders belong to customers, so the customer cookie carries them. Order
    # re-checks the session's app_type itself — see its api/dependencies.py for
    # why a service that cannot decrypt a token must not settle for a header.
    "/api/orders": SESSION_COOKIE_NAME,
    "/api/deliveries": PARTNER_SESSION_COOKIE_NAME,
}

# Reachable without a token. Everything else is rejected at the edge.
#
# Matching is exact, so a new public endpoint has to be listed here or it will
# 401 before the service ever sees it.
PUBLIC_PATHS: set[str] = {
    "/api/users/register",
    "/api/users/login",
    # Called precisely when the access token has expired, so requiring one
    # would defeat the purpose. The refresh token in the body is the credential.
    "/api/users/refresh",
    # Reached by someone who cannot sign in, by definition.
    "/api/users/forgot-password",
    "/api/users/reset-password",
    "/api/partners/register",
    "/api/partners/login",
    "/api/partners/refresh",
    # Address lookup, used before anyone has signed in: the home page lets a
    # visitor describe a delivery first and asks who they are second, and a
    # search box that demanded an account would undo that.
    #
    # Public here means the gateway relays an anonymous request to a third-party
    # geocoder, so it is bounded on the other side — a per-IP quota, a
    # process-wide 1 req/s throttle, and a 24h cache. See geo_routes.py.
    "/api/geo/search",
    "/api/geo/reverse",
    "/health",
}

# Identity the gateway asserts downstream. Any client-supplied copy is stripped
# first — otherwise anyone could forge a user id just by sending the header.
IDENTITY_HEADERS = {"x-user-id", "x-session-id", "x-device-session"}

# CORS is answered here and only here. The gateway is the sole browser-facing
# origin; the services behind it are never called from a browser, so they have
# no reason to carry CORS middleware of their own. If one ever does, its headers
# are stripped on relay (see CORS_RESPONSE_PREFIX) rather than being forwarded —
# two Access-Control-Allow-Origin headers on one response is not "more
# permissive", it is a hard failure in every browser.
CORS_RESPONSE_PREFIX = "access-control-"

# Hop-by-hop headers are connection-scoped and must not be forwarded.
# Content-Length is dropped too, since httpx recomputes it for the new body.
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}


def match_prefix(path: str) -> str | None:
    """The registered prefix this path belongs to, or None.

    Returns the prefix rather than the URL because the caller needs both the
    destination and the cookie name, and looking them up from one key keeps the
    two tables from drifting apart.
    """
    for prefix in sorted(SERVICE_ROUTES, key=len, reverse=True):
        if path == prefix or path.startswith(prefix + "/"):
            return prefix
    return None


def is_public(path: str) -> bool:
    return path.rstrip("/") in PUBLIC_PATHS


def authenticate(request: Request, cookie_name: str) -> dict | None:
    """Coarse edge check: is there a live session for this token?

    The gateway cannot decrypt the token — token_secret is per-subject and lives
    in the owning service's private database. What it can do is consult the
    shared session store, which is the source of truth for revocation. The
    service still performs full cryptographic validation; this only stops
    unauthenticated traffic from reaching it at all.

    Note what this deliberately does not check: whether the session belongs to
    the right kind of subject. A customer's token presented to /api/partners
    passes here and is rejected by partnerServices, because decrypting it needs
    a secret in a database the gateway cannot read. The edge's job is to turn
    away anonymous traffic, not to be the final word.
    """
    token = extract_token(request, cookie_name=cookie_name)
    if token is None:
        return None

    try:
        return get_active_session(token)
    except PyMongoError as error:
        # Fail closed: if the session store is unreachable we cannot prove the
        # caller is authenticated, so we must not forward the request.
        logger.warning("Session lookup failed: %s", error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session store unavailable",
        ) from error


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One pooled client for the process — creating one per request would leak
    # sockets and lose connection reuse.
    app.state.client = httpx.AsyncClient(
        timeout=settings.gateway_timeout_seconds,
        follow_redirects=False,
    )
    logger.info("Gateway routing: %s", SERVICE_ROUTES)
    yield
    await app.state.client.aclose()


app = FastAPI(title="API Gateway", lifespan=lifespan)

# Browser access control. This runs before routing, so a preflight OPTIONS is
# answered here and never reaches proxy() — which matters, because a preflight
# carries no credentials and would otherwise be rejected as unauthenticated
# before the real request ever got a chance to be made.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # Lets the browser send the session credential. Requires an explicit origin
    # list above — a wildcard with credentials is rejected by every browser.
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",   # Bearer <token>, the primary credential
        "Content-Type",
        "X-Token",         # the alternate credential form extract_token accepts
        "X-Signature",     # optional, recorded on the session document
        "X-Requested-With",
    ],
    # Cache the preflight so the browser stops re-asking before every request.
    max_age=600,
)

logger.info("Gateway CORS origins: %s", settings.cors_origins)


@app.get("/health")
async def health():
    """Report the gateway plus every downstream service."""
    services = {}
    for prefix, base_url in SERVICE_ROUTES.items():
        try:
            response = await app.state.client.get(f"{base_url}/health", timeout=5.0)
            services[prefix] = response.json()
        except httpx.HTTPError as error:
            services[prefix] = {"status": "unreachable", "error": str(error)}

    reachable = all(s.get("status") == "ok" for s in services.values())
    return {
        "status": "ok" if reachable else "degraded",
        "service": "gateway",
        "services": services,
    }


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy(path: str, request: Request):
    full_path = "/" + path
    prefix = match_prefix(full_path)

    if prefix is None:
        return Response(
            content=f'{{"detail":"No service registered for {full_path}"}}',
            status_code=status.HTTP_404_NOT_FOUND,
            media_type="application/json",
        )

    base_url = SERVICE_ROUTES[prefix]

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP and k.lower() not in IDENTITY_HEADERS
    }

    if not is_public(full_path):
        session = authenticate(request, cookie_name=ROUTE_COOKIES[prefix])
        if session is None:
            return Response(
                content='{"detail":"Not authenticated"}',
                status_code=status.HTTP_401_UNAUTHORIZED,
                media_type="application/json",
                headers={"WWW-Authenticate": "Bearer"},
            )
        # Hints for the service. It re-verifies the token regardless — these
        # save work, they are not evidence.
        headers["x-user-id"] = str(session["user"])
        headers["x-device-session"] = session.get("device_session", "")

    # The service sees the gateway as its peer, so the real client IP has to be
    # forwarded explicitly — session records depend on it.
    client_ip = request.client.host if request.client else ""
    existing = request.headers.get("x-forwarded-for")
    headers["x-forwarded-for"] = f"{existing}, {client_ip}" if existing else client_ip
    headers["x-forwarded-proto"] = request.url.scheme
    headers["x-forwarded-host"] = request.headers.get("host", "")

    try:
        upstream = await request.app.state.client.request(
            method=request.method,
            url=f"{base_url}{full_path}",
            headers=headers,
            content=await request.body(),
            params=request.query_params,
        )
    except httpx.TimeoutException:
        return Response(
            content='{"detail":"Upstream service timed out"}',
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            media_type="application/json",
        )
    except httpx.HTTPError as error:
        logger.warning("Upstream %s unreachable: %s", base_url, error)
        return Response(
            content='{"detail":"Upstream service unavailable"}',
            status_code=status.HTTP_502_BAD_GATEWAY,
            media_type="application/json",
        )

    # Relay the service's headers, minus hop-by-hop, minus any CORS headers it
    # set for itself. The gateway's own middleware adds the authoritative ones
    # on the way out; forwarding a second set would duplicate
    # Access-Control-Allow-Origin and the browser would reject the response.
    #
    # Set-Cookie is excluded here and re-added below: a dict cannot hold two
    # entries with the same key, so two cookies would collapse into one
    # comma-joined header that no browser parses correctly.
    response_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in HOP_BY_HOP
        and k.lower() != "set-cookie"
        and not k.lower().startswith(CORS_RESPONSE_PREFIX)
    }

    response = Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
    )

    # Each Set-Cookie must survive as its own header line. Without this the
    # session cookie a service issues never reaches the browser, and cookie
    # authentication silently does not work.
    for key, value in upstream.headers.multi_items():
        if key.lower() == "set-cookie":
            response.raw_headers.append((b"set-cookie", value.encode("latin-1")))

    return response
