"""Pydantic schemas for /api/dispatch-attempts (W28, E08-S02/05)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class DispatchAttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    content_asset_id: UUID
    audience_external_id: str | None
    recipient_identifier: str
    idempotency_key: str
    provider: str
    provider_message_id: str | None
    status: str
    attempt_count: int
    last_error: str | None
    sent_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DispatchAttemptListResponse(BaseModel):
    items: list[DispatchAttemptOut]
    total: int
