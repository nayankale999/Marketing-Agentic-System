"""Pydantic schemas for /api/ingest/jobs (E01-S06)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.db.enums import TaskStatus


class IngestJobOut(BaseModel):
    """One ingest job (a `task` row with `skill_name` starting with `ingest.`)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    skill_name: str
    status: TaskStatus
    attempt: int
    scheduled_for: datetime
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    input_data: dict[str, Any]
    output_data: dict[str, Any]
    error_message: str | None
    worker_id: str | None


class IngestJobListResponse(BaseModel):
    items: list[IngestJobOut]
    total: int
    limit: int
    offset: int
