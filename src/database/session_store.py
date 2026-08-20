"""Shared session store.

Sessions live in Mongo rather than in any one service's Postgres database so
every service can read and revoke them without cross-service SQL access.
Document shape mirrors the existing Node/Mongoose SessionModel.
"""

import secrets
import string
from datetime import datetime, timezone

from src.database.mongo import sessions_collection

ALPHABET = string.ascii_letters + string.digits


def generate_random_signature(length: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def get_request_info(request) -> dict:
    """Pull ip / browser / os off the request.

    Behind a proxy the socket peer is the proxy, so X-Forwarded-For wins when
    present — its first entry is the original client.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    else:
        ip = request.client.host if request.client else ""

    user_agent = request.headers.get("user-agent", "")
    ua = user_agent.lower()

    if "edg/" in ua:
        browser = "Edge"
    elif "chrome" in ua and "chromium" not in ua:
        browser = "Chrome"
    elif "safari" in ua and "chrome" not in ua:
        browser = "Safari"
    elif "firefox" in ua:
        browser = "Firefox"
    else:
        browser = user_agent or "Unknown"

    if "windows" in ua:
        os_name = "Windows"
    elif "android" in ua:
        os_name = "Android"
    elif "iphone" in ua or "ipad" in ua:
        os_name = "iOS"
    elif "mac os" in ua or "macintosh" in ua:
        os_name = "macOS"
    elif "linux" in ua:
        os_name = "Linux"
    else:
        os_name = "Unknown"

    return {"ip": ip, "browser": browser, "os": os_name, "user_agent": user_agent}


def insert_session(data: dict) -> dict:
    timestamp = datetime.now(timezone.utc)
    insert_data = {
        "user": data["user"],
        "valid_ip": data.get("user_ip") or "",
        "os": data.get("os") or "",
        "app_type": data.get("app_type") or 1,
        "device_id": data["device_id"],
        "device_info": data.get("device_info") or "",
        "device_session": data["device_session"],
        "is_active": data.get("is_active", True),
        "signature": data.get("signature") or "",
        "token_type": data.get("token_type") or "auth",
        "token": data["token"],
        "parent_token": data.get("parent_token"),
        "login_id": data.get("login_id") or "",
        # Additive to the Node/Mongoose shape. The token carries its own exp,
        # but only the owning service can decrypt it — storing expiry here lets
        # the gateway reject stale sessions without the per-user key.
        "expires_at": data.get("expires_at"),
        "last_activity": timestamp,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    sessions_collection().insert_one(insert_data)
    # insert_one mutates the dict with a non-serialisable ObjectId.
    insert_data.pop("_id", None)
    return insert_data


def create_session(
    request,
    token: str,
    user_id: int,
    app_type: int = 1,
    expires_at: datetime | None = None,
    token_type: str = "auth",
    parent_token: str | None = None,
    device_session: str | None = None,
    device_id: str | None = None,
) -> dict:
    """Record a session.

    device_session/device_id can be carried over from an existing session so a
    refresh keeps the same device identity instead of inventing a new one.
    """
    request_info = get_request_info(request)
    device_session = device_session or "ds_" + generate_random_signature(8)
    device_id = device_id or "di_" + generate_random_signature(7)

    session_data = {
        "user": user_id,
        "expires_at": expires_at,
        "signature": request.headers.get("x-signature", ""),
        "parent_token": parent_token,
        "token": token,
        "token_type": token_type,
        "user_ip": request_info["ip"],
        "browser": request_info["browser"],
        "os": request_info["os"],
        "device_id": device_id,
        "device_session": device_session,
        "device_info": request_info["browser"],
        "is_active": True,
        "app_type": app_type,
        "login_id": str(user_id),
    }
    return insert_session(session_data)


def get_active_session(token: str) -> dict | None:
    """Return the session only if it is active and not past expiry.

    expires_at is treated as optional so sessions written by the Node service,
    which does not set it, still validate on is_active alone.
    """
    session = sessions_collection().find_one(
        {"token": token, "is_active": True},
        {"_id": 0},
    )
    if session is None:
        return None

    expires_at = session.get("expires_at")
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            return None

    return session


def touch_session(token: str) -> bool:
    """Bump last_activity — call on authenticated requests."""
    timestamp = datetime.now(timezone.utc)
    result = sessions_collection().update_one(
        {"token": token, "is_active": True},
        {"$set": {"last_activity": timestamp, "updated_at": timestamp}},
    )
    return result.modified_count > 0


def revoke_session(token: str) -> bool:
    timestamp = datetime.now(timezone.utc)
    result = sessions_collection().update_one(
        {"token": token},
        {"$set": {"is_active": False, "updated_at": timestamp}},
    )
    return result.modified_count > 0


def revoke_user_sessions(
    user_id: int,
    except_token: str | None = None,
    app_type: int | None = None,
) -> int:
    """Revoke every session for a subject — logout-everywhere, or after a breach.

    except_token keeps the caller's own session alive, which is what a password
    change wants: evict every other device without signing yourself out.

    **app_type is not optional in practice, only in signature.** `user` in these
    documents is a bare integer id, and each service numbers its own subjects
    from 1 — user 5 in `user_db` and partner 5 in `partner_db` are different
    people who share a key. Without the discriminator, a customer tapping "log
    out everywhere" knocks an unrelated partner offline mid-delivery.

    It defaults to None so sessions written by the Node service, which predates
    the field, can still be revoked wholesale. Every caller in this repository
    passes it.
    """
    timestamp = datetime.now(timezone.utc)
    criteria: dict = {"user": user_id, "is_active": True}
    if except_token is not None:
        criteria["token"] = {"$ne": except_token}
    if app_type is not None:
        criteria["app_type"] = app_type

    result = sessions_collection().update_many(
        criteria,
        {"$set": {"is_active": False, "updated_at": timestamp}},
    )
    return result.modified_count


def revoke_children(parent_token: str) -> int:
    """Revoke every access token a given refresh token minted.

    The refresh token itself survives — this is what a refresh call uses to
    retire the access token it is replacing.
    """
    timestamp = datetime.now(timezone.utc)
    result = sessions_collection().update_many(
        {"parent_token": parent_token, "is_active": True},
        {"$set": {"is_active": False, "updated_at": timestamp}},
    )
    return result.modified_count


def revoke_session_family(parent_token: str) -> int:
    """Revoke a refresh token and every access token it minted."""
    timestamp = datetime.now(timezone.utc)
    result = sessions_collection().update_many(
        {
            "$or": [{"token": parent_token}, {"parent_token": parent_token}],
            "is_active": True,
        },
        {"$set": {"is_active": False, "updated_at": timestamp}},
    )
    return result.modified_count
