"""FastAPI dependencies for auth, role enforcement, and tenant-scoped sessions."""

from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models import AppUser
from app.db.session import SessionLocal, set_tenant_context


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield an unscoped async DB session (admin/auth bootstrap; bypasses RLS)."""
    async with SessionLocal() as session:
        yield session


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AppUser:
    """Resolve the signed-in user from the session cookie."""
    try:
        user_id_str = request.session.get("user_id")
    except (AssertionError, AttributeError):
        user_id_str = None
    if not user_id_str:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user = await db.get(AppUser, UUID(user_id_str))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user


async def get_tenant_db(
    current: AppUser = Depends(get_current_user),
) -> AsyncIterator[AsyncSession]:
    """Yield an RLS-scoped session bound to the current user's tenant.

    Wraps the request body in a single transaction so SET LOCAL ROLE + the
    set_config('app.tenant_id', ...) call cover every query the handler runs.
    Auto-commits on clean return; rolls back on exception.
    """
    async with SessionLocal() as session:
        try:
            await set_tenant_context(session, current.tenant_id)
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


_ROLE_RANK: dict[UserRole, int] = {
    UserRole.viewer: 0,
    UserRole.marketer: 1,
    UserRole.manager: 2,
    UserRole.admin: 3,
}


def require_role(minimum: UserRole) -> Callable[..., Awaitable[AppUser]]:
    """Build a FastAPI dependency that enforces `current_user.role >= minimum`."""

    async def _dep(current: AppUser = Depends(get_current_user)) -> AppUser:
        if _ROLE_RANK[current.role] < _ROLE_RANK[minimum]:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail=f"requires role >= {minimum.value}",
            )
        return current

    # Marker the route-coverage CI test scans for.
    _dep.__role_dep__ = True  # type: ignore[attr-defined]
    return _dep
