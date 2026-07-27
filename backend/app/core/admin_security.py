from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt

from ..config import settings


ADMIN_TOKEN_AUDIENCE = "thinktv-admin"


def create_admin_access_token(admin_id: UUID) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(admin_id),
        "aud": ADMIN_TOKEN_AUDIENCE,
        "token_type": "admin",
        "iat": now,
        "exp": now + timedelta(minutes=settings.admin_jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.admin_jwt_secret, algorithm="HS256")


def decode_admin_access_token(token: str) -> UUID:
    payload = jwt.decode(
        token,
        settings.admin_jwt_secret,
        algorithms=["HS256"],
        audience=ADMIN_TOKEN_AUDIENCE,
        options={"require": ["sub", "aud", "token_type", "exp"]},
    )
    if payload["token_type"] != "admin":
        raise jwt.InvalidTokenError("Invalid token type")
    return UUID(payload["sub"])
