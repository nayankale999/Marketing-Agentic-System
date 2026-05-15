"""Async SQLAlchemy engine, sessionmaker, and tenant-context helpers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.settings.config import get_settings

_settings = get_settings()

engine = create_async_engine(_settings.database_url, future=True, pool_pre_ping=True)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield a session inside a commit/rollback boundary."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def set_tenant_context(session: AsyncSession, tenant_id: UUID) -> None:
    """Switch the active role to mas_app and stamp app.tenant_id for RLS.

    Must be called inside an active transaction; SET LOCAL / set_config(..., true)
    are transaction-scoped and revert at COMMIT/ROLLBACK.
    """
    await session.execute(text("SET LOCAL ROLE mas_app"))
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )
