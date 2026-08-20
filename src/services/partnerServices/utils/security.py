"""Password hashing and the token cipher, for partners.

Deliberately a sibling of `userServices/utils/security.py` rather than an import
of it. The two produce byte-identical output — they read the same `.env` secrets
and derive keys the same way — but a service that imports another service's
`utils` also imports that service's `config`, and that config pins `user_db`.
One stray import and partnerServices would be opening a connection to a database
it is not allowed to touch.

The duplication is the cost of that boundary, and it is the cheaper side of the
trade today. If a third service needs this, hoist the cipher into
`src/common/crypto.py` taking its secrets as arguments — the values are
properties of the deployment, not of any one service.
"""

import base64
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from src.services.partnerServices.config import settings

# Stamped into every token this service issues and checked on the way back in.
#
# It is not what makes a partner token unusable as a user token — that is
# already guaranteed, because decryption needs a per-partner `token_secret` that
# lives in a database userServices cannot read. This is the cheap explicit check
# that says so out loud, so a future refactor that accidentally shares a secret
# fails loudly instead of authenticating the wrong person.
TOKEN_SUBJECT = "partner"


def _prehash(password: str) -> bytes:
    """HMAC-SHA256 the password under the pepper before bcrypt sees it.

    The pepper lives only in `.env`, never in the database, so a stolen
    `partners` table cannot be cracked offline. The pre-hash also sidesteps
    bcrypt's silent 72-byte truncation, so a long passphrase keeps its entropy.
    """
    message = f"{settings.static_salt}{password}".encode()
    digest = hmac.new(settings.static_pepper.encode(), message, hashlib.sha256).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    # bcrypt generates its own random per-partner salt and embeds it in the output.
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(_prehash(password), password_hash.encode())


def _derive_key(key: str) -> bytes:
    """sha256(key + STATIC_PEPPER + PASS_SALT_STATIC).

    Byte-for-byte the same derivation as the Node encryptData, including the
    concatenation order — change either side and tokens stop crossing over.
    """
    material = f"{key}{settings.static_pepper}{settings.pass_salt_static}"
    return hashlib.sha256(material.encode()).digest()


def _cipher(enc_key: bytes, iv: bytes) -> Cipher:
    # SECRET_KEY carries the OpenSSL algorithm name (e.g. "aes-256-cbc"), which
    # is what Node passes to createCipheriv — it is not key material.
    algorithm = settings.secret_key.lower()
    if algorithm not in ("aes-256-cbc", "aes256"):
        raise ValueError(f"Unsupported SECRET_KEY algorithm: {settings.secret_key}")
    return Cipher(algorithms.AES(enc_key), modes.CBC(iv))


def encrypt_data(data: dict | str, key: str) -> str | None:
    """AES-256-CBC encrypt a payload. Output is hex(iv) + hex(ciphertext)."""
    try:
        enc_key = _derive_key(key)
        # separators match JSON.stringify, which emits no spaces.
        plaintext = json.dumps(data, separators=(",", ":")).encode()

        iv = os.urandom(16)
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()

        encryptor = _cipher(enc_key, iv).encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()
        return iv.hex() + ciphertext.hex()
    except Exception:
        return None


def decrypt_data(encrypted_data: str, key: str) -> dict | None:
    """Reverse encrypt_data. Returns None on any failure.

    CBC is unauthenticated, so a tampered ciphertext surfaces as a padding or a
    JSON failure rather than being detected outright. Returning None uniformly
    for every kind of failure is what keeps that safe — a caller that could tell
    "bad padding" from "bad JSON" would have a padding oracle.
    """
    try:
        enc_key = _derive_key(key)
        iv = bytes.fromhex(encrypted_data[:32])
        ciphertext = bytes.fromhex(encrypted_data[32:])

        decryptor = _cipher(enc_key, iv).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()

        unpadder = padding.PKCS7(128).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
        return json.loads(plaintext)
    except Exception:
        return None


def create_access_token(partner) -> tuple[str, str, datetime]:
    """Encrypt a session token with the partner's own token_secret as the key.

    Returns (token, jti, expires_at) so the caller can persist the session.
    """
    jti = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=settings.access_token_expire_minutes)

    payload = {
        "id": partner.id,
        "name": partner.name,
        # Phone rather than email: it is the login identity here, and email is
        # nullable. A payload field that is sometimes null is a field every
        # consumer has to guard.
        "phone": partner.phone,
        "subject": TOKEN_SUBJECT,
        # Marks this as usable for authentication. A refresh token carries
        # type="refresh" and is rejected by get_current_partner.
        "type": "access",
        "jti": jti,
        "timeStamp": int(now.timestamp() * 1000),
        "exp": int(expires_at.timestamp()),
    }
    return encrypt_data(payload, partner.token_secret), jti, expires_at


def create_refresh_token(partner) -> tuple[str, str, datetime]:
    """A longer-lived token whose only power is minting new access tokens.

    Deliberately carries no name or phone. A refresh token sits in a driver's
    phone for thirty days; there is no reason for it to also be a copy of their
    personal details.
    """
    jti = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=settings.refresh_token_expire_days)

    payload = {
        "id": partner.id,
        "subject": TOKEN_SUBJECT,
        "type": "refresh",
        "jti": jti,
        "timeStamp": int(now.timestamp() * 1000),
        "exp": int(expires_at.timestamp()),
    }
    return encrypt_data(payload, partner.token_secret), jti, expires_at
