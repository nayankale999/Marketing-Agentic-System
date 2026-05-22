"""Brand-voice endpoints (W19, E06-S02).

  - POST   /api/brand-voices                — create a voice
  - GET    /api/brand-voices                — list voices for the tenant
  - GET    /api/brand-voices/active         — fetch the currently-active voice
  - GET    /api/brand-voices/{id}           — single voice detail
  - PATCH  /api/brand-voices/{id}           — update fields
  - POST   /api/brand-voices/{id}/activate  — make this voice the active one

The activate path is the only piece with non-trivial concurrency: the partial
unique index `(tenant_id) WHERE is_active` enforces "at most one active per
tenant", so we deactivate any current winner in the same transaction before
flipping this row on. The PATCH path doesn't change `is_active` — activation
is its own endpoint so the audit_log entries clearly separate "edited the
voice text" from "switched which voice the agents use".
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_role
from app.api.schemas.brand_voice import (
    BrandVoiceCreate,
    BrandVoiceListResponse,
    BrandVoiceOut,
    BrandVoicePatch,
)
from app.audit.context import current_actor_id, current_actor_kind
from app.audit.writer import column_snapshot, write_audit
from app.db.enums import UserRole
from app.db.models import AppUser, BrandVoice

router = APIRouter(prefix="/api/brand-voices", tags=["brand-voice"])


@router.post(
    "",
    response_model=BrandVoiceOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_brand_voice(
    body: BrandVoiceCreate,
    user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> BrandVoice:
    # If the caller wants this row born active, kick off any existing winner
    # first so the partial unique index doesn't reject the insert.
    if body.is_active:
        await _deactivate_others(db, tenant_id=user.tenant_id, except_id=None)

    voice = BrandVoice(
        tenant_id=user.tenant_id,
        name=body.name,
        is_active=body.is_active,
        tone_descriptors=list(body.tone_descriptors),
        do_words=list(body.do_words),
        dont_words=list(body.dont_words),
        sample_paragraphs=list(body.sample_paragraphs),
        reading_grade_target=body.reading_grade_target,
    )
    db.add(voice)
    try:
        await db.flush()
    except IntegrityError as exc:
        # uq_brand_voice_tenant_name collision — friendlier 409 than a 500.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"a brand voice named '{body.name}' already exists",
        ) from exc
    await db.refresh(voice)
    return voice


@router.get("", response_model=BrandVoiceListResponse)
async def list_brand_voices(
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> BrandVoiceListResponse:
    rows = (
        await db.execute(
            select(BrandVoice).order_by(BrandVoice.is_active.desc(), BrandVoice.name.asc())
        )
    ).scalars().all()
    return BrandVoiceListResponse(
        items=[BrandVoiceOut.model_validate(r) for r in rows],
        total=len(rows),
    )


@router.get("/active", response_model=BrandVoiceOut)
async def get_active_brand_voice(
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> BrandVoice:
    row = (
        await db.execute(select(BrandVoice).where(BrandVoice.is_active.is_(True)))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="no active brand voice configured",
        )
    return row


@router.get("/{voice_id}", response_model=BrandVoiceOut)
async def get_brand_voice(
    voice_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> BrandVoice:
    row = await db.get(BrandVoice, voice_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="brand voice not found")
    return row


@router.patch("/{voice_id}", response_model=BrandVoiceOut)
async def patch_brand_voice(
    voice_id: UUID,
    body: BrandVoicePatch,
    _user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> BrandVoice:
    voice = await db.get(BrandVoice, voice_id)
    if voice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="brand voice not found")

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        return voice

    before = column_snapshot(voice)
    for field, value in updates.items():
        setattr(voice, field, value)
    voice.updated_at = datetime.now(UTC)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="a brand voice with that name already exists",
        ) from exc
    after = column_snapshot(voice)

    write_audit(
        db,
        tenant_id=voice.tenant_id,
        actor_kind=current_actor_kind.get(),
        actor_id=current_actor_id.get(),
        entity_kind="brand_voice",
        entity_id=voice.id,
        action="updated",
        before_state=before,
        after_state=after,
        metadata={"fields": sorted(updates.keys())},
    )
    return voice


@router.post("/{voice_id}/activate", response_model=BrandVoiceOut)
async def activate_brand_voice(
    voice_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> BrandVoice:
    voice = await db.get(BrandVoice, voice_id)
    if voice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="brand voice not found")

    if voice.is_active:
        return voice  # idempotent

    before = column_snapshot(voice)
    await _deactivate_others(db, tenant_id=voice.tenant_id, except_id=voice.id)
    voice.is_active = True
    voice.updated_at = datetime.now(UTC)
    await db.flush()
    after = column_snapshot(voice)

    write_audit(
        db,
        tenant_id=voice.tenant_id,
        actor_kind=current_actor_kind.get(),
        actor_id=current_actor_id.get(),
        entity_kind="brand_voice",
        entity_id=voice.id,
        action="activated",
        before_state=before,
        after_state=after,
        metadata={},
    )
    return voice


async def _deactivate_others(
    db: AsyncSession, *, tenant_id: UUID, except_id: UUID | None
) -> None:
    """Set is_active=false on every voice for the tenant except `except_id`.
    Required before flipping a different voice on, because the partial unique
    index on (tenant_id) WHERE is_active would otherwise reject the insert/
    update."""
    stmt = (
        update(BrandVoice)
        .where(BrandVoice.tenant_id == tenant_id, BrandVoice.is_active.is_(True))
        .values(is_active=False, updated_at=func.now())
    )
    if except_id is not None:
        stmt = stmt.where(BrandVoice.id != except_id)
    await db.execute(stmt)
