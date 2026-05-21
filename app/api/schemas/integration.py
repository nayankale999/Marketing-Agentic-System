"""Pydantic schemas for /api/integrations/*."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class IntegrationStatus(BaseModel):
    provider: str
    label: str
    connected: bool
    expires_at: datetime | None
    scopes: list[str] = []


class HubSpotTestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int
    sample: list[dict[str, Any]]
    next_after: str | None = None


class PlausibleSyncRequest(BaseModel):
    """Optional body for the manual Plausible sync trigger."""

    model_config = ConfigDict(extra="forbid")

    days: int = 7


class PlausibleSyncResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fetched: int
    imported: int
    duplicates: int
    unattributed: int
    window_start: datetime
    window_end: datetime
