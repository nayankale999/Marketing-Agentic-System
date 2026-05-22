"""Pydantic schemas for /api/integrations/social (W30, E12-S03)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SocialIntegrationStatus(BaseModel):
    """Whether a social provider is connected for the tenant."""

    provider: str
    connected: bool
    expires_at: datetime | None
    scopes: list[str] = Field(default_factory=list)


class AuthorisedPageOut(BaseModel):
    page_id: str
    page_name: str
    urn: str


class AuthorisedPagesResponse(BaseModel):
    provider: str
    items: list[AuthorisedPageOut]


class CreateSocialChannelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_id: str = Field(min_length=1)
    name: str | None = None


class CreateSocialChannelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    channel_id: UUID
    provider: str
    page_id: str
    page_name: str


class SocialSendTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=3000)


class SocialSendTestResponse(BaseModel):
    provider: str
    provider_post_id: str
    url: str
