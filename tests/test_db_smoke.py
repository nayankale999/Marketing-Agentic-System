"""Integration smoke test: create a tenant + app_user and read it back (W2 exit)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models import AppUser, Tenant


async def test_tenant_and_user_roundtrip(db_session: AsyncSession) -> None:
    tenant = Tenant(name="Acme Inc", domain="acme.test")
    db_session.add(tenant)
    await db_session.flush()
    assert tenant.id is not None

    user = AppUser(
        tenant_id=tenant.id,
        email="founder@acme.test",
        display_name="Founder",
        role=UserRole.admin,
    )
    db_session.add(user)
    await db_session.commit()

    result = await db_session.execute(select(AppUser).where(AppUser.email == "founder@acme.test"))
    found = result.scalar_one()
    assert found.tenant_id == tenant.id
    assert found.role == UserRole.admin
    assert found.is_active is True
