"""W5: audit_log auto-write on insert + append-only enforcement.

The ORM event listener (app/audit/listeners.py) writes an audit_log row in
the same transaction as every tracked INSERT. mas_app can SELECT but lacks
UPDATE/DELETE on audit_log (migration 0004).
"""

import uuid

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.enums import UserRole
from app.db.models import AppUser, AuditLog, Tenant


async def test_tenant_insert_writes_audit_row(db_engine: AsyncEngine) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as setup:
        tenant = Tenant(name=f"audit-{uuid.uuid4().hex[:8]}")
        setup.add(tenant)
        await setup.commit()
        tid = tenant.id

    async with AsyncSession(db_engine, expire_on_commit=False) as q:
        result = await q.execute(select(AuditLog).where(AuditLog.entity_id == tid))
        rows = result.scalars().all()

    assert len(rows) == 1
    row = rows[0]
    assert row.entity_kind == "tenant"
    assert row.action == "created"
    assert row.actor_kind == "system"
    assert row.after_state is not None
    assert row.after_state["id"] == str(tid)
    assert row.after_state["name"].startswith("audit-")


async def test_app_user_insert_writes_audit_row(db_engine: AsyncEngine) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as setup:
        tenant = Tenant(name=f"u-{uuid.uuid4().hex[:8]}")
        setup.add(tenant)
        await setup.flush()
        user = AppUser(
            tenant_id=tenant.id,
            email=f"u-{uuid.uuid4().hex[:8]}@audit.test",
            role=UserRole.marketer,
        )
        setup.add(user)
        await setup.commit()
        user_id = user.id
        tenant_id = tenant.id

    async with AsyncSession(db_engine, expire_on_commit=False) as q:
        result = await q.execute(select(AuditLog).where(AuditLog.entity_id == user_id))
        rows = result.scalars().all()

    assert len(rows) == 1
    row = rows[0]
    assert row.entity_kind == "app_user"
    assert row.tenant_id == tenant_id


async def test_mas_app_cannot_update_audit_log(db_engine: AsyncEngine) -> None:
    """Migration 0004 revoked UPDATE on audit_log from mas_app."""
    async with AsyncSession(db_engine, expire_on_commit=False) as setup:
        tenant = Tenant(name=f"perm-{uuid.uuid4().hex[:8]}")
        setup.add(tenant)
        await setup.commit()
        tid = tenant.id

    async with AsyncSession(db_engine, expire_on_commit=False) as scoped, scoped.begin():
        await scoped.execute(text("SET LOCAL ROLE mas_app"))
        await scoped.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tid)},
        )
        with pytest.raises(DBAPIError, match="permission denied"):
            await scoped.execute(
                update(AuditLog).where(AuditLog.entity_id == tid).values(action="tampered")
            )


async def test_mas_app_cannot_delete_audit_log(db_engine: AsyncEngine) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as setup:
        tenant = Tenant(name=f"del-{uuid.uuid4().hex[:8]}")
        setup.add(tenant)
        await setup.commit()
        tid = tenant.id

    async with AsyncSession(db_engine, expire_on_commit=False) as scoped, scoped.begin():
        await scoped.execute(text("SET LOCAL ROLE mas_app"))
        await scoped.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tid)},
        )
        with pytest.raises(DBAPIError, match="permission denied"):
            await scoped.execute(
                text("DELETE FROM audit_log WHERE entity_id = :eid"),
                {"eid": str(tid)},
            )
