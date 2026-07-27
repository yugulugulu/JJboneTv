import argparse
import ipaddress
import sys
from collections.abc import Iterable
from urllib.parse import urlparse

from email_validator import EmailNotValidError, validate_email

from .config import Settings, settings


class RuntimeConfigurationError(RuntimeError):
    pass


_PLACEHOLDER_MARKERS = (
    "change-me",
    "changeme",
    "example",
    "placeholder",
    "replace-me",
    "replace-with",
    "your-",
    "<",
)


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return not normalized or any(marker in normalized for marker in _PLACEHOLDER_MARKERS)


def _secret_is_weak(value: str) -> bool:
    return _is_placeholder(value) or len(value.strip()) < 32


def _require(errors: list[str], name: str, value: str) -> None:
    if _is_placeholder(value):
        errors.append(f"{name} 缺失或仍为示例值")


def _validate_database_url(errors: list[str], value: str) -> None:
    _require(errors, "DATABASE_URL", value)
    parsed = urlparse(value.replace("postgresql+psycopg", "postgresql", 1))
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        errors.append("DATABASE_URL 必须是有效的 PostgreSQL 连接地址")
    if parsed.username in {"thinktv", "postgres"} or parsed.password in {"thinktv", "postgres"}:
        errors.append("DATABASE_URL 仍使用默认数据库凭据")


def _validate_secrets(errors: list[str], pairs: Iterable[tuple[str, str]]) -> None:
    seen: dict[str, str] = {}
    for name, value in pairs:
        if _secret_is_weak(value):
            errors.append(f"{name} 缺失、过短或仍为示例值")
        normalized = value.strip()
        if normalized and normalized in seen:
            errors.append(f"{name} 不能与 {seen[normalized]} 使用相同密钥")
        else:
            seen[normalized] = name


def _validate_range(
    errors: list[str], name: str, value: float, minimum: float, maximum: float
) -> None:
    if not minimum <= value <= maximum:
        errors.append(f"{name} 必须位于 {minimum:g} 到 {maximum:g} 之间")


def _validate_storage(errors: list[str], config: Settings) -> None:
    backend = config.asset_storage_backend.strip().lower()
    if backend != "oss":
        errors.append("ASSET_STORAGE_BACKEND 在生产环境必须为 oss")
        return
    for name, value in (
        ("OSS_ENDPOINT", config.oss_endpoint),
        ("OSS_BUCKET_NAME", config.oss_bucket_name),
        ("OSS_ACCESS_KEY_ID", config.oss_access_key_id),
        ("OSS_ACCESS_KEY_SECRET", config.oss_access_key_secret),
    ):
        _require(errors, name, value)
    _validate_range(
        errors,
        "OSS_SIGNED_URL_EXPIRE_SECONDS",
        config.oss_signed_url_expire_seconds,
        60,
        86400,
    )


def _validate_provider_limits(errors: list[str], config: Settings) -> None:
    _validate_range(
        errors, "PROVIDER_CONNECT_TIMEOUT_SECONDS", config.provider_connect_timeout_seconds, 0.1, 120
    )
    _validate_range(
        errors, "PROVIDER_READ_TIMEOUT_SECONDS", config.provider_read_timeout_seconds, 1, 3600
    )
    _validate_range(
        errors, "GENERATED_ASSET_MAX_BYTES", config.generated_asset_max_bytes, 1024, 1024**3
    )
    _validate_range(
        errors,
        "USER_IMAGE_UPLOAD_MAX_BYTES",
        config.user_image_upload_max_bytes,
        1024,
        100 * 1024 * 1024,
    )
    _validate_range(
        errors,
        "GENERATION_INPUT_IMAGES_MAX_TOTAL_BYTES",
        config.generation_input_images_max_total_bytes,
        1024,
        5 * 1024**3,
    )
    _validate_range(
        errors, "PROVIDER_RESPONSE_MAX_BYTES", config.provider_response_max_bytes, 1024, 1024**3
    )
    minimum_base64_response = ((config.generated_asset_max_bytes + 2) // 3) * 4
    if config.provider_response_max_bytes < minimum_base64_response:
        errors.append(
            "PROVIDER_RESPONSE_MAX_BYTES 必须至少容纳一个最大资产的 Base64 编码"
        )
    _validate_range(errors, "PROVIDER_MAX_OUTPUTS", config.provider_max_outputs, 1, 16)


def _validate_trusted_proxies(errors: list[str], value: str) -> None:
    candidates = [item.strip() for item in value.split(",") if item.strip()]
    if not candidates:
        errors.append("AUTH_TRUSTED_PROXY_CIDRS 在生产环境必须显式配置")
        return
    for candidate in candidates:
        try:
            network = ipaddress.ip_network(candidate, strict=False)
        except ValueError:
            errors.append("AUTH_TRUSTED_PROXY_CIDRS 包含非法 CIDR")
            continue
        if network.prefixlen == 0:
            errors.append("AUTH_TRUSTED_PROXY_CIDRS 不允许信任整个互联网")


def _validate_bootstrap(errors: list[str], config: Settings) -> None:
    email = config.admin_bootstrap_email.strip()
    password = config.admin_bootstrap_password
    if not config.admin_bootstrap_enabled:
        if email or password:
            errors.append(
                "ADMIN_BOOTSTRAP_ENABLED=false 时管理员 Bootstrap 邮箱和密码必须为空"
            )
        return
    if not email:
        errors.append("ADMIN_BOOTSTRAP_EMAIL 缺失")
    else:
        try:
            validate_email(email, check_deliverability=False)
        except EmailNotValidError:
            errors.append("ADMIN_BOOTSTRAP_EMAIL 必须是有效邮箱")
    if len(password) < 16:
        errors.append("ADMIN_BOOTSTRAP_PASSWORD 必须至少 16 个字符")
    if not any(character.isupper() for character in password):
        errors.append("ADMIN_BOOTSTRAP_PASSWORD 必须包含大写字母")
    if not any(character.islower() for character in password):
        errors.append("ADMIN_BOOTSTRAP_PASSWORD 必须包含小写字母")
    if not any(character.isdigit() for character in password):
        errors.append("ADMIN_BOOTSTRAP_PASSWORD 必须包含数字")


def validate_runtime_settings(role: str, config: Settings = settings) -> None:
    if role not in {"user-api", "admin-api", "worker", "migration"}:
        raise RuntimeConfigurationError(f"未知运行时角色：{role}")
    if config.app_environment.strip().lower() != "production":
        return

    errors: list[str] = []
    _validate_database_url(errors, config.database_url)
    if role == "user-api":
        _validate_secrets(
            errors,
            (
                ("JWT_SECRET", config.jwt_secret),
                ("USER_MODEL_CREDENTIALS_SECRET", config.user_model_credentials_secret),
            ),
        )
        if not config.auth_cookie_secure:
            errors.append("AUTH_COOKIE_SECURE 在生产环境必须为 true")
        _validate_range(errors, "JWT_EXPIRE_MINUTES", config.jwt_expire_minutes, 1, 1440)
        _validate_range(errors, "AUTH_RATE_LIMIT_ATTEMPTS", config.auth_rate_limit_attempts, 1, 100)
        _validate_range(
            errors, "AUTH_RATE_LIMIT_WINDOW_SECONDS", config.auth_rate_limit_window_seconds, 1, 3600
        )
        _validate_trusted_proxies(errors, config.auth_trusted_proxy_cidrs)
        _validate_storage(errors, config)
        _validate_provider_limits(errors, config)
    if role == "worker":
        _validate_secrets(
            errors,
            (("USER_MODEL_CREDENTIALS_SECRET", config.user_model_credentials_secret),),
        )
        _validate_storage(errors, config)
        _validate_provider_limits(errors, config)
        _validate_range(
            errors, "GENERATION_WORKER_POLL_SECONDS", config.generation_worker_poll_seconds, 0.1, 60
        )
        _validate_range(
            errors, "GENERATION_LEASE_SECONDS", config.generation_lease_seconds, 10, 3600
        )
        _validate_range(
            errors, "GENERATION_HEARTBEAT_SECONDS", config.generation_heartbeat_seconds, 1, 600
        )
        _validate_range(
            errors, "GENERATION_MAX_ATTEMPTS", config.generation_max_attempts, 1, 10
        )
        if config.generation_heartbeat_seconds >= config.generation_lease_seconds:
            errors.append("GENERATION_HEARTBEAT_SECONDS 必须小于 GENERATION_LEASE_SECONDS")
    if role == "admin-api":
        _validate_secrets(errors, (("ADMIN_JWT_SECRET", config.admin_jwt_secret),))
        if not config.admin_cookie_secure:
            errors.append("ADMIN_COOKIE_SECURE 在生产环境必须为 true")
        _validate_range(
            errors, "ADMIN_JWT_EXPIRE_MINUTES", config.admin_jwt_expire_minutes, 1, 10080
        )
        _validate_bootstrap(errors, config)

    configured_secrets = [
        (name, value)
        for name, value in (
            ("JWT_SECRET", config.jwt_secret),
            ("ADMIN_JWT_SECRET", config.admin_jwt_secret),
            ("USER_MODEL_CREDENTIALS_SECRET", config.user_model_credentials_secret),
        )
        if not _is_placeholder(value)
    ]
    seen: dict[str, str] = {}
    for name, value in configured_secrets:
        if value in seen:
            errors.append(f"{name} 不能与 {seen[value]} 使用相同密钥")
        seen[value] = name

    if errors:
        raise RuntimeConfigurationError("生产运行配置校验失败：" + "；".join(dict.fromkeys(errors)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ThinkTV 运行时配置校验")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("role", choices=("user-api", "admin-api", "worker", "migration"))
    args = parser.parse_args(argv)
    try:
        validate_runtime_settings(args.role)
    except RuntimeConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"运行时配置校验通过：{args.role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
