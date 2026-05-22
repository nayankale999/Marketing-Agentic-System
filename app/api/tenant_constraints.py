"""Tenant constraint CRUD — admin-only (W20, E05-S05).

These rows are guardrails the Strategist must respect (forbidden channels,
hard caps). Admin-only by AC #4: "Given I lack admin role, when I try to edit
constraints, then the action is denied."

List/get is also admin-only — there's no business reason for non-admins to
see constraint configuration; it doesn't shape any UI a marketer interacts
with directly.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_role
from app.api.schemas.tenant_constraint import (
    TenantConstraintCreate,
    TenantConstraintListResponse,
    TenantConstraintOut,
)
from app.db.enums import UserRole
from app.db.models import AppUser, TenantConstraint

router = APIRouter(prefix="/api/tenant-constraints", tags=["tenant-constraint"])


@router.post(
    "",
    response_model=TenantConstraintOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_tenant_constraint(
    body: TenantConstraintCreate,
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> TenantConstraint:
    row = TenantConstraint(
        tenant_id=user.tenant_id,
        kind=body.kind.value,
        payload=dict(body.payload),
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


@router.get("", response_model=TenantConstraintListResponse)
async def list_tenant_constraints(
    _user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> TenantConstraintListResponse:
    rows = (
        await db.execute(
            select(TenantConstraint).order_by(
                TenantConstraint.kind.asc(), TenantConstraint.created_at.asc()
            )
        )
    ).scalars().all()
    return TenantConstraintListResponse(
        items=[TenantConstraintOut.model_validate(r) for r in rows],
        total=len(rows),
    )


@router.delete("/{constraint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tenant_constraint(
    constraint_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> None:
    row = await db.get(TenantConstraint, constraint_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="constraint not found")
    await db.delete(row)
    await db.flush()
