"""Application settings loaded from environment + .env.local."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.local",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Database
    database_url: str = "postgresql+asyncpg://mas:mas@localhost:5434/mas"

    # OIDC (dev defaults; override via env in prod)
    oidc_issuer: str = "http://localhost:9000"
    oidc_client_id: str = "mas-dev"
    oidc_client_secret: str = "dev-secret"
    oidc_redirect_uri: str = "http://localhost:8001/api/auth/callback"

    # Anthropic
    anthropic_api_key: str = ""

    # OpenTelemetry
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_service_name: str = "mas-api"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
