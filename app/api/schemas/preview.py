"""Pydantic schemas for /api/content-assets/{id}/preview surfaces (W24, E06-S07)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PreviewRequest(BaseModel):
    """Optional body for the preview GET — accepts `sample_values` via query
    string OR JSON body so the API stays curl-friendly."""

    model_config = ConfigDict(extra="forbid")

    sample_values: dict[str, str] = Field(default_factory=dict)


class PreviewResponse(BaseModel):
    asset_id: UUID
    asset_type: str
    channel_kind: str | None
    title: str | None
    rendered: dict[str, str]
    referenced_fields: list[str]
    unresolved_fields: list[str]
    resolved_with: dict[str, str]
    channel_constraints: dict[str, Any]


class AudienceAuditEntry(BaseModel):
    field: str
    total_members: int
    unresolved: int


class AudienceAuditResponse(BaseModel):
    asset_id: UUID
    total_members: int
    field_audit: list[AudienceAuditEntry]


class ShareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_days: int | None = Field(default=None, ge=1, le=30)


class ShareResponse(BaseModel):
    asset_id: UUID
    token: str
    url_path: str
    expires_at: datetime
