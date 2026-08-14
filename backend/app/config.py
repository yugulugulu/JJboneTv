from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://thinktv:thinktv@postgres:5432/thinktv"
    database_pool_size: int = 5
    database_max_overflow: int = 2
    model_api_base_url: str = ""
    model_api_key: str = ""
    jwt_secret: str = "change-me-in-production"
    jwt_expire_minutes: int = 1440
    auth_cookie_secure: bool = False
    admin_jwt_secret: str = "change-me-admin-in-production"
    admin_jwt_expire_minutes: int = 60
    admin_cookie_secure: bool = False
    admin_bootstrap_enabled: bool = False
    admin_bootstrap_email: str = ""
    admin_bootstrap_password: str = ""
    user_model_credentials_secret: str = "change-user-model-credentials-secret-in-production"
    app_environment: str = "development"
    app_public_base_url: str = ""
    asset_storage_backend: str = "local"
    generated_storage_root: str = "/app/storage/generated"
    oss_endpoint: str = ""
    oss_internal_endpoint: str = ""
    oss_bucket_name: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    oss_security_token: str = ""
    oss_prefix: str = "thinktv/generated"
    oss_cname: bool = False
    oss_signed_url_expire_seconds: int = 3600
    provider_connect_timeout_seconds: float = 15
    provider_read_timeout_seconds: float = 300
    generated_asset_max_bytes: int = 30 * 1024 * 1024
    generated_video_asset_max_bytes: int = 200 * 1024 * 1024
    user_image_upload_max_bytes: int = 20 * 1024 * 1024
    user_video_upload_max_bytes: int = 200 * 1024 * 1024
    user_audio_upload_max_bytes: int = 50 * 1024 * 1024
    generation_input_images_max_total_bytes: int = 100 * 1024 * 1024
    provider_response_max_bytes: int = 48 * 1024 * 1024
    provider_max_outputs: int = 4
    provider_video_poll_seconds: float = 5.0
    image_active_window: int = 20
    image_provider_concurrency: int = 10
    image_result_concurrency: int = 3
    user_image_active_limit: int = 4
    video_active_limit: int = 10
    video_submit_concurrency: int = 4
    video_poll_concurrency: int = 4
    user_video_active_limit: int = 2
    video_provider_timeout_seconds: int = 3600
    video_poll_connect_timeout_seconds: float = 3
    video_poll_read_timeout_seconds: float = 10
    provider_retry_max_attempts: int = 3
    provider_retry_max_delay_seconds: int = 60
    auth_rate_limit_attempts: int = 5
    auth_rate_limit_window_seconds: int = 60
    auth_trusted_proxy_cidrs: str = ""
    generation_worker_poll_seconds: float = 1.0
    generation_lease_seconds: int = 90
    generation_heartbeat_seconds: int = 30
    generation_max_attempts: int = 3
    trash_retention_days: int = 30
    trash_purge_interval_seconds: int = 300

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
