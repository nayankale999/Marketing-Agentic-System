"""UI routes for the outbound personalisation flow (W43).

* GET  /ui/outbound/upload           — CSV upload form (picks a campaign).
* POST /ui/outbound/upload           — multipart submit; redirects to the
                                       drafts page on success.
* GET  /ui/campaigns/{id}/personalised-drafts
                                     — list every rendered per-contact
                                       LinkedIn DM + email, grouped by
                                       contact, with copy-to-clipboard.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_role
from app.api.ui.templates import templates
from app.audiences.csv_upload import parse_csv
from app.db.enums import TaskStatus, UserRole
from app.db.models import (
    AppUser,
    Audience,
    AudienceMember,
    Campaign,
    Task,
)
from app.orchestrator.state_machine import _ensure_orchestrator_agent
from app.outbound import render_personalised_drafts

router = APIRouter(prefix="/ui", tags=["ui-outbound"])

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@router.get("/outbound/upload", response_class=HTMLResponse)
async def upload_form(
    request: Request,
    current_user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> HTMLResponse:
    """Show the CSV upload form. The campaign dropdown lists campaigns
    in `drafted`, `audience_built`, or `strategy_set` — those are the
    states where adding an audience makes sense."""
    campaigns = (
        await db.execute(
            select(Campaign).order_by(Campaign.created_at.desc()).limit(50)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request,
        "outbound_upload.html",
        {
            "campaigns": campaigns,
            "current_user": current_user,
        },
    )


@router.post("/outbound/upload", response_class=HTMLResponse)
async def upload_submit(
    request: Request,
    file: Annotated[UploadFile, File()],
    campaign_id: Annotated[UUID, Form()],
    audience_name: Annotated[str | None, Form()] = None,
    current_user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> HTMLResponse:
    """Handle the multipart upload. Wraps the API's CSV import flow but
    redirects to a UI page on success rather than returning JSON."""
    started = datetime.now(UTC)
    body = await file.read()
    if len(body) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file exceeds {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit",
        )

    campaign = await db.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != current_user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return templates.TemplateResponse(
            request,
            "outbound_upload.html",
            {
                "campaigns": [campaign],
                "current_user": current_user,
                "error": "File is not valid UTF-8 — re-save the CSV as UTF-8 and try again.",
            },
            status_code=400,
        )

    result = parse_csv(text)
    if result.total_rows == 0 and result.errors:
        return templates.TemplateResponse(
            request,
            "outbound_upload.html",
            {
                "campaigns": [campaign],
                "current_user": current_user,
                "error": f"CSV format error: {result.errors[0].reason}",
            },
            status_code=400,
        )

    name = audience_name or f"CSV upload — {file.filename or 'unnamed'}"
    audience = Audience(
        tenant_id=current_user.tenant_id,
        campaign_id=campaign_id,
        name=name,
        segment_criteria={
            "source": "csv",
            "filename": file.filename,
            "uploaded_at": started.isoformat(),
            "uploaded_by": str(current_user.id),
        },
        actual_size=len(result.valid),
        refreshed_at=started,
    )
    db.add(audience)
    await db.flush()
    db.add_all(
        AudienceMember(
            audience_id=audience.id,
            external_id=row.external_id,
            payload=row.payload,
            source="csv",
            fetched_at=started,
        )
        for row in result.valid
    )
    await db.flush()

    completed = datetime.now(UTC)
    agent = await _ensure_orchestrator_agent(db, current_user.tenant_id)
    db.add(
        Task(
            tenant_id=current_user.tenant_id,
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
                "audience_id": str(audience.id),
            },
            scheduled_for=started,
            started_at=started,
            completed_at=completed,
        )
    )
    await db.flush()

    # Land the user on the drafts page — even before enrichment + generation
    # they can see the imported contacts and trigger the next steps from
    # there (or via the assistant).
    return RedirectResponse(
        url=f"/ui/campaigns/{campaign_id}/personalised-drafts"
        f"?audience_id={audience.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get(
    "/campaigns/{campaign_id}/personalised-drafts",
    response_class=HTMLResponse,
)
async def personalised_drafts_page(
    request: Request,
    campaign_id: UUID,
    audience_id: UUID | None = None,
    current_user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> HTMLResponse:
    """List every per-contact LinkedIn DM + email draft, grouped by
    contact. If no audience_id is given, pick the most recent audience
    on the campaign — there's usually one."""
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != current_user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")

    if audience_id is None:
        audience = (
            await db.execute(
                select(Audience)
                .where(Audience.campaign_id == campaign_id)
                .order_by(Audience.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if audience is None:
            return templates.TemplateResponse(
                request,
                "outbound_drafts.html",
                {
                    "campaign": campaign,
                    "audience": None,
                    "drafts_by_contact": [],
                    "current_user": current_user,
                    "member_count": 0,
                },
            )
        audience_id = audience.id
    else:
        audience = await db.get(Audience, audience_id)
        if audience is None or audience.tenant_id != current_user.tenant_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="audience not found")

    member_count = (
        await db.execute(
            select(AudienceMember).where(AudienceMember.audience_id == audience_id)
        )
    ).scalars().all()

    drafts = await render_personalised_drafts(
        db, campaign_id=campaign_id, audience_id=audience_id
    )

    # Group drafts by contact for display.
    grouped: dict[str, dict] = {}
    for d in drafts:
        bucket = grouped.setdefault(
            d.contact_email,
            {
                "email": d.contact_email,
                "name": d.contact_name,
                "title": d.contact_title,
                "company": d.contact_company,
                "linkedin_url": d.contact_linkedin_url,
                "segment_label": d.segment_label,
                "linkedin_dm": None,
                "email_draft": None,
            },
        )
        if d.channel == "linkedin_dm":
            bucket["linkedin_dm"] = {"body": d.body}
        else:
            bucket["email_draft"] = {"subject": d.subject, "body": d.body}

    drafts_by_contact = list(grouped.values())

    return templates.TemplateResponse(
        request,
        "outbound_drafts.html",
        {
            "campaign": campaign,
            "audience": audience,
            "drafts_by_contact": drafts_by_contact,
            "member_count": len(member_count),
            "current_user": current_user,
        },
    )
