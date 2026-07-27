from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.core.security import create_access_token
from app.database import SessionLocal
from app.main import app
from app.models import User


def test_auth_contract():
    email = f"creator-{uuid4()}@example.com"
    client = TestClient(app)

    try:
        register_response = client.post(
            "/api/auth/register",
            json={"email": f"  {email.upper()}  ", "password": "Strong123"},
        )
        assert register_response.status_code == 201
        assert register_response.json()["user"]["email"] == email
        assert register_response.json()["user"]["id"]
        assert "HttpOnly" in register_response.headers["set-cookie"]
        assert "SameSite=lax" in register_response.headers["set-cookie"]
        assert "Path=/" in register_response.headers["set-cookie"]

        with SessionLocal() as db:
            stored_user = db.scalar(select(User).where(User.email == email))
            assert stored_user is not None
            assert stored_user.password_hash.startswith("$argon2")
            assert stored_user.password_hash != "Strong123"

        me_response = client.get("/api/auth/me")
        assert me_response.status_code == 200
        assert me_response.json() == register_response.json()

        duplicate_response = client.post(
            "/api/auth/register",
            json={"email": email, "password": "Strong123"},
        )
        assert duplicate_response.status_code == 409

        client.cookies.clear()
        login_response = client.post(
            "/api/auth/login",
            json={"email": f"  {email.upper()}  ", "password": "Strong123"},
        )
        assert login_response.status_code == 200
        assert login_response.json() == register_response.json()

        logout_response = client.post("/api/auth/logout")
        assert logout_response.status_code == 204
        assert "access_token=" in logout_response.headers["set-cookie"]
        assert client.get("/api/auth/me").status_code == 401
    finally:
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == email))
            if user is not None:
                db.delete(user)
                db.commit()


def test_auth_validation_and_unauthorized_access():
    client = TestClient(app)

    assert client.get("/api/auth/me").status_code == 401
    assert client.post(
        "/api/auth/register",
        json={"email": "not-an-email", "password": "Strong123"},
    ).status_code == 422
    assert client.post(
        "/api/auth/register",
        json={"email": "creator@example.com", "password": "weakpass"},
    ).status_code == 422
    assert client.post(
        "/api/auth/login",
        json={"email": "missing@example.com", "password": "Strong123"},
    ).status_code == 401

    client.cookies.set("access_token", create_access_token(uuid4()))
    assert client.get("/api/auth/me").status_code == 401

    expired_token = jwt.encode(
        {
            "sub": str(uuid4()),
            "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    client.cookies.set("access_token", expired_token)
    assert client.get("/api/auth/me").status_code == 401
