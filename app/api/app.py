"""FastAPI application factory."""

from fastapi import FastAPI

from app.api import health


def create_app() -> FastAPI:
    application = FastAPI(title="Marketing Agentic System", version="0.0.1")
    application.include_router(health.router)
    return application


app = create_app()
