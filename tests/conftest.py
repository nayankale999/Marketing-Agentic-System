"""Shared pytest fixtures."""

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from app.api.app import app


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
    # Alembic env.py reads DATABASE_URL; ensure it points at the container.
    os.environ["DATABASE_URL"] = url
    command.upgrade(Config("alembic.ini"), "head")
    return url


@pytest_asyncio.fixture
async def db_session(migrated_db: str) -> AsyncIterator[AsyncSession]:
    """Per-test async session bound to the migrated test database."""
    engine = create_async_engine(migrated_db, future=True)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_maker() as session:
        yield session
    await engine.dispose()
