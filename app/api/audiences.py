"""Audience upload + read endpoints.

W12 shipped the CSV upload (E01-S02 + E01-S03). W13 adds per-member
provenance + a GET endpoint that exposes a freshness summary (E01-S05).
E04 segmentation/ICP endpoints land alongside the segmentation tools in
W14-W15.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.audience_targeting import ensure_audience_targeting_agent
from app.api.deps import get_tenant_db, require_role
from app.api.schemas.audience import (
    AudienceDetail,
    AudienceFreshness,
    CsvRowErrorOut,
    CsvUploadResponse,
    CsvUploadSummary,
    EnqueueTaskResponse,
    EstimateResponse,
    MaterialiseAudienceRequest,
    SegmentationCriteriaIn,
)
from app.audiences.csv_upload import parse_csv
from app.audiences.segmentation import (
    SegmentationError,
    validate_criteria,
)
from app.audiences.segmentation import (
    estimate as segmentation_estimate,
)
from app.db.enums import TaskStatus, UserRole
from app.db.models import AppUser, Audience, AudienceMember, Campaign, Task
from app.orchestrator.queue import enqueue_task
from app.orchestrator.state_machine import _ensure_orchestrator_agent
from app.settings.config import get_settings

# Two routers because we have two URL prefixes (/api/campaigns/{id}/audiences/...
# for the upload, and /api/audiences/{id} for the read view).
campaigns_router = APIRouter(prefix="/api/campaigns", tags=["audiences"])
audiences_router = APIRouter(prefix="/api/audiences", tags=["audiences"])

# Hard caps for the sync upload path. Larger files should go through the
# async ingest task once that path lands.
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
_MAX_INLINE_ERRORS = 100


@campaigns_router.post(
    "/{campaign_id}/audiences/upload",
    response_model=CsvUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_audience_csv(
    campaign_id: UUID,
    file: Annotated[UploadFile, File(description="UTF-8 CSV with at least an `email` column")],
    name: Annotated[str | None, Form()] = None,
    user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> CsvUploadResponse:
    """E01-S02 — upload a seed list against an existing campaign.

    Each AudienceMember row records `source='csv'` + `fetched_at=now()`
    (E01-S05). An `ingest.csv_upload` task row is written alongside so the
    operator dashboard (E01-S06, `GET /api/ingest/jobs`) shows the run with
    counts + filename.
    """
    started_at = datetime.now(UTC)
    content = await file.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file exceeds {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit",
        )

    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="file is not valid UTF-8",
        ) from exc

    result = parse_csv(text)

    if result.total_rows == 0 and result.errors:
        # Bad header etc. — bail before creating an audience.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=result.errors[0].reason,
        )

    audience_name = name or f"CSV upload — {file.filename or 'unnamed'}"
    audience = Audience(
        tenant_id=user.tenant_id,
        campaign_id=campaign_id,
        name=audience_name,
        segment_criteria={
            "source": "csv",
            "filename": file.filename,
            "uploaded_at": started_at.isoformat(),
            "uploaded_by": str(user.id),
        },
        actual_size=len(result.valid),
        refreshed_at=started_at,
    )
    db.add(audience)
    await db.flush()

    if result.valid:
        db.add_all(
            AudienceMember(
                audience_id=audience.id,
                external_id=row.external_id,
                payload=row.payload,
                source="csv",
                fetched_at=started_at,
            )
            for row in result.valid
        )
        await db.flush()

    failed = sum(1 for e in result.errors if e.reason != "duplicate_in_file")
    skipped_dup = sum(1 for e in result.errors if e.reason == "duplicate_in_file")

    # Write an ingest-job row (E01-S06). Synchronous path -> already succeeded.
    completed_at = datetime.now(UTC)
    agent = await _ensure_orchestrator_agent(db, user.tenant_id)
    db.add(
        Task(
            tenant_id=user.tenant_id,
            campaign_id=campaign_id,
            agent_id=agent.id,
            skill_name="ingest.csv_upload",
            status=TaskStatus.succeeded,
            attempt=1,
            input_data={
                "filename": file.filename,
                "rows_attempted": result.total_rows,
                "audience_id": str(audience.id),
            },
            output_data={
                "imported": len(result.valid),
                "skipped_duplicate": skipped_dup,
                "failed": failed,
                "audience_id": str(audience.id),
            },
            scheduled_for=started_at,
            started_at=started_at,
            completed_at=completed_at,
        )
    )
    await db.flush()

    truncated = len(result.errors) > _MAX_INLINE_ERRORS
    errors_out = [
        CsvRowErrorOut(row=e.row, reason=e.reason, field=e.field, value=e.value)
        for e in result.errors[:_MAX_INLINE_ERRORS]
    ]

    return CsvUploadResponse(
        audience_id=audience.id,
        audience_name=audience.name,
        summary=CsvUploadSummary(
            total_rows=result.total_rows,
            imported=len(result.valid),
            skipped_duplicate=skipped_dup,
            failed=failed,
        ),
        errors=errors_out,
        errors_truncated=truncated,
    )


@campaigns_router.post(
    "/{campaign_id}/audiences",
    response_model=EnqueueTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_audience(
    campaign_id: UUID,
    body: MaterialiseAudienceRequest,
    user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> EnqueueTaskResponse:
    """E04-S01/S03 — enqueue a criteria-driven materialisation task.

    Poll `/api/ingest/jobs?status=succeeded` for completion; the new
    audience_id is in the task's `output_data`. The task runs under the
    per-tenant `audience_targeting` agent, registered by the queue handler.
    """
    # Surface validation errors at submit time, before the worker is involved.
    try:
        validate_criteria(body.criteria.model_dump())
    except SegmentationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": str(exc),
                "section": exc.section,
                "index": exc.index,
            },
        ) from exc

    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")

    agent = await ensure_audience_targeting_agent(db, user.tenant_id)
    task = await enqueue_task(
        db,
        tenant_id=user.tenant_id,
        agent_id=agent.id,
        campaign_id=campaign_id,
        skill_name="audience_targeting.materialise",
        input_data={
            "campaign_id": str(campaign_id),
            "audience_name": body.name,
            "criteria": body.criteria.model_dump(),
        },
    )
    return EnqueueTaskResponse(
        task_id=task.id,
        skill_name=task.skill_name,
        status=task.status.value,
    )


@audiences_router.post("/estimate", response_model=EstimateResponse)
async def estimate_audience(
    body: SegmentationCriteriaIn,
    user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> EstimateResponse:
    """E11-S04 — return how many tenant-wide contacts match the criteria,
    without persisting anything. Use before materialising a real audience.
    """
    try:
        result = await segmentation_estimate(
            db,
            tenant_id=user.tenant_id,
            criteria=body.model_dump(),
        )
    except SegmentationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": str(exc),
                "section": exc.section,
                "index": exc.index,
            },
        ) from exc

    return EstimateResponse(
        total_reachable=result.total_reachable,
        suppressed=result.suppressed,
        net=result.net,
    )


@audiences_router.get("/{audience_id}", response_model=AudienceDetail)
async def get_audience(
    audience_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> AudienceDetail:
    """E01-S05 — return audience metadata plus a freshness summary.

    `stale_member_count` is the count of members whose `fetched_at` is older
    than the configured TTL (`audience_member_freshness_ttl_days`). Members
    with NULL `fetched_at` are also considered stale — we have no way to
    know how current that data is.
    """
    audience = await db.get(Audience, audience_id)
    if audience is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="audience not found")

    settings = get_settings()
    ttl_days = settings.audience_member_freshness_ttl_days
    cutoff = datetime.now(UTC) - timedelta(days=ttl_days)

    total_stmt = (
        select(func.count())
        .select_from(AudienceMember)
        .where(AudienceMember.audience_id == audience_id)
    )
    stale_stmt = (
        select(func.count())
        .select_from(AudienceMember)
        .where(
            AudienceMember.audience_id == audience_id,
            (AudienceMember.fetched_at.is_(None)) | (AudienceMember.fetched_at < cutoff),
        )
    )
    total = (await db.execute(total_stmt)).scalar_one()
    stale = (await db.execute(stale_stmt)).scalar_one()

    return AudienceDetail(
        id=audience.id,
        tenant_id=audience.tenant_id,
        campaign_id=audience.campaign_id,
        name=audience.name,
        segment_criteria=audience.segment_criteria,
        estimated_size=audience.estimated_size,
        actual_size=audience.actual_size,
        refreshed_at=audience.refreshed_at,
        created_at=audience.created_at,
        updated_at=audience.updated_at,
        freshness=AudienceFreshness(
            member_count=total,
            stale_member_count=stale,
            ttl_days=ttl_days,
        ),
    )
