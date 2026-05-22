"""FastAPI application factory."""

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.api import (
    _protected,
    ab_tests,
    approval_settings,
    approvals,
    audiences,
    auth,
    brand_voice,
    campaigns,
    compliance_rules,
    compliance_settings,
    content_assets,
    distribution,
    frequency_caps,
    health,
    ingest,
    integrations,
    integrations_email,
    integrations_social,
    me,
    preview,
    provider_rate_limits,
    strategy,
    tenant_constraints,
    unsubscribe,
)
from app.audit import register_listeners
from app.observability import init_observability, tag_span_with_actor
from app.settings.config import get_settings


def create_app() -> FastAPI:
    register_listeners()
    settings = get_settings()
    application = FastAPI(title="Marketing Agentic System", version="0.0.1")
    application.add_middleware(SessionMiddleware, secret_key=settings.session_secret)
    # Order matters: SessionMiddleware runs first (innermost in starlette stack),
    # so by the time tag_span_with_actor runs the session is available.
    application.add_middleware(BaseHTTPMiddleware, dispatch=tag_span_with_actor)
    application.include_router(health.router)
    application.include_router(auth.router)
    application.include_router(me.router)
    application.include_router(_protected.router)
    application.include_router(campaigns.router)
    application.include_router(integrations.router)
    application.include_router(audiences.campaigns_router)
    application.include_router(audiences.audiences_router)
    application.include_router(ingest.router)
    application.include_router(brand_voice.router)
    application.include_router(strategy.campaigns_router)
    application.include_router(strategy.proposals_router)
    application.include_router(strategy.touchpoints_router)
    application.include_router(content_assets.campaigns_router)
    application.include_router(content_assets.assets_router)
    application.include_router(ab_tests.campaigns_router)
    application.include_router(ab_tests.ab_tests_router)
    application.include_router(compliance_rules.router)
    application.include_router(preview.content_router)
    application.include_router(preview.public_router)
    application.include_router(approvals.router)
    application.include_router(approval_settings.router)
    application.include_router(integrations_email.router)
    application.include_router(integrations_social.router)
    application.include_router(distribution.router)
    application.include_router(frequency_caps.router)
    application.include_router(compliance_settings.router)
    application.include_router(unsubscribe.router)
    application.include_router(provider_rate_limits.router)
    application.include_router(tenant_constraints.router)
    init_observability(app=application)
    return application


app = create_app()
