"""Campaign report endpoints (W38, E10-S04 / E13-S04).

  - POST  /api/campaigns/{id}/reports              generate now (marketer+)
  - GET   /api/campaigns/{id}/reports              list versions (viewer+)
  - GET   /api/campaigns/{id}/reports/latest       latest JSON (viewer+)
  - GET   /api/campaigns/{id}/reports/{rid}        specific version (viewer+)
  - GET   /api/campaigns/{id}/reports/latest.csv   CSV export (viewer+)

CSV export uses the stdlib `csv` module — pure-Python, no extra deps. The
PDF format that E10-S04 AC #2 mentions is a polish unit; the JSON payload
is renderer-stable so adding weasyprint later is non-breaking.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.report import generate_report
from app.api.deps import get_tenant_db, require_role
from app.api.schemas.report import (
    CampaignReportListResponse,
    CampaignReportOut,
    CampaignReportSummaryOut,
)
from app.db.enums import UserRole
from app.db.models import AppUser, Campaign, CampaignReport


router = APIRouter(prefix="/api/campaigns", tags=["reports"])


@router.post(
    "/{campaign_id}/reports",
    response_model=CampaignReportOut,
    status_code=status.HTTP_201_CREATED,
)
async def generate_campaign_report(
    campaign_id: UUID,
    user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> CampaignReportOut:
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")

    report = await generate_report(
        db,
        tenant_id=user.tenant_id,
        campaign_id=campaign_id,
        now=datetime.now(UTC),
        generated_by=str(user.id),
    )
    return CampaignReportOut.model_validate(report)


@router.get(
    "/{campaign_id}/reports",
    response_model=CampaignReportListResponse,
)
async def list_campaign_reports(
    campaign_id: UUID,
    user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> CampaignReportListResponse:
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")

    rows = (
        await db.execute(
            select(CampaignReport)
            .where(CampaignReport.campaign_id == campaign_id)
            .order_by(CampaignReport.version.desc())
        )
    ).scalars().all()
    return CampaignReportListResponse(
        items=[CampaignReportSummaryOut.model_validate(r) for r in rows],
        total=len(rows),
    )


@router.get(
    "/{campaign_id}/reports/latest",
    response_model=CampaignReportOut,
)
async def get_latest_report(
    campaign_id: UUID,
    user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> CampaignReportOut:
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")

    report = await _load_latest(db, campaign_id=campaign_id)
    if report is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="no report has been generated yet"
        )
    return CampaignReportOut.model_validate(report)


@router.get(
    "/{campaign_id}/reports/latest.csv",
    response_class=Response,
)
async def get_latest_report_csv(
    campaign_id: UUID,
    user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> Response:
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")

    report = await _load_latest(db, campaign_id=campaign_id)
    if report is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="no report has been generated yet"
        )
    csv_text = _report_to_csv(report.data)
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="campaign-{campaign_id}-report-v{report.version}.csv"'
            ),
        },
    )


@router.get(
    "/{campaign_id}/reports/{report_id}",
    response_model=CampaignReportOut,
)
async def get_campaign_report(
    campaign_id: UUID,
    report_id: UUID,
    user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> CampaignReportOut:
    report = await db.get(CampaignReport, report_id)
    if report is None or report.campaign_id != campaign_id or report.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="report not found")
    return CampaignReportOut.model_validate(report)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_latest(
    session: AsyncSession, *, campaign_id: UUID
) -> CampaignReport | None:
    return (
        await session.execute(
            select(CampaignReport).where(
                CampaignReport.campaign_id == campaign_id,
                CampaignReport.is_latest.is_(True),
            )
        )
    ).scalar_one_or_none()


def _report_to_csv(data: dict[str, Any]) -> str:
    """Flatten the JSON report into `section, key, value` rows. Keeps a
    stable section order so two regenerations produce diffable CSV when
    the data hasn't changed."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["section", "key", "value"])

    # Stable section ordering — matches the order the UI renders.
    order = (
        "objectives",
        "kpis_vs_target",
        "custom_kpis",
        "channel_breakdown",
        "ab_tests",
        "anomalies",
        "recommendations_applied",
        "recommendations_rejected",
        "spend_total",
        "spend_reconciliation",
    )
    for section in order:
        value = data.get(section)
        if value is None:
            writer.writerow([section, "", "(no data)"])
            continue
        _write_section_rows(writer, section, value)
    return buf.getvalue()


def _write_section_rows(
    writer: "csv._writer", section: str, value: Any  # type: ignore[name-defined]
) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            writer.writerow([section, str(k), _stringify(v)])
    elif isinstance(value, list):
        for i, item in enumerate(value):
            if isinstance(item, dict):
                for k, v in item.items():
                    writer.writerow([section, f"[{i}].{k}", _stringify(v)])
            else:
                writer.writerow([section, f"[{i}]", _stringify(item)])
    else:
        writer.writerow([section, "", _stringify(value)])


def _stringify(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        import json

        return json.dumps(v, sort_keys=True)
    return str(v)
