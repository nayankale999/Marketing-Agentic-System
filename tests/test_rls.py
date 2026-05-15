"""W4: row-level security keeps tenants isolated.

These tests run against the testcontainer DB. Setup uses the owner role (RLS
bypassed) to insert rows for two tenants. Verification opens a fresh session,
calls `SET LOCAL ROLE mas_app` + `set_config('app.tenant_id', ..., true)`, and
asserts that only the matching tenant's rows are visible.
"""

import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.enums import UserRole
from app.db.models import AppUser, Tenant


def _new_tenant(label: str) -> Tenant:
    """Tenant with a unique name to avoid collisions across test re-runs."""
    return Tenant(name=f"{label}-{uuid.uuid4().hex[:8]}")


async def _set_tenant(session: AsyncSession, tid: uuid.UUID) -> None:
    await session.execute(text("SET LOCAL ROLE mas_app"))
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tid)},
    )


async def test_rls_filters_cross_tenant_reads(db_engine: AsyncEngine) -> None:
    # Setup: two tenants + one user each, committed as the DB owner.
    async with AsyncSession(db_engine, expire_on_commit=False) as setup:
        t1 = _new_tenant("t1")
        t2 = _new_tenant("t2")
        setup.add_all([t1, t2])
        await setup.flush()
        u1 = AppUser(
            tenant_id=t1.id,
            email=f"u1-{uuid.uuid4().hex[:8]}@t1.test",
            role=UserRole.admin,
        )
        u2 = AppUser(
            tenant_id=t2.id,
            email=f"u2-{uuid.uuid4().hex[:8]}@t2.test",
            role=UserRole.admin,
        )
        setup.add_all([u1, u2])
        await setup.commit()
        t1_id, t2_id, u1_id, u2_id = t1.id, t2.id, u1.id, u2.id

    # As tenant 1 (mas_app + app.tenant_id=t1.id): only u1 visible.
    async with AsyncSession(db_engine, expire_on_commit=False) as scoped, scoped.begin():
        await _set_tenant(scoped, t1_id)
        result = await scoped.execute(select(AppUser).where(AppUser.id.in_([u1_id, u2_id])))
        visible = result.scalars().all()
        assert [u.id for u in visible] == [u1_id]

    # As tenant 2: only u2 visible.
    async with AsyncSession(db_engine, expire_on_commit=False) as scoped, scoped.begin():
        await _set_tenant(scoped, t2_id)
        result = await scoped.execute(select(AppUser).where(AppUser.id.in_([u1_id, u2_id])))
        visible = result.scalars().all()
        assert [u.id for u in visible] == [u2_id]


async def test_rls_blocks_unscoped_role(db_engine: AsyncEngine) -> None:
    """With mas_app active but no app.tenant_id set, every domain SELECT is empty."""
    # Make sure at least one tenant row exists.
    async with AsyncSession(db_engine, expire_on_commit=False) as setup:
        setup.add(_new_tenant("isolation"))
        await setup.commit()

    async with AsyncSession(db_engine, expire_on_commit=False) as scoped, scoped.begin():
        await scoped.execute(text("SET LOCAL ROLE mas_app"))
        # Deliberately do NOT call set_config('app.tenant_id', ...).
        result = await scoped.execute(select(Tenant))
        assert result.scalars().all() == []


async def test_rls_tenant_table_self_filters(db_engine: AsyncEngine) -> None:
    """The `tenant` table policy matches by id (not tenant_id) -- only the
    current tenant's own row should be visible."""
    async with AsyncSession(db_engine, expire_on_commit=False) as setup:
        t_self = _new_tenant("self")
        t_other = _new_tenant("other")
        setup.add_all([t_self, t_other])
        await setup.commit()
        self_id, other_id = t_self.id, t_other.id

    async with AsyncSession(db_engine, expire_on_commit=False) as scoped, scoped.begin():
        await _set_tenant(scoped, self_id)
        result = await scoped.execute(select(Tenant).where(Tenant.id.in_([self_id, other_id])))
        visible = result.scalars().all()
        assert [t.id for t in visible] == [self_id]
