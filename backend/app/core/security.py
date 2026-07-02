"""Secrets encryption, password hashing and dashboard JWTs."""
import base64
import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta

import jwt
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

_PBKDF2_ITERATIONS = 600_000


def _fernet() -> Fernet:
    settings = get_settings()
    if not settings.master_key:
        raise RuntimeError(
            "AUTOBOT_MASTER_KEY is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`"
        )
    return Fernet(settings.master_key.encode())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Cannot decrypt secret: wrong AUTOBOT_MASTER_KEY?") from exc


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"{base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_b64, digest_b64 = stored.split("$", 1)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)


def _jwt_secret() -> str:
    settings = get_settings()
    return settings.jwt_secret or hashlib.sha256(f"jwt:{settings.master_key}".encode()).hexdigest()


def create_token(subject: str = "admin") -> str:
    settings = get_settings()
    payload = {
        "sub": subject,
        "exp": datetime.now(UTC) + timedelta(minutes=settings.jwt_ttl_minutes),
        "iat": datetime.now(UTC),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def verify_token(token: str) -> str:
    payload = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    return payload["sub"]
