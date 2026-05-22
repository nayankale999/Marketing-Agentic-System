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

    # OIDC (dev defaults point at the local mock-oauth2-server at /default;
    # override via env for Google Workspace in prod)
    oidc_issuer: str = "http://localhost:9000/default"
    oidc_client_id: str = "mas-dev"
    oidc_client_secret: str = "dev-secret"
    oidc_redirect_uri: str = "http://localhost:8001/api/auth/callback"

    # Session cookie signing (override in prod via env)
    session_secret: str = "dev-only-not-a-real-secret"

    # Fernet key for `integration_credential.encrypted_payload` (44-char
    # url-safe base64 of 32 bytes). MUST be overridden in prod via env;
    # generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
    credentials_secret: str = "SwVcuvR4muCCE69YjMmD7zNeoSl5aknPRm9FCB4eSOc="

    # HubSpot OAuth (CRM connector — W11)
    hubspot_client_id: str = ""
    hubspot_client_secret: str = ""
    hubspot_redirect_uri: str = "http://localhost:8001/api/integrations/hubspot/callback"

    # Plausible Analytics (web analytics connector — W16)
    plausible_api_key: str = ""
    plausible_site_id: str = ""

    # Anthropic
    anthropic_api_key: str = ""
    # Default model for the `copywriting.generate` tool. Override per-tenant or
    # per-deployment via env (e.g. claude-opus-4-7 for higher-quality drafts).
    copywriting_model: str = "claude-sonnet-4-6"
    # Default model for the Campaign Strategist agent (W20). Same conditional-
    # registration story as copywriting — requires anthropic_api_key.
    strategist_model: str = "claude-sonnet-4-6"

    # OpenTelemetry
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_service_name: str = "mas-api"

    # Audience member freshness window (E01-S05). Members whose `fetched_at`
    # is older than this are flagged stale in the audience composition view.
    audience_member_freshness_ttl_days: int = 30

    # Asset preview share-link signing (W24, E06-S07 #4). Empty falls back to
    # `session_secret` for dev convenience; in prod override so share-link
    # tokens don't share signing material with session cookies.
    preview_share_secret: str = ""
    preview_share_ttl_days: int = 7

    def effective_preview_share_secret(self) -> str:
        return self.preview_share_secret or self.session_secret


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
