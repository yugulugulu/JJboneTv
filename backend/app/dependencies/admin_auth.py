from typing import Annotated

import jwt
from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..core.admin_security import decode_admin_access_token
from ..database import get_db
from ..models import AdminAccount
from ..repositories.admin import AdminRepository


def get_current_admin(
    session_token: Annotated[
        str | None, Cookie(alias="thinktv_admin_session")
    ] = None,
    db: Session = Depends(get_db),
) -> AdminAccount:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Administrator authentication required",
    )
    if session_token is None:
        raise unauthorized
    try:
        admin_id = decode_admin_access_token(session_token)
    except (jwt.PyJWTError, ValueError, TypeError):
        raise unauthorized from None
    admin = AdminRepository(db).get_admin_by_id(admin_id)
    if admin is None or not admin.is_active:
        raise unauthorized
    return admin
