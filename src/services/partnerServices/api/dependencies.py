import hmac
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from src.common.request_auth import (
    PARTNER_SESSION_COOKIE_NAME,
    extract_token,
    extract_user_id,
)
from src.database.session_store import get_active_session, touch_session
from src.services.partnerServices.config import settings
from src.services.partnerServices.database.connection import get_db
from src.services.partnerServices.models.partner_model import Partner
from src.services.partnerServices.repositories.partner_repositories import (
    PartnerRepository,
)
from src.services.partnerServices.utils.security import TOKEN_SUBJECT, decrypt_data

UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def partner_token(request: Request) -> str | None:
    return extract_token(request, cookie_name=PARTNER_SESSION_COOKIE_NAME)


def get_current_partner(
    request: Request,
    db: Session = Depends(get_db),
) -> Partner:
    """Authoritative authentication for this service.

    The gateway performs a cheaper session check first, but this must not rely
    on that: a service reachable directly has to authenticate on its own, and
    headers set by an upstream proxy are not evidence of anything.

    The steps mirror userServices' `get_current_user`, with two additions that
    exist because two services now share one session collection:

    * the session's `app_type` must be this service's, and
    * the token payload's `subject` must be "partner".

    Neither is what makes a customer's token unusable here — that is already
    guaranteed, since decrypting it needs a `token_secret` stored in a database
    this service cannot read. They are the checks that make the boundary
    explicit, so a future change that accidentally shares a secret fails loudly
    rather than authenticating the wrong person.
    """
    token = partner_token(request)
    if token is None:
        raise UNAUTHORIZED

    repository = PartnerRepository(db)

    # A supplied id is a lookup shortcut, not a claim we trust. If it is absent
    # or wrong, fall back to resolving the partner from the session.
    partner = None
    claimed_id = extract_user_id(request)
    if claimed_id is not None:
        partner = repository.find_by_id(claimed_id)

    if partner is None:
        session = get_active_session(token)
        if session is None:
            raise UNAUTHORIZED
        if session.get("app_type") != settings.partner_app_type:
            raise UNAUTHORIZED
        partner = repository.find_by_id(session["user"])

    if partner is None:
        raise UNAUTHORIZED

    # The real check. Only this partner's token_secret decrypts this token, so a
    # successful decrypt is proof the token was issued to them — whatever the
    # request claimed.
    payload = decrypt_data(token, partner.token_secret)
    if payload is None:
        raise UNAUTHORIZED

    if payload.get("id") != partner.id:
        raise UNAUTHORIZED

    if payload.get("subject") != TOKEN_SUBJECT:
        raise UNAUTHORIZED

    # A refresh token must never authenticate a request — it is long-lived and
    # only exists to mint access tokens.
    if payload.get("type") != "access":
        raise UNAUTHORIZED

    expires = payload.get("exp")
    if expires is None or datetime.now(timezone.utc).timestamp() >= expires:
        raise UNAUTHORIZED

    # Checked even when the partner came from the id hint: decryption proves the
    # token is authentic, but only the session store knows if it was revoked.
    # The app_type check repeats here for the same reason — the hint path skips
    # the lookup above.
    session = get_active_session(token)
    if session is None or session.get("app_type") != settings.partner_app_type:
        raise UNAUTHORIZED

    touch_session(token)
    return partner


def require_internal_key(x_internal_key: str = Header(default="")) -> None:
    """Guard the /internal routes.

    Those paths are not in the gateway's routing table at all — it only forwards
    `/api/*` — so nothing reaching them arrived from the public internet through
    the front door. This is the second lock: anything that can reach the service
    port directly (another container, a misconfigured port mapping, a developer
    laptop on the same network) still has to know the shared secret.

    An empty `INTERNAL_API_KEY` disables the check, which is a deliberate
    convenience for a local machine and is logged as a warning at startup. It
    must be set anywhere else — these endpoints can approve KYC and reassign
    partners.
    """
    expected = settings.internal_api_key
    if not expected:
        return

    # Constant-time comparison. A plain `!=` leaks the length of the matching
    # prefix through timing, which is enough to recover a secret one character
    # at a time given enough requests.
    if not hmac.compare_digest(x_internal_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal API key",
        )
