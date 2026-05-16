"""Shared pytest fixtures."""

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer

from app.api.app import app
from app.api.deps import get_current_user, get_db, get_tenant_db
from app.audit import register_listeners
from app.db.models import AppUser
from app.db.session import set_tenant_context

register_listeners()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:15-alpine", driver="asyncpg") as pg:
        yield pg


@pytest.fixture(scope="session")
def migrated_db(postgres_container: PostgresContainer) -> str:
    """Spin up Postgres, run Alembic upgrade head, return the connection URL."""
    url = postgres_container.get_connection_url()
    os.environ["DATABASE_URL"] = url
    command.upgrade(Config("alembic.ini"), "head")
    return url


@pytest_asyncio.fixture
async def db_engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    """Fresh engine per test (cheap) bound to the migrated test DB.

    Function-scoped to keep the engine on the test's own event loop -- avoids
    pytest-asyncio's session-vs-function loop mismatch with asyncpg pools.
    """
    engine = create_async_engine(migrated_db, future=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Per-test async session (runs as DB owner, so RLS is bypassed)."""
    session_maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_maker() as session:
        yield session


@pytest.fixture
def override_api_db(db_engine: AsyncEngine) -> Iterator[None]:
    """Point `get_db` and `get_tenant_db` at the test engine for the duration.

    The app's `SessionLocal` is bound at import time to whatever DATABASE_URL
    pointed at then (typically the dev compose Postgres). Tests that hit the
    FastAPI surface need to redirect the DB deps to the testcontainer.
    """
    test_maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def _test_get_db() -> AsyncIterator[AsyncSession]:
        async with test_maker() as session:
            yield session

    async def _test_get_tenant_db(
        current: AppUser = Depends(get_current_user),
    ) -> AsyncIterator[AsyncSession]:
        async with test_maker() as session:
            try:
                await set_tenant_context(session, current.tenant_id)
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _test_get_db
    app.dependency_overrides[get_tenant_db] = _test_get_tenant_db
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_tenant_db, None)
