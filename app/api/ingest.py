"""Operator endpoint listing ingest jobs (E01-S06).

An "ingest job" is a `task` row whose `skill_name` starts with `ingest.`.
W13 only emits one variant — `ingest.csv_upload` from the CSV uploader.
Future CRM / web-analytics syncs (W11/W16) enqueue tasks under the same
prefix and show up here automatically.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_role
from app.api.schemas.ingest import IngestJobListResponse, IngestJobOut
from app.db.enums import TaskStatus, UserRole
from app.db.models import AppUser, Task

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


def _duration_ms(task: Task) -> int | None:
    if task.started_at is None or task.completed_at is None:
        return None
    delta = task.completed_at - task.started_at
    return int(delta.total_seconds() * 1000)


@router.get("/jobs", response_model=IngestJobListResponse)
async def list_ingest_jobs(
    status_filter: Annotated[TaskStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> IngestJobListResponse:
    """List recent ingest jobs in this tenant, newest first."""
    stmt = select(Task).where(Task.skill_name.like("ingest.%")).order_by(Task.scheduled_for.desc())
    if status_filter is not None:
        stmt = stmt.where(Task.status == status_filter)

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()

    items = [
        IngestJobOut(
            id=row.id,
            skill_name=row.skill_name,
            status=row.status,
            attempt=row.attempt,
            scheduled_for=row.scheduled_for,
            started_at=row.started_at,
            completed_at=row.completed_at,
            duration_ms=_duration_ms(row),
            input_data=row.input_data,
            output_data=row.output_data,
            error_message=row.error_message,
            worker_id=row.worker_id,
        )
        for row in rows
    ]
    return IngestJobListResponse(items=items, total=total, limit=limit, offset=offset)
