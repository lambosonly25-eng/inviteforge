"""
InviteForge — Auth Layer
JWT tokens (90-day expiry). bcrypt password hashing.
JWT_SECRET from env var — set this in Render. If missing, generates one per process
(tokens invalidated on restart, acceptable for dev).
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Request, HTTPException
from passlib.context import CryptContext

# ── PASSWORD HASHING ──
_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return _pwd_ctx.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plain, hashed)


# ── JWT ──
JWT_SECRET: str = os.getenv("JWT_SECRET", str(uuid.uuid4()))
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 90


def create_token(user_id: str, email: str) -> str:
    payload = {
        "user_id": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except Exception:
        return None


def get_token_from_request(request: Request) -> Optional[str]:
    """Extract JWT from Authorization header or cookie."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.cookies.get("if_jwt")


def get_current_user_id(request: Request) -> Optional[str]:
    """Return user_id from JWT, or None if not authenticated."""
    token = get_token_from_request(request)
    if not token:
        return None
    payload = verify_token(token)
    return payload.get("user_id") if payload else None


def require_auth(request: Request) -> dict:
    """FastAPI dependency — raises 401 if not authenticated."""
    token = get_token_from_request(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token. Please log in again.")
    return payload
