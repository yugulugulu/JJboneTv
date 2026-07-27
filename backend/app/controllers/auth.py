from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from ..config import settings
from ..core.security import create_access_token
from ..database import get_db
from ..dependencies.auth import get_current_user
from ..exceptions import DuplicateEmailError, InvalidCredentialsError
from ..models import User
from ..repositories.users import UserRepository
from ..schemas import AuthCredentials, AuthResponse
from ..services.auth import AuthService


router = APIRouter(prefix="/api/auth", tags=["auth"])


def set_access_token_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="access_token",
        value=token,
        max_age=settings.jwt_expire_minutes * 60,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def register(payload: AuthCredentials, response: Response, db: Session = Depends(get_db)):
    try:
        user = AuthService(UserRepository(db)).register(str(payload.email), payload.password)
    except DuplicateEmailError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered") from None
    set_access_token_cookie(response, create_access_token(user.id))
    return {"user": user}


@router.post("/login", response_model=AuthResponse)
def login(payload: AuthCredentials, response: Response, db: Session = Depends(get_db)):
    try:
        user = AuthService(UserRepository(db)).login(str(payload.email), payload.password)
    except InvalidCredentialsError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        ) from None
    set_access_token_cookie(response, create_access_token(user.id))
    return {"user": user}


@router.get("/me", response_model=AuthResponse)
def me(user: User = Depends(get_current_user)):
    return {"user": user}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout():
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        key="access_token",
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response
