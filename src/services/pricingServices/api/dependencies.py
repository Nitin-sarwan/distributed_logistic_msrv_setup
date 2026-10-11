"""Who may call.

Nobody needs to be signed in to ask what a delivery costs, so the public routes
have no session dependency at all — there is no `get_current_customer_id` in
this service, and adding one would be the first step toward personalised
pricing, which is a decision and not a refactor.

`/internal/*` is guarded by the shared key, like every other service.
"""

import hmac

from fastapi import Header, HTTPException, status

from src.services.pricingServices.config import settings


def require_internal_key(x_internal_key: str = Header(default="")) -> None:
    """Guard `/internal/*`, which the gateway does not route.

    Same contract as the other services: an empty INTERNAL_API_KEY disables the
    check for local development, and the comparison is constant-time because a
    plain `!=` leaks the matching prefix through timing.
    """
    expected = settings.internal_api_key
    if not expected:
        return

    if not hmac.compare_digest(x_internal_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal API key",
        )
