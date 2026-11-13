"""Password hashing, JWT sessions, credential encryption, API-key hashing."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

from .config import JWT_SECRET, JWT_TTL_HOURS, MASTER_KEY

_fernet = Fernet(MASTER_KEY.encode())


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode()[:72], hashed.encode())
    except ValueError:
        return False


def create_token(user_id: str, org_id: str | None) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": user_id, "org": org_id, "iat": now, "exp": now + timedelta(hours=JWT_TTL_HOURS)}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt(token: str) -> str | None:
    try:
        return _fernet.decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def generate_api_key() -> tuple[str, str, str]:
    """Returns (plaintext, prefix, sha256-hash). Plaintext is shown once only."""
    raw = "gtm_live_" + secrets.token_urlsafe(32)
    return raw, raw[:14], hash_api_key(raw)


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def mask(value: str) -> str:
    return ("•" * 8 + value[-4:]) if value and len(value) > 4 else "••••"
