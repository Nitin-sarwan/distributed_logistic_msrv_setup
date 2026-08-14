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

from src.services.userServices.config import settings


def _prehash(password: str) -> bytes:

    message = f"{settings.static_salt}{password}".encode()
    digest = hmac.new(settings.static_pepper.encode(), message, hashlib.sha256).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    # bcrypt generates its own random per-user salt and embeds it in the output.
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
    """AES-256-CBC encrypt a payload. Output is hex(iv) + hex(ciphertext).

    Mirrors the Node encryptData: 16-byte random IV, PKCS7 padding, and the IV
    hex-prefixed onto the ciphertext. Returns None on failure, as Node does.
    """
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
    """Reverse encrypt_data. Returns None on any failure, as Node does.

    CBC is unauthenticated, so a tampered ciphertext usually fails the padding
    check or the JSON parse rather than being detected outright.
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


def create_access_token(user) -> tuple[str, str, datetime]:
    """Encrypt a session token with the user's own token_secret as the salt.

    Returns (token, jti, expires_at) so the caller can persist the session.
    Rotating a user's token_secret invalidates only that user's tokens.
    """
    jti = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=settings.access_token_expire_minutes)

    payload = {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "jti": jti,
        "timeStamp": int(now.timestamp() * 1000),
        "exp": int(expires_at.timestamp()),
    }
    return encrypt_data(payload, user.token_secret), jti, expires_at
