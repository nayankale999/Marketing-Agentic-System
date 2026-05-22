"""Pydantic schemas for /api/integrations/email surfaces (W27, E12-S02)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EmailConfigUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=40)
    api_key: str = Field(min_length=1)
    default_from_email: str = Field(min_length=3, max_length=320)
    verified_senders: list[str] = Field(min_length=1)
    webhook_secret: str | None = Field(default=None, max_length=200)


class EmailConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    provider: str
    default_from_email: str
    verified_senders: list[str]
    api_key_last4: str
    has_webhook_secret: bool
    created_at: datetime
    updated_at: datetime


class SendTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_email: str = Field(min_length=3, max_length=320)
    from_email: str | None = Field(default=None, max_length=320)
    subject: str = Field(default="MAS test message", max_length=200)
    body: str = Field(default="This is a test send from MAS.", max_length=2000)


class SendTestResponse(BaseModel):
    accepted: bool
    provider: str
    provider_message_id: str | None
    error: str | None = None


class WebhookIngestResponse(BaseModel):
    received: int
    written: int
    deduped: int
    suppression_writes: int
