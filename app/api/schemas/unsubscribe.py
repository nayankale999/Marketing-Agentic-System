"""Schemas for /api/unsubscribe/{token} (W29, E16-S04 #2)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class UnsubscribeResponse(BaseModel):
    """Returned to the bearer of a valid unsubscribe token. Same shape on
    repeated calls (idempotent) so retries don't surprise anyone."""

    model_config = ConfigDict(from_attributes=True)

    tenant_id: UUID
    channel_platform: str
    identifier: str
    suppressed_at: datetime
    already_existed: bool
