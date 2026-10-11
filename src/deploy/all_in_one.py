"""The whole backend in one process, for a host with one small machine.

Locally every service is its own uvicorn process. Render's free tier gives one
web service with 512 MB, which eight Python processes do not fit in, so this
mounts the eight apps into one ASGI app instead:

    /svc/user  /svc/partner  /svc/order  /svc/dispatch
    /svc/notification  /svc/pricing  /svc/payment      ← the services
    /                                                   ← the gateway

Nothing in any service changes. The gateway still forwards over HTTP and the
services still call each other over HTTP — to this same process, on loopback,
at the URLs set below. Every background loop (relays, consumers, the dispatch
worker) runs, because each app's own lifespan is entered.

**The services are not public.** Only the gateway is meant to face the
internet; `/svc/*` answers loopback callers only — the gateway and the services
themselves — and 404s anyone else, exactly as an unrouted port would.

Run with `--no-proxy-headers`, so the loopback check sees the real connecting
address rather than whatever an X-Forwarded-For header claims.
"""

import os

# Must be set before any service module is imported: each reads its settings
# once, at import.
_PORT = os.environ.get("PORT", "10000")
_BASE = f"http://127.0.0.1:{_PORT}/svc"
SERVICE_URLS = {
    "USER_SERVICE_URL": f"{_BASE}/user",
    "GEO_SERVICE_URL": f"{_BASE}/user",
    "PARTNER_SERVICE_URL": f"{_BASE}/partner",
    "ORDER_SERVICE_URL": f"{_BASE}/order",
    "DISPATCH_SERVICE_URL": f"{_BASE}/dispatch",
    "NOTIFICATION_SERVICE_URL": f"{_BASE}/notification",
    "PRICING_SERVICE_URL": f"{_BASE}/pricing",
    "PAYMENT_SERVICE_URL": f"{_BASE}/payment",
}
os.environ.update(SERVICE_URLS)

from contextlib import AsyncExitStack, asynccontextmanager  # noqa: E402

from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Mount  # noqa: E402

from src.gateway.main import app as gateway  # noqa: E402
from src.services.dispatchServices.main import app as dispatch  # noqa: E402
from src.services.notificationServices.main import app as notification  # noqa: E402
from src.services.orderServices.main import app as order  # noqa: E402
from src.services.partnerServices.main import app as partner  # noqa: E402
from src.services.paymentServices.main import app as payment  # noqa: E402
from src.services.pricingServices.main import app as pricing  # noqa: E402
from src.services.userServices.main import app as user  # noqa: E402

SERVICES = {
    "user": user,
    "partner": partner,
    "order": order,
    "dispatch": dispatch,
    "notification": notification,
    "pricing": pricing,
    "payment": payment,
}

LOOPBACK = {"127.0.0.1", "::1"}


class LoopbackOnly:
    """Let only this process's own calls reach a service."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            client = scope.get("client")
            if not client or client[0] not in LOOPBACK:
                response = JSONResponse({"detail": "Not Found"}, status_code=404)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(_app):
    # Enter every app's own startup and shutdown, in one stack: database
    # checks, Mongo, and each service's relay, consumer and worker loops.
    async with AsyncExitStack() as stack:
        for app in [*SERVICES.values(), gateway]:
            await stack.enter_async_context(app.router.lifespan_context(app))
        yield


app = Starlette(
    routes=[Mount(f"/svc/{name}", app=LoopbackOnly(service)) for name, service in SERVICES.items()]
    + [Mount("/", app=gateway)],
    lifespan=lifespan,
)
