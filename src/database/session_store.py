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
        "last_activity": timestamp,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    sessions_collection().insert_one(insert_data)
    # insert_one mutates the dict with a non-serialisable ObjectId.
    insert_data.pop("_id", None)
    return insert_data


def create_session(request, token: str, user_id: int, app_type: int = 1) -> dict:
    request_info = get_request_info(request)
    device_session = "ds_" + generate_random_signature(8)
    device_id = "di_" + generate_random_signature(7)

    session_data = {
        "user": user_id,
        "signature": request.headers.get("x-signature", ""),
        "parent_token": None,
        "token": token,
        "token_type": "auth",
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
    return sessions_collection().find_one(
        {"token": token, "is_active": True},
        {"_id": 0},
    )


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


def revoke_user_sessions(user_id: int) -> int:
    """Revoke every session for a user — logout-everywhere, or after a breach."""
    timestamp = datetime.now(timezone.utc)
    result = sessions_collection().update_many(
        {"user": user_id, "is_active": True},
        {"$set": {"is_active": False, "updated_at": timestamp}},
    )
    return result.modified_count
