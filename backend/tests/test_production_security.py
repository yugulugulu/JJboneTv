import base64
from contextlib import contextmanager
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from app.config import Settings, settings
from app.exceptions import IdempotencyConflictError
from app.middleware import AuthRateLimitMiddleware
from app.runtime import RuntimeConfigurationError, validate_runtime_settings
from app.schemas import GenerationCreate
from app.services.auth_rate_limit import RateLimitDecision, client_ip
from app.services.generation_provider import (
    ProviderGenerationError,
    _decode_base64_image,
    _download_image,
    _limited_response_bytes,
)
from app.services.project_domain import ProjectDomainService, generation_request_hash
from app.controllers.project_domain import create_generation


def production_settings(**overrides):
    values = {
        "app_environment": "production",
        "database_url": "postgresql+psycopg://app_user:long-db-password@db.internal:5432/app",
        "jwt_secret": "u" * 48,
        "admin_jwt_secret": "a" * 48,
        "user_model_credentials_secret": "m" * 48,
        "auth_cookie_secure": True,
        "admin_cookie_secure": True,
        "admin_bootstrap_enabled": True,
        "asset_storage_backend": "oss",
        "oss_endpoint": "https://oss-cn-shenzhen.aliyuncs.com",
        "oss_bucket_name": "production-assets",
        "oss_access_key_id": "production-access-id",
        "oss_access_key_secret": "production-access-secret",
        "admin_bootstrap_email": "admin@company.com",
        "admin_bootstrap_password": "StrongProductionPassword123!",
        "auth_trusted_proxy_cidrs": "172.30.0.10/32",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_runtime_validation_is_role_aware_and_fails_closed_without_leaking_secret():
    validate_runtime_settings("user-api", production_settings(admin_jwt_secret="change-me"))
    validate_runtime_settings("admin-api", production_settings(jwt_secret="change-me"))

    secret = "visible-secret-that-must-never-appear"
    with pytest.raises(RuntimeConfigurationError) as caught:
        validate_runtime_settings(
            "user-api",
            production_settings(
                jwt_secret=secret,
                user_model_credentials_secret=secret,
                auth_cookie_secure=False,
                asset_storage_backend="local",
            ),
        )
    assert secret not in str(caught.value)
    assert "AUTH_COOKIE_SECURE" in str(caught.value)
    assert "ASSET_STORAGE_BACKEND" in str(caught.value)


def test_security_queue_migration_uses_driver_sql_for_json_update():
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260724_0005_security_runtime_queue.py"
    )
    spec = importlib.util.spec_from_file_location("security_runtime_queue_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    operations = MagicMock()
    migration.op = operations

    migration.upgrade()

    operations.execute.assert_not_called()
    operations.get_bind.return_value.exec_driver_sql.assert_called_once()
    update_sql = operations.get_bind.return_value.exec_driver_sql.call_args.args[0]
    assert "UPDATE generation_tasks" in update_sql
    assert "{\"provider_outcome_unknown\":true}" in update_sql


def test_client_ip_only_trusts_forwarding_from_configured_proxy_and_normalizes_ipv6(monkeypatch):
    monkeypatch.setattr(settings, "auth_trusted_proxy_cidrs", "10.0.0.0/8,2001:db8:ffff::/48")
    assert client_ip("203.0.113.9", "198.51.100.7") == "203.0.113.9"
    assert client_ip("10.0.0.4", "198.51.100.7, 10.0.0.3") == "198.51.100.7"
    assert client_ip("2001:db8:ffff::1", "2001:db8:abcd:1234::beef") == (
        "2001:db8:abcd:1234::/64"
    )


def test_auth_rate_limit_middleware_returns_retry_contract(monkeypatch):
    monkeypatch.setattr(
        "app.middleware.consume_auth_attempt",
        lambda _address: RateLimitDecision(False, 17),
    )
    app = FastAPI()
    app.add_middleware(AuthRateLimitMiddleware)

    @app.post("/api/auth/login")
    def login():
        return {"unexpected": True}

    response = TestClient(app).post("/api/auth/login", json={})

    assert response.status_code == 429
    assert response.headers["retry-after"] == "17"
    assert response.json() == {
        "detail": "操作过于频繁，请在 17 秒后重试。",
        "code": "AUTH_RATE_LIMITED",
        "retry_after": 17,
    }


def test_generation_idempotency_reuses_same_request_and_rejects_conflict():
    payload = GenerationCreate(
        project_id=uuid4(),
        node_id=uuid4(),
        input_revision=3,
        kind="image",
        model="gpt-image-2",
        prompt="A white ceramic coffee cup",
        parameters={"size": "1024x1024", "quality": "medium"},
    )
    existing = SimpleNamespace(request_hash=generation_request_hash(payload))
    service = ProjectDomainService(SimpleNamespace())
    service.repo = SimpleNamespace(
        get_generation_by_idempotency=lambda _owner, _key: existing
    )

    owner_id = uuid4()
    result = service.create_generation(owner_id, payload, "stable-key")
    assert result.task is existing
    assert result.replayed is True

    conflicting_payload = payload.model_copy(update={"prompt": "A black ceramic coffee cup"})
    with pytest.raises(IdempotencyConflictError):
        service.create_generation(owner_id, conflicting_payload, "stable-key")


def test_generation_controller_marks_idempotency_replay():
    payload = GenerationCreate(
        project_id=uuid4(),
        node_id=uuid4(),
        input_revision=3,
        kind="image",
        model="gpt-image-2",
        prompt="A white ceramic coffee cup",
    )
    task = SimpleNamespace()
    domain = SimpleNamespace(
        create_generation=lambda _owner, _payload, _key: SimpleNamespace(
            task=task, replayed=True
        )
    )
    response = Response()

    returned = create_generation(
        payload=payload,
        response=response,
        idempotency_key="stable-key",
        current_user=SimpleNamespace(id=uuid4()),
        domain=domain,
    )

    assert returned is task
    assert response.headers["Idempotency-Replayed"] == "true"


def test_admin_bootstrap_flag_and_runtime_ranges_are_fail_closed():
    validate_runtime_settings(
        "admin-api",
        production_settings(
            admin_bootstrap_enabled=False,
            admin_bootstrap_email="",
            admin_bootstrap_password="",
        ),
    )
    with pytest.raises(RuntimeConfigurationError, match="Bootstrap"):
        validate_runtime_settings(
            "admin-api",
            production_settings(admin_bootstrap_enabled=False),
        )
    with pytest.raises(RuntimeConfigurationError, match="至少 16"):
        validate_runtime_settings(
            "admin-api",
            production_settings(admin_bootstrap_password="Short1"),
        )
    with pytest.raises(RuntimeConfigurationError, match="HEARTBEAT"):
        validate_runtime_settings(
            "worker",
            production_settings(
                generation_lease_seconds=30,
                generation_heartbeat_seconds=30,
            ),
        )


class StreamOnlyResponse:
    def __init__(self, chunks, headers=None, *, status_code=200):
        self._chunks = chunks
        self.headers = headers or {}
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300

    def iter_bytes(self):
        yield from self._chunks

    @property
    def content(self):
        raise AssertionError("unbounded response.content must not be accessed")

    def json(self):
        raise AssertionError("unbounded response.json() must not be accessed")


def test_stream_limits_declared_and_actual_bytes():
    with pytest.raises(ProviderGenerationError, match="超过大小限制"):
        _limited_response_bytes(
            StreamOnlyResponse([b"x"], {"content-length": "11"}), 10, "响应"
        )
    with pytest.raises(ProviderGenerationError, match="超过大小限制"):
        _limited_response_bytes(StreamOnlyResponse([b"12345", b"678901"]), 10, "响应")
    assert _limited_response_bytes(StreamOnlyResponse([b"123", b"456"]), 6, "响应") == b"123456"


def test_base64_limit_is_checked_before_decode(monkeypatch):
    monkeypatch.setattr(settings, "generated_asset_max_bytes", 3)
    assert _decode_base64_image(base64.b64encode(b"abc").decode()) == b"abc"
    with pytest.raises(ProviderGenerationError, match="超过大小限制"):
        _decode_base64_image(base64.b64encode(b"abcd").decode())


def test_image_download_uses_bounded_stream(monkeypatch):
    monkeypatch.setattr(settings, "generated_asset_max_bytes", 8)
    monkeypatch.setattr(
        "app.services.generation_provider._validate_image_download_url",
        lambda value: value,
    )

    class Client:
        @contextmanager
        def stream(self, method, url, **kwargs):
            assert method == "GET"
            assert kwargs == {"follow_redirects": False}
            yield StreamOnlyResponse(
                [b"\x89PNG\r\n\x1a\n"], {"content-type": "image/png"}
            )

    data, mime_type = _download_image(Client(), "https://provider.example/image.png")
    assert data == b"\x89PNG\r\n\x1a\n"
    assert mime_type == "image/png"
