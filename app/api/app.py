"""FastAPI application factory."""

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.api import _protected, auth, campaigns, health, me
from app.audit import register_listeners
from app.settings.config import get_settings


def create_app() -> FastAPI:
    register_listeners()
    settings = get_settings()
    application = FastAPI(title="Marketing Agentic System", version="0.0.1")
    application.add_middleware(SessionMiddleware, secret_key=settings.session_secret)
    application.include_router(health.router)
    application.include_router(auth.router)
    application.include_router(me.router)
    application.include_router(_protected.router)
    application.include_router(campaigns.router)
    return application


app = create_app()
