from typing import Annotated

import jwt
from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..core.security import decode_access_token
from ..database import get_db
from ..exceptions import AccountDisabledError
from ..models import User
from ..repositories.users import UserRepository


def get_current_user(
    access_token: Annotated[str | None, Cookie()] = None,
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
    )
    if access_token is None:
        raise unauthorized
    try:
        user_id = decode_access_token(access_token)
    except (jwt.PyJWTError, ValueError, TypeError):
        raise unauthorized from None
    user = UserRepository(db).get_by_id(user_id)
    if user is None:
        raise unauthorized
    if not user.is_active:
        raise AccountDisabledError
    return user
