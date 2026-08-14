import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response, status

from src.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Longest prefix wins, so a more specific route can shadow a broader one.
SERVICE_ROUTES: dict[str, str] = {
    "/api/users": settings.user_service_url,
}

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


def resolve_service(path: str) -> str | None:
    for prefix in sorted(SERVICE_ROUTES, key=len, reverse=True):
        if path == prefix or path.startswith(prefix + "/"):
            return SERVICE_ROUTES[prefix]
    return None


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
    base_url = resolve_service(full_path)

    if base_url is None:
        return Response(
            content=f'{{"detail":"No service registered for {full_path}"}}',
            status_code=status.HTTP_404_NOT_FOUND,
            media_type="application/json",
        )

    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}

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

    response_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
    )
