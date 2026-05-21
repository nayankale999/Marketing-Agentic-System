"""Audience upload + read endpoints, nested under /api/campaigns/.

W12 ships the CSV upload path (E01-S02 + E01-S03). E04 segmentation/ICP
endpoints land alongside the segmentation tools in W14-W15.
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_role
from app.api.schemas.audience import (
    CsvRowErrorOut,
    CsvUploadResponse,
    CsvUploadSummary,
)
from app.audiences.csv_upload import parse_csv
from app.db.enums import UserRole
from app.db.models import AppUser, Audience, AudienceMember, Campaign

router = APIRouter(prefix="/api/campaigns", tags=["audiences"])

# Hard caps for the sync upload path. Larger files should go through the
# async ingest task once W13 wires it.
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
_MAX_INLINE_ERRORS = 100


@router.post(
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

    First-write-wins on duplicate emails within the file. Per-row errors are
    returned inline (capped at 100); a downloadable error report can be
    layered on top later.
    """
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
            "uploaded_at": datetime.now(UTC).isoformat(),
            "uploaded_by": str(user.id),
        },
        actual_size=len(result.valid),
        refreshed_at=datetime.now(UTC),
    )
    db.add(audience)
    await db.flush()

    if result.valid:
        db.add_all(
            AudienceMember(
                audience_id=audience.id,
                external_id=row.external_id,
                payload=row.payload,
            )
            for row in result.valid
        )
        await db.flush()

    failed = sum(1 for e in result.errors if e.reason != "duplicate_in_file")
    skipped_dup = sum(1 for e in result.errors if e.reason == "duplicate_in_file")

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
