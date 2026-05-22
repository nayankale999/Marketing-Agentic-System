"""Compliance rule CRUD — admin-only (W23, E06-S08).

These are the per-tenant suppression patterns the Content Creator checks
every draft against. Universal forbidden patterns (guarantees, medical
claims) live in code under `app.agents._compliance` and apply to every
tenant; this table is purely additive.

Admin-only because compliance configuration touches legal/regulatory
posture — marketers can read drafts and edit briefs, not compliance gates.
"""

import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_role
from app.api.schemas.compliance import (
    ComplianceRuleCreate,
    ComplianceRuleListResponse,
    ComplianceRuleOut,
)
from app.db.enums import CompliancePatternKind, UserRole
from app.db.models import AppUser, ComplianceRule

router = APIRouter(prefix="/api/compliance-rules", tags=["compliance"])


@router.post(
    "",
    response_model=ComplianceRuleOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_compliance_rule(
    body: ComplianceRuleCreate,
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> ComplianceRule:
    # Validate the regex at create time so a malformed pattern doesn't
    # silently break every future draft compliance check (E06-S08 #4).
    if body.pattern_kind == CompliancePatternKind.regex:
        try:
            re.compile(body.keyword)
        except re.error as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"keyword is not a valid regex: {exc}",
            ) from exc

    rule = ComplianceRule(
        tenant_id=user.tenant_id,
        keyword=body.keyword,
        pattern_kind=body.pattern_kind.value,
        severity=body.severity.value,
        description=body.description,
    )
    db.add(rule)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"a compliance rule for '{body.keyword}' already exists",
        ) from exc
    await db.refresh(rule)
    return rule


@router.get("", response_model=ComplianceRuleListResponse)
async def list_compliance_rules(
    _user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> ComplianceRuleListResponse:
    rows = (
        await db.execute(
            select(ComplianceRule).order_by(
                ComplianceRule.severity.desc(),  # 'warn' < 'block' alphabetically? actually warn comes first; we want block first.
                ComplianceRule.keyword.asc(),
            )
        )
    ).scalars().all()
    # Order by severity manually so 'block' rows surface first regardless of
    # alphabetical accident.
    rows = sorted(rows, key=lambda r: (0 if r.severity == "block" else 1, r.keyword))
    return ComplianceRuleListResponse(
        items=[ComplianceRuleOut.model_validate(r) for r in rows],
        total=len(rows),
    )


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_compliance_rule(
    rule_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> None:
    rule = await db.get(ComplianceRule, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="rule not found")
    await db.delete(rule)
    await db.flush()
