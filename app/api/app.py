"""FastAPI application factory."""

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.api import _protected, audiences, auth, campaigns, health, integrations, me
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
    application.include_router(audiences.router)
    init_observability(app=application)
    return application


app = create_app()
