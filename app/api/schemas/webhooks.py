"""Pydantic schemas for /webhooks/... + /api/webhooks/unmapped (W33)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class WebhookIngestResult(BaseModel):
    """Returned to the provider after a dispatched webhook. We expose the
    raw_webhook id so a provider's retry mechanism can correlate against
    our audit trail if needed."""

    raw_webhook_id: UUID
    provider: str
    signature_valid: bool
    signature_reason: str | None
    events_written: int
    events_deduped: int


class UnmappedWebhookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID | None
    provider: str
    signature_valid: bool
    signature_reason: str | None
    headers: dict[str, Any]
    received_at: datetime


class UnmappedWebhookListResponse(BaseModel):
    items: list[UnmappedWebhookOut]
    total: int
